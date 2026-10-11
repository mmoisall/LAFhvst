"""릴레이 에이전트 — PC(앱)가 릴레이에 **아웃바운드**로 붙어 원격 요청을 로컬 앱으로 중계한다.

계약: `relay/protocol.md` (v1). 포트포워딩이 필요 없도록 PC 는 나가는 연결만 연다.

구성
    폰 → 릴레이(HTTPS) → [이 에이전트의 WS] → 127.0.0.1:<target_port> (원격 리스너)

원격 리스너(`core/remote_listener.RemoteOnly`)가 요청을 항상 "원격"으로 표시하므로,
릴레이 경유 사용자는 로컬 권한을 얻지 못한다.

v1 구현 범위
- 요청 본문은 메모리에 모아서 전달한다(앱 요청 본문은 작다). 응답은 **조각 단위로 스트리밍**한다
  → `text/event-stream`(SSE) 같은 장기 응답도 릴레이를 통과한다. 비(非)스트리밍 응답은
  `MAX_RESPONSE_BYTES` 초과 시 잘라내고 스트림을 취소한다.
- 전송 실패는 지수 백오프로 재연결한다. `host_id` 선점(409)·형식 오류(404)는 재시도하지 않는다.
- 로컬 앱과는 HTTP/1.1 **keep-alive 연결 풀**로 통신한다(요청당 새 연결을 열지 않는다).
  청크 사이 대기는 `READ_TIMEOUT`(30초)이므로 SSE 는 하트비트를 30초보다 짧게 보내야 한다.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import struct
import time
from urllib.parse import quote, urlsplit

from websockets.asyncio.client import connect

PROTOCOL_VERSION = "1"
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
RESPONSE_CHUNK = 256 * 1024
FRAME_LIMIT = 2 * 1024 * 1024
HEADER_TIMEOUT = 30.0
READ_TIMEOUT = 30.0
STREAM_CONTENT_TYPES = ("text/event-stream",)
PING_INTERVAL = 20.0
PING_TIMEOUT = 60.0
OPEN_TIMEOUT = 15.0
BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
FATAL_HELLO_CODES = {"host_id_taken", "bad_host_id", "version"}

T_HELLO = 0x01
T_HELLO_OK = 0x02
T_HELLO_ERR = 0x03
T_REQ_HEAD = 0x10
T_REQ_BODY = 0x11
T_REQ_END = 0x12
T_RES_HEAD = 0x20
T_RES_BODY = 0x21
T_RES_END = 0x22
T_CANCEL = 0x30
T_PING = 0x40
T_PONG = 0x41

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
}


def encode(frame_type: int, stream_id: int, payload: bytes = b"") -> bytes:
    return bytes([frame_type]) + struct.pack(">I", stream_id) + payload


def decode(message) -> tuple[int, int, bytes]:
    if isinstance(message, str):
        message = message.encode("utf-8")
    if len(message) < 5:
        raise ValueError("frame too short")
    return message[0], struct.unpack(">I", message[1:5])[0], message[5:]


def encode_json(frame_type: int, stream_id: int, payload: dict) -> bytes:
    return encode(frame_type, stream_id, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def decode_json(payload: bytes) -> dict:
    if not payload:
        return {}
    data = json.loads(payload.decode("utf-8"))
    return data if isinstance(data, dict) else {}


_DEBUG = os.environ.get("LAF_AGENT_DEBUG") == "1"


def _debug(message: str) -> None:
    """진단용 로그(LAF_AGENT_DEBUG=1 일 때만)."""
    if _DEBUG:
        print("[agent] " + message, flush=True)


def agent_url(relay_url: str) -> str:
    """`https://relay.example.com` → `wss://relay.example.com/agent`."""
    parts = urlsplit(relay_url if "://" in relay_url else "https://" + relay_url)
    scheme = "wss" if parts.scheme != "http" else "ws"
    host = parts.netloc or parts.path
    return "%s://%s/agent" % (scheme, host)


def public_url(relay_url: str, host_id: str) -> str:
    parts = urlsplit(relay_url if "://" in relay_url else "https://" + relay_url)
    scheme = parts.scheme or "https"
    host = parts.netloc or parts.path
    return "%s://%s/h/%s" % (scheme, host, quote(host_id, safe=""))


def _filtered_headers(headers: dict) -> dict:
    return {
        str(key): str(value)
        for key, value in (headers or {}).items()
        if str(key).lower() not in HOP_BY_HOP
    }


async def _open_local_connection(host: str, port: int, timeout: float):
    """로컬 앱 연결. 순간적인 accept 실패(Windows loopback RST)에 한해 1회 재시도한다.

    요청을 보내기 전 단계이므로 재시도해도 부작용이 없다.
    """
    last: Exception | None = None
    for attempt in (0, 1):
        try:
            return await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 0:
                await asyncio.sleep(0.05)
    raise last if last is not None else RuntimeError("connect failed")


def _framing(headers: dict) -> str:
    """응답 본문 경계 방식: `chunked` | `length` | `close`."""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        return "chunked"
    if str(headers.get("content-length", "")).isdigit():
        return "length"
    return "close"


class LocalRequestNotSent(Exception):
    """요청 바이트가 서버로 전달되기 전에 실패했음(재시도 안전)."""


class LocalPool:
    """로컬 앱(원격 리스너)에 대한 keep-alive 연결 풀.

    요청마다 TCP 연결을 새로 열면 Windows loopback 에서 간헐적인 RST(즉시 close 로 인한
    연결 재설정)에 노출된다. 연결을 재사용하면 그 창이 크게 줄고 썸네일 지연도 개선된다.
    동시 요청이 풀 상한을 넘으면 **일회성 연결**을 열어 처리한다(대기로 인한 교착 없음).

    유휴 연결은 `idle_ttl` 초가 지나면 버린다(서버의 keep-alive 타임아웃보다 짧게 유지).
    그럼에도 서버가 먼저 닫은 연결을 잡으면, 요청이 **전달되기 전** 실패한 경우에 한해
    새 연결로 1회 재시도한다(멱등 메서드 또는 미전송 실패).
    """

    IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

    def __init__(
        self,
        host: str,
        port: int,
        *,
        max_connections: int = 4,
        connect_timeout: float = READ_TIMEOUT,
        idle_ttl: float = 60.0,
    ) -> None:
        self.host = host
        self.port = port
        self.max_connections = max(1, int(max_connections))
        self.connect_timeout = connect_timeout
        self.idle_ttl = float(idle_ttl)
        self._idle: list = []
        self._open = 0
        self._lock = asyncio.Lock()

    async def _take(self):
        """유휴 연결을 꺼내거나 새로 연다. 반환: (reader, writer, pooled)."""
        async with self._lock:
            now = time.monotonic()
            while self._idle:
                reader, writer, used_at = self._idle.pop()
                if not writer.is_closing() and (now - used_at) < self.idle_ttl:
                    return reader, writer, True
                self._close(writer, True)
            if self._open < self.max_connections:
                self._open += 1
                try:
                    reader, writer = await _open_local_connection(
                        self.host, self.port, self.connect_timeout
                    )
                except Exception:
                    self._open -= 1
                    raise
                return reader, writer, True
        # 풀이 가득 찼다: 일회성 연결로 처리한다(재사용하지 않음).
        reader, writer = await _open_local_connection(self.host, self.port, self.connect_timeout)
        return reader, writer, False

    async def request(self, method: str, path: str, query: str, headers: dict, body: bytes):
        """요청을 보내고 `(reader, writer, status, headers, pooled)` 를 돌려준다."""
        method = (method or "GET").upper()
        for attempt in (0, 1):
            reader, writer, pooled = await self._take()
            try:
                status, out_headers = await _send_request(
                    reader, writer, self.host, self.port, method, path, query, headers, body
                )
            except LocalRequestNotSent:
                self._discard(reader, writer, pooled)
                if attempt == 0 and pooled:
                    continue  # 서버가 닫은 유휴 연결 → 새 연결로 재시도(요청은 전달되지 않음)
                raise
            except Exception:
                self._discard(reader, writer, pooled)
                if attempt == 0 and pooled and method in self.IDEMPOTENT_METHODS:
                    continue  # 멱등 메서드는 응답을 못 받았으면 재시도해도 안전
                raise
            return reader, writer, status, out_headers, pooled
        raise RuntimeError("local request retry failed")

    def release(self, reader, writer, pooled: bool, reusable: bool) -> None:
        """응답을 끝까지 읽었으면 반납하고, 아니면 버린다."""
        if not pooled or not reusable:
            self._close(writer, pooled)
            return
        self._idle.append((reader, writer, time.monotonic()))

    def _discard(self, reader, writer, pooled: bool) -> None:
        self._close(writer, pooled)

    def _close(self, writer, pooled: bool) -> None:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass
        if pooled:
            self._open = max(0, self._open - 1)

    def close(self) -> None:
        for entry in self._idle:
            writer = entry[1]
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._idle.clear()
        self._open = 0


async def _send_request(
    reader,
    writer,
    host: str,
    port: int,
    method: str,
    path: str,
    query: str,
    headers: dict,
    body: bytes,
) -> tuple[int, dict]:
    """연결에 요청을 쓰고 응답 헤더를 읽는다."""
    target = path or "/"
    if query:
        target += ("&" if "?" in target else "?") + query
    lines = ["%s %s HTTP/1.1" % (method, target), "Host: %s:%d" % (host, port)]
    for key, value in _filtered_headers(headers).items():
        if key.lower() in ("host", "connection", "content-length"):
            continue
        lines.append("%s: %s" % (key, value))
    lines.append("Content-Length: %d" % len(body or b""))
    try:
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b""))
        await writer.drain()
    except Exception as exc:
        # 유휴 연결이 서버에서 닫힌 경우가 대표적이다(요청 미전달 → 재시도 안전).
        raise LocalRequestNotSent(str(exc)[:120]) from exc

    blob = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=HEADER_TIMEOUT)
    status_line, _, rest = blob.partition(b"\r\n")
    parts = status_line.decode("latin-1").split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        # 상태줄이 깨졌다 = 이 연결은 신뢰할 수 없다(풀에서 버리고 오류로 알린다).
        raise ValueError("malformed status line: %r" % status_line[:60])
    status = int(parts[1])
    out_headers: dict[str, str] = {}
    for line in rest.decode("latin-1").split("\r\n"):
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out_headers[key.strip().lower()] = value.strip()
    return status, out_headers


async def _iter_body(reader, headers: dict, chunk_size: int = RESPONSE_CHUNK):
    """응답 본문을 도착하는 대로 내보낸다(chunked / content-length / EOF 종료 모두 지원)."""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                size = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                return
            if size <= 0:
                # 종료 청크: 트레일러 섹션(없으면 빈 줄 하나)까지 소비해야
                # 연결을 재사용할 때 다음 응답이 오염되지 않는다.
                while True:
                    trailer = await reader.readline()
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                return
            yield await reader.readexactly(size)
            await reader.readexactly(2)  # 청크 뒤 CRLF
        return

    length = headers.get("content-length", "")
    if length.isdigit():
        remaining = int(length)
        while remaining > 0:
            chunk = await reader.read(min(chunk_size, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk
        return

    while True:
        chunk = await reader.read(chunk_size)
        if not chunk:
            return
        yield chunk


_local_pools: dict[tuple[int, int], LocalPool] = {}


async def local_request(
    port: int,
    method: str,
    path: str,
    query: str = "",
    headers: dict | None = None,
    body: bytes = b"",
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> tuple[int, dict, bytes]:
    """로컬 앱(원격 리스너)으로 1홉 요청을 보내고 `(status, headers, body)` 를 돌려준다.

    연결 풀을 재사용하므로 요청마다 TCP 를 새로 열지 않는다. E2E 봉투 처리처럼 앱이 자기 자신의
    원격 리스너를 거쳐야 하는 경로에서 쓴다(미들웨어·스코프 검증을 그대로 통과시키기 위함).

    풀은 **(포트, 이벤트 루프)** 단위로 만든다 — 연결은 소속 루프에서만 쓸 수 있으므로
    서로 다른 루프(테스트의 TestClient 등)가 같은 연결을 재사용하면 안 된다.
    """
    key = (int(port), id(asyncio.get_running_loop()))
    pool = _local_pools.get(key)
    if pool is None:
        pool = _local_pools[key] = LocalPool("127.0.0.1", port)
    reader, writer, status, out_headers, pooled = await pool.request(
        method, path, query, headers or {}, body
    )
    payload = bytearray()
    reusable = False
    try:
        async for chunk in _iter_body(reader, out_headers):
            payload += chunk
            if len(payload) > max_bytes:
                break
        reusable = len(payload) <= max_bytes and _framing(out_headers) != "close"
    except Exception:
        reusable = False
        raise
    finally:
        pool.release(reader, writer, pooled, reusable)
    return status, out_headers, bytes(payload)


def close_local_pools() -> None:
    for pool in _local_pools.values():
        pool.close()
    _local_pools.clear()


class RelayAgent:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._ws = None
        self._stopping = False
        self._last_pong = time.time()
        self._config: dict = {}
        self._pool: LocalPool | None = None
        self._streams: dict[int, dict] = {}
        self._stats = {"reconnects": 0, "requests": 0, "errors": 0, "bytes_out": 0}
        self._state = {
            "transport": "none",
            "running": False,
            "url": "",
            "error": "",
            "since": None,
        }

    # --- 상태 -----------------------------------------------------------
    def state(self) -> dict:
        payload = dict(self._state)
        payload["streams"] = len(self._streams)
        payload["stats"] = dict(self._stats)
        return payload

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # --- 수명주기 -------------------------------------------------------
    async def start(
        self,
        *,
        relay_url: str,
        host_id: str,
        agent_token: str,
        target_port: int,
        label: str = "",
    ) -> None:
        await self.stop()
        self._stopping = False
        self._last_pong = time.time()
        self._config = {
            "url": agent_url(relay_url),
            "public": public_url(relay_url, host_id),
            "host_id": host_id,
            "agent_token": agent_token,
            "port": int(target_port),
            "label": label,
        }
        self._state.update(
            {
                "transport": "relay",
                "running": False,
                "url": self._config["public"],
                "error": "",
                "since": time.time(),
            }
        )
        if self._pool is not None:
            self._pool.close()
        self._pool = LocalPool("127.0.0.1", self._config["port"])
        self._task = asyncio.create_task(self._runner(), name="lafhvst-relay-agent")
        await asyncio.sleep(0)  # 첫 연결 시도를 시작시킨다(결과는 state() 로 관찰)

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 종료 경로
                pass
        await self._close_socket()
        if self._pool is not None:
            self._pool.close()
            self._pool = None
        self._streams.clear()
        self._state.update({"running": False})

    async def _close_socket(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    # --- 연결 루프 ------------------------------------------------------
    async def _runner(self) -> None:
        attempt = 0
        while not self._stopping:
            try:
                async with connect(
                    self._config["url"],
                    open_timeout=OPEN_TIMEOUT,
                    close_timeout=5,
                    max_size=FRAME_LIMIT,
                    ping_interval=None,
                ) as ws:
                    self._ws = ws
                    outcome = await self._handshake(ws)
                    if outcome == "fatal":
                        return
                    if outcome != "ok":
                        raise ConnectionError("handshake failed: " + str(outcome))
                    attempt = 0
                    self._state.update({"running": True, "error": ""})
                    await self._session(ws)
                    _debug(
                        "세션 종료 code=%s reason=%r" % (ws.close_code, ws.close_reason)
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 재연결로 흡수
                self._stats["errors"] += 1
                self._state.update({"running": False, "error": str(exc)[:200]})
                _debug("세션 오류 %r" % (exc,))
            finally:
                self._ws = None
                self._streams.clear()
                self._state.update({"running": False})
            if self._stopping:
                return
            delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
            attempt += 1
            self._stats["reconnects"] += 1
            await asyncio.sleep(delay + random.uniform(0, 0.5))

    async def _handshake(self, ws) -> str:
        await ws.send(
            encode_json(
                T_HELLO,
                0,
                {
                    "host_id": self._config["host_id"],
                    "agent_token": self._config["agent_token"],
                    "version": PROTOCOL_VERSION,
                    "label": self._config["label"],
                },
            )
        )
        message = await asyncio.wait_for(ws.recv(), timeout=OPEN_TIMEOUT)
        frame_type, _, payload = decode(message)
        if frame_type == T_HELLO_OK:
            return "ok"
        if frame_type == T_HELLO_ERR:
            data = decode_json(payload)
            code = str(data.get("code") or "unknown")
            message_text = str(data.get("message") or code)
            self._state.update({"running": False, "error": message_text[:200]})
            return "fatal" if code in FATAL_HELLO_CODES else "retry"
        return "retry"

    async def _session(self, ws) -> None:
        pinger = asyncio.create_task(self._ping_loop(ws), name="lafhvst-relay-ping")
        try:
            async for message in ws:
                frame_type, stream_id, payload = decode(message)
                await self._dispatch(ws, frame_type, stream_id, payload)
        finally:
            pinger.cancel()
            for entry in list(self._streams.values()):
                task = entry.get("task")
                if task is not None and not task.done():
                    task.cancel()

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            if time.time() - self._last_pong > PING_TIMEOUT:
                _debug("ping timeout(%.0fs 무응답) → 세션 종료" % (time.time() - self._last_pong))
                await ws.close(code=1001, reason="ping timeout")
                return
            try:
                await ws.send(encode(T_PING, 0))
            except Exception as exc:  # noqa: BLE001 - 종료 중
                _debug("PING 전송 실패 %r" % exc)
                return

    async def _dispatch(self, ws, frame_type: int, stream_id: int, payload: bytes) -> None:
        if frame_type == T_PING:
            await ws.send(encode(T_PONG, 0))
            return
        if frame_type == T_PONG:
            self._last_pong = time.time()
            return
        if frame_type == T_CANCEL:
            _debug("CANCEL stream=%d payload=%r" % (stream_id, payload[:120]))
            entry = self._streams.pop(stream_id, None)
            if entry and entry.get("task"):
                entry["task"].cancel()
            return
        if frame_type == T_REQ_HEAD:
            head = decode_json(payload)
            _debug("REQ_HEAD stream=%d %s %s" % (stream_id, head.get("method"), head.get("path")))
            self._streams[stream_id] = {"head": head, "body": bytearray(), "task": None}
            return
        if frame_type == T_REQ_BODY:
            entry = self._streams.get(stream_id)
            if entry is None:
                return
            entry["body"] += payload
            if len(entry["body"]) > MAX_REQUEST_BYTES:
                self._streams.pop(stream_id, None)
                await ws.send(encode_json(T_CANCEL, stream_id, {"reason": "too_large"}))
                await self._send_error(ws, stream_id, 413, "request body too large")
            return
        if frame_type == T_REQ_END:
            entry = self._streams.get(stream_id)
            if entry is None:
                return
            entry["task"] = asyncio.create_task(self._handle_stream(ws, stream_id, entry))
            return

    async def _send_error(self, ws, stream_id: int, status: int, text: str) -> None:
        _debug("SEND_ERROR stream=%d status=%d %s" % (stream_id, status, text[:80]))
        body = text.encode("utf-8")
        try:
            await ws.send(
                encode_json(
                    T_RES_HEAD,
                    stream_id,
                    {
                        "status": status,
                        "headers": {
                            "content-type": "text/plain; charset=utf-8",
                            "content-length": str(len(body)),
                        },
                    },
                )
            )
            await ws.send(encode(T_RES_BODY, stream_id, body))
            await ws.send(encode(T_RES_END, stream_id))
        except Exception as exc:  # noqa: BLE001 - 세션 종료 중이면 보낼 수 없다
            _debug("SEND_ERROR 실패 stream=%d %r" % (stream_id, exc))

    async def _handle_stream(self, ws, stream_id: int, entry: dict) -> None:
        """로컬 응답을 **도착하는 대로** 릴레이에 흘려보낸다.

        - `text/event-stream`(SSE) 등 스트리밍 응답은 크기 상한 없이 계속 전달한다.
        - 그 외 응답은 `MAX_RESPONSE_BYTES` 를 넘으면 잘라내고 스트림을 취소한다.
        """
        head = entry["head"]
        body = bytes(entry["body"])
        writer = None
        pooled = False
        reusable = False
        pool = self._pool  # stop() 이 None 으로 만들 수 있으므로 지역 참조를 쓴다
        try:
            if pool is None:
                raise RuntimeError("릴레이 에이전트가 중지되었습니다")
            reader, writer, status, headers, pooled = await asyncio.wait_for(
                pool.request(
                    str(head.get("method") or "GET").upper(),
                    str(head.get("path") or "/"),
                    str(head.get("query") or ""),
                    head.get("headers") or {},
                    body,
                ),
                timeout=HEADER_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 앱 도달 실패는 502
            self._stats["errors"] += 1
            await self._send_error(ws, stream_id, 502, "local request failed: " + str(exc)[:120])
            return
        finally:
            self._streams.pop(stream_id, None)

        framing = _framing(headers)
        streaming = any(
            token in (headers.get("content-type") or "") for token in STREAM_CONTENT_TYPES
        )
        total = 0
        truncated = False
        try:
            _debug("RES_HEAD stream=%d status=%d bytes=%d" % (stream_id, status, total))
            await ws.send(
                encode_json(T_RES_HEAD, stream_id, {"status": status, "headers": headers})
            )
            async for chunk in _iter_body(reader, headers):
                if not chunk:
                    continue
                total += len(chunk)
                if not streaming and total > MAX_RESPONSE_BYTES:
                    truncated = True
                    break
                await ws.send(encode(T_RES_BODY, stream_id, chunk))
            # 본문을 끝까지 읽었고 close-delimited 가 아니면 연결을 재사용할 수 있다.
            reusable = not truncated and framing != "close"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 전송 중 끊김(타임아웃 포함)
            self._stats["errors"] += 1
            self._state["error"] = "stream aborted: " + str(exc)[:120]
        finally:
            if writer is not None and pool is not None:
                pool.release(reader, writer, pooled, reusable)

        self._stats["requests"] += 1
        self._stats["bytes_out"] += total
        try:
            if truncated:
                await ws.send(encode_json(T_CANCEL, stream_id, {"reason": "response_too_large"}))
                return
            await ws.send(encode(T_RES_END, stream_id))
        except Exception as exc:  # noqa: BLE001 - 세션 종료 중이면 보낼 수 없다
            _debug("RES_END 실패 stream=%d %r" % (stream_id, exc))


tunnel = RelayAgent()
