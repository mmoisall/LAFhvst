"""테스트용 가짜 에이전트(에코 백엔드).

`websockets` 동기 클라이언트로 실제 relayd 의 `/agent` 에 접속해 HELLO 를 보내고,
REQ_HEAD/REQ_BODY/REQ_END 를 받아 RES_HEAD/RES_BODY/RES_END 로 응답한다.
프레임 인코딩은 릴레이 구현을 import 하지 않고 여기서 독립적으로 구현한다
(와이어 포맷이 정말 계약대로인지 검증하기 위함).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

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

Response = dict[str, Any]


def encode(frame_type: int, stream_id: int = 0, payload: bytes = b"") -> bytes:
    return bytes((frame_type,)) + stream_id.to_bytes(4, "big") + payload


def decode(raw: bytes) -> tuple[int, int, bytes]:
    assert len(raw) >= 5, f"frame too short: {raw!r}"
    return raw[0], int.from_bytes(raw[1:5], "big"), raw[5:]


def echo(req: dict[str, Any]) -> Response:
    """기본 정책 — 본문을 그대로 되돌리고 경로/쿼리를 헤더로 노출한다."""
    body = req["body"] or req["path"].encode("utf-8")
    return {
        "status": 200,
        "headers": {
            "content-type": "text/plain; charset=utf-8",
            "x-laf-path": req["path"],
            "x-laf-query": req["query"],
        },
        "body": body,
    }


def silent(req: dict[str, Any]) -> None:
    """아무 응답도 보내지 않는다(무진행/헤더 대기 테스트)."""
    return None


class FakeAgent(threading.Thread):
    """`/agent` 에 붙는 가짜 PC 에이전트.

    - `responder`: 요청 dict → `{status, headers, body}` 또는 None(응답 없음).
    - `hold`: 설정 전까지 응답을 미루는 `threading.Event`(동시 스트림 상한 테스트).
    """

    def __init__(
        self,
        ws_base: str,
        host_id: str,
        token: str,
        *,
        version: str = "1",
        label: str = "test-agent",
        responder: Callable[[dict[str, Any]], Response | None] | None = None,
        hold: threading.Event | None = None,
        body_chunk: int = 64 * 1024,
        answer_pings: bool = True,
    ) -> None:
        super().__init__(daemon=True)
        self.url = f"{ws_base}/agent"
        self.host_id = host_id
        self.token = token
        self.version = version
        self.label = label
        self.responder = responder or echo
        self.hold = hold
        self.body_chunk = body_chunk
        self.answer_pings = answer_pings
        self.requests: list[dict[str, Any]] = []
        self.pending_heads: list[dict[str, Any]] = []
        self.cancels: list[tuple[int, str]] = []
        self.hello_ok: dict[str, Any] | None = None
        self.hello_err: dict[str, Any] | None = None
        self.close_code: int | None = None
        self.close_reason: str = ""
        self.error: str | None = None
        self._conn: Any = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()

    # ── 외부 대기 헬퍼 ──────────────────────────────────────────────────
    def wait_hello(self, timeout: float = 10.0) -> bool:
        return self._wait_until(lambda: self.hello_ok is not None or self.hello_err is not None, timeout)

    def wait_requests(self, count: int, timeout: float = 10.0) -> bool:
        return self._wait_until(lambda: len(self.requests) >= count, timeout)

    def wait_cancel(self, count: int = 1, timeout: float = 10.0) -> bool:
        return self._wait_until(lambda: len(self.cancels) >= count, timeout)

    def wait_closed(self, timeout: float = 10.0) -> bool:
        return self._wait_until(lambda: self.close_code is not None, timeout)

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass

    def _wait_until(self, predicate: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    # ── 송신 ────────────────────────────────────────────────────────────
    def send(self, frame_type: int, stream_id: int = 0, payload: bytes = b"") -> None:
        with self._send_lock:
            self._conn.send(encode(frame_type, stream_id, payload))

    def send_json(self, frame_type: int, stream_id: int, payload: dict[str, Any]) -> None:
        self.send(frame_type, stream_id, json.dumps(payload).encode("utf-8"))

    # ── 스레드 본체 ─────────────────────────────────────────────────────
    def run(self) -> None:
        try:
            with ws_connect(self.url, open_timeout=10, close_timeout=5, max_size=None) as conn:
                self._conn = conn
                self.send_json(
                    TYPE_HELLO,
                    0,
                    {
                        "host_id": self.host_id,
                        "agent_token": self.token,
                        "version": self.version,
                        "label": self.label,
                    },
                )
                if not self._handshake():
                    return
                self._loop()
        except ConnectionClosed as exc:
            self._record_close(exc)
        except Exception as exc:  # pragma: no cover — 테스트 실패 진단용
            if not self._stop.is_set():
                self.error = repr(exc)

    def _handshake(self) -> bool:
        while not self._stop.is_set():
            try:
                raw = self._conn.recv(timeout=10)
            except TimeoutError:
                self.error = "HELLO 응답 없음"
                return False
            frame_type, _sid, payload = decode(raw)
            if frame_type == TYPE_HELLO_OK:
                self.hello_ok = json.loads(payload)
                return True
            if frame_type == TYPE_HELLO_ERR:
                self.hello_err = json.loads(payload)
                return False
            if frame_type == TYPE_PING:
                self.send(TYPE_PONG)
        return False

    def _loop(self) -> None:
        pending: dict[int, dict[str, Any]] = {}
        while not self._stop.is_set():
            try:
                raw = self._conn.recv(timeout=0.2)
            except TimeoutError:
                continue
            frame_type, stream_id, payload = decode(raw)
            if frame_type == TYPE_PING:
                if self.answer_pings:
                    self.send(TYPE_PONG)
                continue
            if frame_type == TYPE_PONG:
                continue
            if frame_type == TYPE_CANCEL:
                reason = ""
                try:
                    reason = str(json.loads(payload).get("reason") or "")
                except ValueError:
                    pass
                self.cancels.append((stream_id, reason))
                pending.pop(stream_id, None)
                continue
            if frame_type == TYPE_REQ_HEAD:
                request = json.loads(payload)
                request["stream"] = stream_id
                request["body"] = b""
                pending[stream_id] = request
                self.pending_heads.append(request)
                continue
            if frame_type == TYPE_REQ_BODY:
                if stream_id in pending:
                    pending[stream_id]["body"] += payload
                continue
            if frame_type == TYPE_REQ_END:
                request = pending.pop(stream_id, None)
                if request is None:
                    continue
                self.requests.append(request)
                threading.Thread(
                    target=self._respond, args=(stream_id, request), daemon=True
                ).start()

    def _respond(self, stream_id: int, request: dict[str, Any]) -> None:
        try:
            if self.hold is not None and not self.hold.wait(timeout=30):
                return
            if self._cancelled(stream_id):
                return
            result = self.responder(request)
            if result is None or self._cancelled(stream_id):
                return
            headers = dict(result.get("headers") or {})
            body: bytes = result.get("body") or b""
            self.send_json(TYPE_RES_HEAD, stream_id, {"status": result["status"], "headers": headers})
            chunk = max(1, self.body_chunk)
            for offset in range(0, len(body), chunk):
                self.send(TYPE_RES_BODY, stream_id, body[offset : offset + chunk])
            self.send(TYPE_RES_END, stream_id)
        except (ConnectionClosed, OSError):
            pass  # 세션이 닫혔다.

    def _cancelled(self, stream_id: int) -> bool:
        return any(sid == stream_id for sid, _ in self.cancels)

    def _record_close(self, exc: ConnectionClosed) -> None:
        frame = exc.rcvd
        if frame is not None:
            self.close_code = frame.code
            self.close_reason = frame.reason
