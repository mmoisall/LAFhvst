"""SSE 이벤트 스트림(M6) 테스트.

`/api/events` 는 대시보드 변화와 감시 이벤트를 한 연결로 밀어준다.
- 접근 제어는 다른 원격 API 와 동일(원격은 기기 토큰 필요).
- 하트비트 간격은 릴레이/프록시의 무진행 타임아웃(60초)보다 짧아야 한다.
"""

import asyncio
import json
import types

import pytest

import server
from core import browser_watcher, remote_access

from conftest import pair_device

from test_remote_access import auth


class StubRequest:
    """`api_events` 가 요구하는 최소 Request 대역."""

    def __init__(self, device=None):
        self.state = types.SimpleNamespace(remote_device=device)
        self.scope = {"type": "http"}

    async def is_disconnected(self) -> bool:
        return False


def collect_frames(count: int, timeout: float = 12.0):
    """`api_events` 제너레이터에서 SSE 프레임을 count 개 모은다(이벤트 단위)."""

    async def scenario():
        response = await server.api_events(StubRequest())
        iterator = response.body_iterator
        frames = []
        try:
            while len(frames) < count:
                chunk = await iterator.__anext__()
                frames.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
        except (StopAsyncIteration, asyncio.TimeoutError):
            pass
        finally:
            await iterator.aclose()
        return frames

    try:
        return asyncio.run(asyncio.wait_for(scenario(), timeout=timeout))
    except asyncio.TimeoutError:
        return []


async def _open_stream(device=None):
    return await server.api_events(StubRequest(device))


def test_stream_starts_with_connected_comment():
    frames = collect_frames(1)
    assert frames[0] == ": connected\n\n"


def test_stream_emits_dashboard_event():
    frames = collect_frames(2)
    dashboard = [frame for frame in frames if frame.startswith("event: dashboard")]
    assert dashboard, frames
    payload = json.loads(dashboard[0].split("data: ", 1)[1].strip())
    assert "queue" in payload and "scheduler" in payload


def test_stream_forwards_watcher_events():
    browser_watcher.watcher._add_event(kind="test", message="sse 확인")
    frames = collect_frames(3)
    watcher = [frame for frame in frames if frame.startswith("event: watcher")]
    assert watcher, frames
    payload = json.loads(watcher[0].split("data: ", 1)[1].strip())
    assert payload["message"] == "sse 확인"


def test_stream_heartbeat_is_shorter_than_relay_idle_timeout():
    # 릴레이의 스트림 무진행 타임아웃(60초)보다 짧아야 연결이 유지된다.
    assert server.SSE_HEARTBEAT_SECONDS < 60
    assert server.SSE_INTERVAL_SECONDS <= server.SSE_HEARTBEAT_SECONDS


def test_stream_rejects_too_many_clients(monkeypatch):
    monkeypatch.setattr(server, "_sse_clients", server.SSE_MAX_CLIENTS)
    with pytest.raises(Exception) as excinfo:
        asyncio.run(_open_stream())
    assert getattr(excinfo.value, "status_code", None) == 503


def test_events_requires_token_for_remote(local_client, remote_client):
    """원격에서 토큰 없이는 401 (실제 스트림 수신은 릴레이 스트리밍 테스트에서 검증)."""
    pair_device(local_client, remote_client)
    assert remote_client.get("/api/events").status_code == 401


def test_client_counter_returns_to_zero():
    assert server._sse_clients == 0
    frames = collect_frames(1)
    assert frames
    assert server._sse_clients == 0
