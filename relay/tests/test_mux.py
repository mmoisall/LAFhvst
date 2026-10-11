"""멀티플렉서 단위 테스트 — 프레이밍, stream_id 규칙, 동시 스트림 상한, 백프레셔."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from relay import mux

HOST = "t" * 16


class StubSocket:
    """`AgentSession` 이 쓰는 부분만 흉내내는 소켓."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.closed: tuple[int, str] | None = None

    async def send_bytes(self, data: bytes) -> None:
        self.frames.append(data)

    async def receive(self) -> dict[str, Any]:  # pragma: no cover — 사용되지 않는다
        raise AssertionError("receive 는 쓰이지 않는다")

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason or "")


def _session(**kwargs: Any) -> mux.AgentSession:
    return mux.AgentSession(StubSocket(), HOST, **kwargs)


# ── 프레이밍 ────────────────────────────────────────────────────────────
def test_frame_roundtrip():
    raw = mux.encode_frame(mux.TYPE_RES_BODY, 7, b"payload")
    assert raw[0] == mux.TYPE_RES_BODY == 0x21
    assert raw[1:5] == b"\x00\x00\x00\x07"  # big-endian uint32
    assert mux.decode_frame(raw) == (mux.TYPE_RES_BODY, 7, b"payload")
    assert mux.decode_frame(mux.encode_frame(mux.TYPE_PING)) == (mux.TYPE_PING, 0, b"")


def test_short_frame_is_protocol_error():
    with pytest.raises(mux.ProtocolError):
        mux.decode_frame(b"\x01\x00\x00")


def test_json_helpers():
    assert mux.loads(mux.dumps({"a": "한글"})) == {"a": "한글"}
    with pytest.raises(mux.ProtocolError):
        mux.loads(b"not json")
    with pytest.raises(mux.ProtocolError):
        mux.loads(b"[1,2]")


# ── stream_id 규칙 ──────────────────────────────────────────────────────
def test_stream_ids_monotonic_and_never_reused():
    session = _session()
    first = session.open_stream()
    second = session.open_stream()
    assert (first.id, second.id) == (1, 2)  # 0 은 세션 제어 전용
    session.close_stream(second.id)
    third = session.open_stream()
    assert third.id == 3  # 재사용 금지
    assert session.stream_count == 2


def test_stream_limit(monkeypatch):
    monkeypatch.setattr(mux, "MAX_STREAMS", 2)
    session = _session()
    session.open_stream()
    session.open_stream()
    with pytest.raises(mux.TooManyStreams):
        session.open_stream()


# ── 백프레셔 ────────────────────────────────────────────────────────────
def test_backpressure_cancels_only_that_stream(monkeypatch):
    """큐 상한 초과 시 해당 스트림만 CANCEL 되고 세션은 유지된다."""
    monkeypatch.setattr(mux, "QUEUE_MAX", 1)

    async def scenario() -> tuple[mux.AgentSession, mux.Stream, mux.Stream]:
        session = _session()
        stream = session.open_stream()
        other = session.open_stream()
        await session._dispatch(mux.TYPE_RES_BODY, stream.id, b"a")  # 큐가 가득 찬다
        await session._dispatch(mux.TYPE_RES_BODY, stream.id, b"b")  # 초과 → 취소
        await asyncio.sleep(0.05)  # 취소 통지 태스크 실행
        return session, stream, other

    session, stream, other = asyncio.run(scenario())
    assert stream.cancel_reason == "backpressure"
    assert other.cancel_reason is None
    assert session.closed is False  # 세션은 유지
    assert session.streams.get(stream.id) is stream
    frames = session.websocket.frames
    kinds = [mux.decode_frame(frame)[0] for frame in frames]
    assert mux.TYPE_CANCEL in kinds
    reason = mux.loads(mux.decode_frame(frames[kinds.index(mux.TYPE_CANCEL)])[2])
    assert reason["reason"] == "backpressure"
    assert stream.queue.get_nowait()[0] == "cancel"


# ── 취소 사유 → 상태 코드 (protocol.md §5/§6) ───────────────────────────
def test_cancel_reason_status_mapping():
    assert mux.status_for_reason("too_large") == 413
    assert mux.status_for_reason("no_progress") == 504
    assert mux.status_for_reason("header_timeout") == 504
    assert mux.status_for_reason("timeout") == 504
    assert mux.status_for_reason("backpressure") == 502
    assert mux.status_for_reason("client_gone") == 499
    assert mux.status_for_reason("replaced") == 502
    assert mux.status_for_reason(None) == 502
