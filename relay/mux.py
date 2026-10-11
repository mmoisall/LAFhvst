"""단일 WebSocket 세션 위의 스트림 멀티플렉서.

프로토콜: `relay/protocol.md` §2~§5. PC 측 에이전트(`core/remote_tunnel.py`)와
같은 계약을 공유한다.

- 프레이밍: `1바이트 type + 4바이트 BE stream_id + payload`.
- `stream_id` 는 세션 내 단조 증가(0 은 세션 제어 전용, 재사용 금지).
- 한계: 요청 본문 8 MiB · 프레임 1 MiB · 응답 헤더 대기 30초 · 스트림 무진행 60초.
- 동시 스트림 16 — 초과분은 `TooManyStreams` 로 거부한다(HTTP 429).
- 백프레셔: 스트림 큐가 넘치면 **그 스트림만** CANCEL 하고 세션은 유지한다.

한계값은 모듈 전역이라 테스트에서 monkeypatch 할 수 있다(운영에서는 환경변수).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Protocol

from starlette.websockets import WebSocketDisconnect

from .registry import Registry, is_valid_host_id

log = logging.getLogger("relay.mux")

PROTOCOL_VERSION = "1"

# ── 프레임 타입 (protocol.md §2) ────────────────────────────────────────
TYPE_HELLO = 0x01
TYPE_HELLO_OK = 0x02
TYPE_HELLO_ERR = 0x03
TYPE_REQ_HEAD = 0x10
TYPE_REQ_BODY = 0x11
TYPE_REQ_END = 0x12
TYPE_RES_HEAD = 0x20
TYPE_RES_BODY = 0x21
TYPE_RES_END = 0x22
TYPE_CANCEL = 0x30
TYPE_PING = 0x40
TYPE_PONG = 0x41

# ── 한계값 (protocol.md §5) ─────────────────────────────────────────────
MAX_FRAME = int(os.environ.get("LAF_RELAY_MAX_FRAME", 1 * 1024 * 1024))
MAX_BODY = int(os.environ.get("LAF_RELAY_MAX_BODY", 8 * 1024 * 1024))
MAX_STREAMS = int(os.environ.get("LAF_RELAY_MAX_STREAMS", 16))
HEADER_TIMEOUT = float(os.environ.get("LAF_RELAY_HEADER_TIMEOUT", 30.0))
STREAM_IDLE_TIMEOUT = float(os.environ.get("LAF_RELAY_STREAM_IDLE_TIMEOUT", 60.0))
PING_INTERVAL = float(os.environ.get("LAF_RELAY_PING_INTERVAL", 20.0))
PING_TIMEOUT = float(os.environ.get("LAF_RELAY_PING_TIMEOUT", 60.0))
HELLO_TIMEOUT = float(os.environ.get("LAF_RELAY_HELLO_TIMEOUT", 30.0))
QUEUE_MAX = int(os.environ.get("LAF_RELAY_QUEUE_MAX", 32))

WATCHDOG_INTERVAL = 0.25
REPLACED_CLOSE_CODE = 4000  # protocol.md §3.3 — 새 세션이 기존 세션을 대체
POLICY_CLOSE_CODE = 1008  # HELLO 거부
PROTOCOL_CLOSE_CODE = 1002  # 프레이밍/프로토콜 오류
HEARTBEAT_CLOSE_CODE = 1001  # WS 무응답

# 취소 사유 → 클라이언트 HTTP 상태 (protocol.md §5, §6)
REASON_STATUS: dict[str, int] = {
    "too_large": 413,
    "no_progress": 504,
    "header_timeout": 504,
    "timeout": 504,
    "backpressure": 502,
    "cancelled": 502,
    "replaced": 502,
    "closed": 502,
    "client_gone": 499,
}
REASON_DETAIL: dict[str, str] = {
    "too_large": "request body too large",
    "no_progress": "upstream timeout",
    "header_timeout": "upstream timeout",
    "timeout": "upstream timeout",
    "backpressure": "upstream backpressure",
    "client_gone": "client disconnect",
}
AGENT_REASONS = {"too_large", "timeout", "error", "cancelled"}


class ProtocolError(RuntimeError):
    """프레이밍/JSON 규약 위반 — 세션을 닫는다."""


class TooManyStreams(RuntimeError):
    """세션당 동시 스트림 상한 초과 — 클라이언트에 429."""


def status_for_reason(reason: str | None) -> int:
    """취소 사유를 클라이언트 상태 코드로 매핑한다(기본 502)."""
    return REASON_STATUS.get(reason or "", 502)


def detail_for_reason(reason: str | None) -> str:
    return REASON_DETAIL.get(reason or "", "relay stream cancelled")


# ── 프레이밍 ────────────────────────────────────────────────────────────
def encode_frame(frame_type: int, stream_id: int = 0, payload: bytes = b"") -> bytes:
    """`1바이트 type + 4바이트 BE stream_id + payload`."""
    return bytes((frame_type,)) + stream_id.to_bytes(4, "big") + payload


def decode_frame(raw: bytes) -> tuple[int, int, bytes]:
    """프레임을 (type, stream_id, payload) 로 분해한다."""
    if len(raw) < 5:
        raise ProtocolError("frame shorter than 5 bytes")
    return raw[0], int.from_bytes(raw[1:5], "big"), raw[5:]


def dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def loads(payload: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"invalid JSON payload: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("JSON payload must be an object")
    return obj


# ── 스트림 ──────────────────────────────────────────────────────────────
class Stream:
    """요청 1건에 대응하는 스트림. 에이전트가 보낸 프레임을 큐로 전달한다."""

    __slots__ = (
        "id",
        "queue",
        "created",
        "last_activity",
        "request_ended_at",
        "status",
        "res_headers",
        "cancel_reason",
        "cancel_notified",
    )

    def __init__(self, stream_id: int) -> None:
        self.id = stream_id
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(QUEUE_MAX)
        now = time.monotonic()
        self.created = now
        self.last_activity = now
        self.request_ended_at: float | None = None
        self.status: int | None = None
        self.res_headers: dict[str, str] = {}
        self.cancel_reason: str | None = None
        self.cancel_notified = False

    @property
    def ready(self) -> bool:
        """응답 헤더를 받았는지."""
        return self.status is not None

    def touch(self) -> None:
        self.last_activity = time.monotonic()


class _Socket(Protocol):
    """starlette `WebSocket` 중 쓰는 부분만."""

    async def send_bytes(self, data: bytes) -> None: ...

    async def receive(self) -> dict[str, Any]: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


# ── 세션 ────────────────────────────────────────────────────────────────
class AgentSession:
    """에이전트 1개(= host 1개)와의 WS 세션."""

    def __init__(self, websocket: _Socket, host_id: str, label: str = "") -> None:
        self.websocket = websocket
        self.host_id = host_id
        self.label = label
        self.id = uuid.uuid4().hex
        self.streams: dict[int, Stream] = {}
        self._next_stream_id = 1
        self._send_lock = asyncio.Lock()
        self._pong_at = time.monotonic()
        self._closed = False

    # ── 상태 ────────────────────────────────────────────────────────────
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stream_count(self) -> int:
        return len(self.streams)

    # ── 송신 ────────────────────────────────────────────────────────────
    async def send_frame(self, frame_type: int, stream_id: int = 0, payload: bytes = b"") -> None:
        async with self._send_lock:
            await self.websocket.send_bytes(encode_frame(frame_type, stream_id, payload))

    async def send_json(self, frame_type: int, stream_id: int, payload: dict[str, Any]) -> None:
        await self.send_frame(frame_type, stream_id, dumps(payload))

    # ── 스트림 수명 ─────────────────────────────────────────────────────
    def open_stream(self) -> Stream:
        """새 스트림을 연다. 동시 16개 초과면 `TooManyStreams`."""
        if len(self.streams) >= MAX_STREAMS:
            raise TooManyStreams(f"host {self.host_id} already has {len(self.streams)} streams")
        stream_id = self._next_stream_id
        self._next_stream_id += 1
        if self._next_stream_id > 0xFFFFFFFF:
            raise ProtocolError("stream id space exhausted")
        stream = Stream(stream_id)
        self.streams[stream_id] = stream
        return stream

    def close_stream(self, stream_id: int) -> None:
        """스트림 등록을 해제한다(늦게 도착한 프레임은 무시된다)."""
        self.streams.pop(stream_id, None)

    async def cancel_stream(self, stream_id: int, reason: str, notify: bool = True) -> None:
        """스트림을 취소하고 종료 이벤트를 큐에 넣는다(세션은 유지)."""
        stream = self.streams.get(stream_id)
        if stream is None:
            return
        await self._cancel_stream(stream, reason, notify=notify)

    def cancel_soon(self, stream_id: int, reason: str, notify: bool = True) -> None:
        """await 할 수 없는 곳(제너레이터 finally 등)에서 취소를 예약한다."""
        stream = self.streams.get(stream_id)
        if stream is None or stream.cancel_reason is not None:
            return
        stream.cancel_reason = reason
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_and_end(stream)
            return
        loop.create_task(self._cancel_stream(stream, reason, notify=notify))

    async def _cancel_stream(self, stream: Stream, reason: str, notify: bool = True) -> None:
        if stream.cancel_reason is None:
            stream.cancel_reason = reason
        self._flush_and_end(stream)
        if notify and not stream.cancel_notified:
            stream.cancel_notified = True
            try:
                await self.send_frame(TYPE_CANCEL, stream.id, dumps({"reason": reason}))
            except Exception as exc:  # 세션이 이미 죽은 경우
                log.debug("cancel frame 전송 실패 stream=%s: %r", stream.id, exc)

    def _flush_and_end(self, stream: Stream) -> None:
        """대기 중인 프레임을 버리고 종료 마커를 넣는다(취소 통지)."""
        while True:
            try:
                stream.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        try:
            stream.queue.put_nowait(("cancel", stream.cancel_reason))
        except asyncio.QueueFull:  # pragma: no cover — 큐는 방금 비웠다
            log.warning("stream %s 종료 마커 실패", stream.id)

    def _deliver(self, stream: Stream, item: tuple[str, Any]) -> None:
        """큐에 프레임을 넣는다. 넘치면 해당 스트림만 취소한다(세션 유지)."""
        try:
            stream.queue.put_nowait(item)
        except asyncio.QueueFull:
            log.warning("stream %s backpressure — cancel", stream.id)
            self.cancel_soon(stream.id, "backpressure")

    # ── 수신 루프 ───────────────────────────────────────────────────────
    async def run(self) -> None:
        """HELLO_OK 이후 프레임 수신 루프. 종료 시 스트림을 정리한다."""
        heartbeat = asyncio.create_task(self._heartbeat())
        watchdog = asyncio.create_task(self._watchdog())
        try:
            while not self._closed:
                message = await self.websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("bytes")
                if raw is None:
                    raise ProtocolError("agent must send binary frames")
                if len(raw) - 5 > MAX_FRAME:
                    raise ProtocolError("frame exceeds 1 MiB")
                frame_type, stream_id, payload = decode_frame(raw)
                self._pong_at = time.monotonic()
                await self._dispatch(frame_type, stream_id, payload)
        except WebSocketDisconnect:
            pass
        except ProtocolError as exc:
            log.warning("protocol error host=%s: %s", self.host_id, exc)
            await self.shutdown("closed", close_code=PROTOCOL_CLOSE_CODE, close_reason="protocol error")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 예기치 못한 오류 — 세션만 닫는다
            log.exception("session 오류 host=%s: %r", self.host_id, exc)
        finally:
            heartbeat.cancel()
            watchdog.cancel()
            await self.shutdown("closed")

    async def _dispatch(self, frame_type: int, stream_id: int, payload: bytes) -> None:
        if frame_type == TYPE_PING:
            await self.send_frame(TYPE_PONG, 0)
            return
        if frame_type == TYPE_PONG:
            return
        if stream_id == 0:
            raise ProtocolError(f"type {frame_type:#x} not allowed on stream 0")
        stream = self.streams.get(stream_id)
        if frame_type == TYPE_CANCEL:
            if stream is None:
                return
            reason = "cancelled"
            try:
                reason = str(loads(payload).get("reason") or "cancelled")
            except ProtocolError:
                pass
            reason = reason if reason in AGENT_REASONS else "cancelled"
            await self._cancel_stream(stream, reason, notify=False)
            return
        if stream is None:
            # 이미 끝난/취소된 스트림에 대한 늦은 프레임은 무시한다.
            return
        if frame_type == TYPE_RES_HEAD:
            data = loads(payload)
            status = data.get("status")
            if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
                raise ProtocolError("RES_HEAD status must be an int in 100..599")
            headers = data.get("headers") or {}
            if not isinstance(headers, dict):
                raise ProtocolError("RES_HEAD headers must be an object")
            stream.touch()
            stream.status = status
            stream.res_headers = {str(key).lower(): str(value) for key, value in headers.items()}
            self._deliver(stream, ("head", None))
        elif frame_type == TYPE_RES_BODY:
            stream.touch()
            self._deliver(stream, ("body", payload))
        elif frame_type == TYPE_RES_END:
            stream.touch()
            self._deliver(stream, ("end", None))
        else:
            raise ProtocolError(f"unexpected type {frame_type:#x} from agent")

    # ── 타이머 ──────────────────────────────────────────────────────────
    async def _watchdog(self) -> None:
        """헤더 대기 30초 · 무진행 60초 → CANCEL(+클라이언트 504)."""
        try:
            while not self._closed:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                now = time.monotonic()
                for stream in list(self.streams.values()):
                    if stream.cancel_reason is not None:
                        continue
                    anchor = stream.request_ended_at or stream.created
                    if not stream.ready and now - anchor > HEADER_TIMEOUT:
                        await self._cancel_stream(stream, "header_timeout")
                    elif now - stream.last_activity > STREAM_IDLE_TIMEOUT:
                        await self._cancel_stream(stream, "no_progress")
        except asyncio.CancelledError:
            raise

    async def _heartbeat(self) -> None:
        """20초마다 PING, 60초 무응답이면 세션을 닫는다."""
        try:
            while not self._closed:
                await asyncio.sleep(PING_INTERVAL)
                if self._closed:
                    return
                if time.monotonic() - self._pong_at > PING_TIMEOUT:
                    log.warning("heartbeat timeout host=%s", self.host_id)
                    await self._close_socket(HEARTBEAT_CLOSE_CODE, "heartbeat timeout")
                    return
                await self.send_frame(TYPE_PING, 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 소켓이 이미 닫힌 경우
            log.debug("heartbeat 종료 host=%s: %r", self.host_id, exc)

    # ── 종료 ────────────────────────────────────────────────────────────
    async def shutdown(
        self,
        reason: str = "closed",
        *,
        close_code: int | None = None,
        close_reason: str = "",
    ) -> None:
        """세션을 닫고 열린 스트림을 모두 취소한다."""
        self.close_streams(reason)
        if close_code is not None:
            await self._close_socket(close_code, close_reason or reason)

    def close_streams(self, reason: str = "closed") -> None:
        """소켓을 건드리지 않고 스트림만 정리한다(동기 정리용)."""
        self._closed = True
        for stream in list(self.streams.values()):
            if stream.cancel_reason is None:
                stream.cancel_reason = reason
            self._flush_and_end(stream)

    async def _close_socket(self, code: int, reason: str) -> None:
        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception as exc:  # 이미 끊긴 소켓
            log.debug("close 실패 host=%s: %r", self.host_id, exc)


# ── 세션 등록소 ─────────────────────────────────────────────────────────
class SessionHub:
    """`/agent` 세션 등록소 — host_id 당 1개, 새 세션이 기존 세션을 대체한다."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.sessions: dict[str, AgentSession] = {}

    def get(self, host_id: str) -> AgentSession | None:
        session = self.sessions.get(host_id)
        if session is None or session.closed:
            return None
        return session

    def counts(self) -> tuple[int, int, int]:
        """(등록 호스트 수, 활성 세션 수, 열린 스트림 수) — `/healthz`."""
        streams = sum(session.stream_count for session in self.sessions.values())
        return len(self.registry), len(self.sessions), streams

    async def serve(self, websocket: _Socket) -> None:
        """`WS /agent` 전체 수명: HELLO 검증 → 등록 → 수신 루프."""
        await websocket.accept()
        try:
            hello = await asyncio.wait_for(_read_hello(websocket), HELLO_TIMEOUT)
        except (asyncio.TimeoutError, WebSocketDisconnect):
            await _reject_hello(websocket, "bad_hello", "HELLO timeout")
            return
        except ProtocolError as exc:
            await _reject_hello(websocket, "bad_hello", str(exc))
            return

        version = hello.get("version")
        host_id = hello.get("host_id")
        token = hello.get("agent_token")
        if str(version) != PROTOCOL_VERSION:
            await _reject_hello(websocket, "version", f"unsupported protocol version: {version!r}")
            return
        if not is_valid_host_id(host_id):
            await _reject_hello(websocket, "bad_host_id", "host_id must match [A-Za-z0-9_-]{16,}")
            return
        if not isinstance(token, str) or not token:
            await _reject_hello(websocket, "bad_hello", "agent_token required")
            return
        assert isinstance(host_id, str)

        try:
            result = self.registry.bind_or_verify(host_id, token)
        except OSError as exc:
            log.error("registry 저장 실패 host=%s: %r", host_id, exc)
            await _reject_hello(websocket, "host_id_taken", "registry unavailable")
            return
        if result == "bad_host_id":
            await _reject_hello(websocket, "bad_host_id", "host_id must match [A-Za-z0-9_-]{16,}")
            return
        if result == "mismatch":
            await _reject_hello(websocket, "host_id_taken", "host_id already bound to another token")
            return

        label = hello.get("label")
        session = AgentSession(websocket, host_id, label if isinstance(label, str) else "")
        previous = self.sessions.get(host_id)
        self.sessions[host_id] = session
        if previous is not None and previous is not session:
            # protocol.md §3.3 — 재연결을 흡수하기 위해 기존 세션을 닫는다.
            await previous.shutdown(
                "replaced", close_code=REPLACED_CLOSE_CODE, close_reason="replaced"
            )
        try:
            await session.send_json(
                TYPE_HELLO_OK,
                0,
                {"host_id": host_id, "session": session.id, "streams_max": MAX_STREAMS},
            )
        except Exception as exc:
            log.warning("HELLO_OK 전송 실패 host=%s: %r", host_id, exc)
            if self.sessions.get(host_id) is session:
                self.sessions.pop(host_id, None)
            return
        log.info("agent connected host=%s session=%s label=%s", host_id, session.id, session.label)
        try:
            await session.run()
        finally:
            if self.sessions.get(host_id) is session:
                self.sessions.pop(host_id, None)
            await session.shutdown("closed")
            log.info("agent disconnected host=%s session=%s", host_id, session.id)


async def _read_hello(websocket: _Socket) -> dict[str, Any]:
    """HELLO 프레임 1개를 읽는다(세션 제어 스트림 0)."""
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect()
    raw = message.get("bytes")
    if raw is None:
        raise ProtocolError("HELLO must be a binary frame")
    if len(raw) - 5 > MAX_FRAME:
        raise ProtocolError("HELLO frame exceeds 1 MiB")
    frame_type, stream_id, payload = decode_frame(raw)
    if frame_type != TYPE_HELLO or stream_id != 0:
        raise ProtocolError("first frame must be HELLO on stream 0")
    return loads(payload)


async def _reject_hello(websocket: _Socket, code: str, message: str) -> None:
    """HELLO_ERR 전송 후 소켓을 닫는다(protocol.md §2)."""
    log.info("HELLO 거부 code=%s message=%s", code, message)
    try:
        await websocket.send_bytes(encode_frame(TYPE_HELLO_ERR, 0, dumps({"code": code, "message": message})))
    except Exception:
        pass
    try:
        await websocket.close(code=POLICY_CLOSE_CODE, reason=code)
    except Exception:
        pass
