"""원격 정지(M6) 테스트.

- `CancelToken` 은 실제 프로세스를 종료해야 한다.
- 정지 요청은 실행 중 수집을 취소하고, **실패로 기록하지 않는다**(INFO 로그만).
- 정지 엔드포인트는 control 스코프로 원격 기기에서도 허용된다.
"""

import asyncio
import sys
import time
import types

import pytest

import server
from core import gdl_executor, remote_access, utils

from conftest import enable_remote, pair_device

from test_remote_access import auth


# --- CancelToken --------------------------------------------------------
def test_cancel_token_kills_process():
    async def scenario():
        process = await gdl_executor.start_process(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        token = gdl_executor.CancelToken()
        token.bind(process)
        assert token.cancelled is False
        started = time.monotonic()
        assert token.cancel() is True
        await asyncio.wait_for(process.wait(), timeout=10)
        elapsed = time.monotonic() - started
        assert process.returncode != 0
        assert elapsed < 5, "취소가 즉시 반영되지 않았습니다: %.2fs" % elapsed
        assert token.cancel() is False  # 중복 취소는 False
        assert token.cancelled is True

    asyncio.run(scenario())


def test_cancel_token_bind_after_cancel_kills_immediately():
    async def scenario():
        token = gdl_executor.CancelToken()
        token.cancel()
        process = await gdl_executor.start_process(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        token.bind(process)  # 이미 취소된 상태로 등록
        await asyncio.wait_for(process.wait(), timeout=10)
        assert process.returncode != 0

    asyncio.run(scenario())


# --- 정지 엔드포인트 -----------------------------------------------------
@pytest.fixture
def blocked_collect(monkeypatch, tmp_path):
    """gallery-dl 실행을 취소 신호만 기다리는 가짜 수집으로 대체한다."""
    calls = []

    async def fake_collect(*args, **kwargs):
        token = kwargs.get("cancel_token")
        calls.append(token)
        while token is not None and not token.cancelled:
            await asyncio.sleep(0.05)
        return {
            "ok": False,
            "cancelled": True,
            "returncode": -9,
            "errors": [],
            "usage_delta": 0,
        }

    monkeypatch.setattr(gdl_executor, "collect_with_failover", fake_collect)
    monkeypatch.setattr(
        utils, "build_download_directory", lambda base, source: str(tmp_path / "download")
    )
    return calls


def stub_request(device=None):
    """`stop_source` 가 요구하는 최소 Request 대역(로컬 또는 지정 기기)."""
    return types.SimpleNamespace(state=types.SimpleNamespace(remote_device=device))


def make_source(client, name="정지 테스트") -> int:
    folder = client.post("/api/folders", json={"name": "취소폴더"})
    assert folder.status_code == 201, folder.text
    source = client.post(
        "/api/sources",
        json={
            "name": name,
            "url": "https://example.com/user",
            "site": "example",
            "key": "user",
            "folder_id": folder.json()["id"],
        },
    )
    assert source.status_code == 201, source.text
    return source.json()["id"]


def wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def test_stop_running_collection(local_client, blocked_collect):
    """정지 요청이 실행 중 수집을 취소하고 실패로 기록하지 않는다.

    (TestClient 는 컨텍스트 없이 쓰면 요청마다 새 이벤트 루프를 만들므로,
     백그라운드 수집이 필요한 부분은 같은 루프에서 직접 구동한다.)
    """
    source_id = make_source(local_client)

    async def scenario():
        task = asyncio.create_task(server._collect_source(source_id))
        for _ in range(300):
            if source_id in server._running and blocked_collect:
                break
            await asyncio.sleep(0.02)
        assert source_id in server._running, "수집이 시작되지 않았습니다"
        assert blocked_collect and blocked_collect[0] is not None

        # 취소 직후에는 '정지 중'으로 표시된다(아직 태스크가 정리되기 전).
        assert server.stop_source(source_id, stub_request())["ok"] is True
        assert server.running_sources()["stopping"] == [source_id]
        queue = {item["id"]: item for item in server._build_dashboard()["queue"]}
        assert queue[source_id]["stopping"] is True

        result = await asyncio.wait_for(task, timeout=15)
        assert result["status"] == "cancelled"
        assert source_id not in server._running
        assert source_id not in server._cancel_tokens
        assert server.running_sources()["stopping"] == []

    asyncio.run(scenario())

    # 취소는 실패가 아니다: ERROR/WARN 이 남지 않아야 한다.
    logs = local_client.get("/api/logs?source_id=%d" % source_id).json()
    entries = logs.get("items", []) if isinstance(logs, dict) else (logs or [])
    assert not [row for row in entries if row.get("level") in ("ERROR", "WARN")], entries

    # 취소 INFO 로그는 남는다(같은 문구는 중복 병합되므로 전체 검색으로 확인).
    cancel_logs = local_client.get("/api/logs?q=%EC%B7%A8%EC%86%8C").json()
    cancel_entries = cancel_logs.get("items", []) if isinstance(cancel_logs, dict) else []
    assert any("취소" in (row.get("message") or "") for row in cancel_entries), cancel_entries

    source = local_client.get("/api/sources/%d" % source_id).json()
    assert source.get("status") != "error"


def test_stop_idle_source_reports_not_running(local_client):
    source_id = make_source(local_client, name="대기 소스")
    response = local_client.post("/api/sources/%d/stop" % source_id, json={})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "not_running", "source_id": source_id}


def test_stop_from_remote_device_uses_control_scope(local_client, remote_client):
    source_id = make_source(local_client, name="원격 정지")
    claimed = pair_device(local_client, remote_client)
    token = claimed["device_token"]

    # 토큰 없이는 거부, control 스코프 기기에는 허용(실행 중이 아니므로 ok=False)
    assert remote_client.post("/api/sources/%d/stop" % source_id, json={}).status_code == 401
    response = remote_client.post(
        "/api/sources/%d/stop" % source_id, json={}, headers=auth(token)
    )
    assert response.status_code == 200, response.text
    assert response.json()["reason"] == "not_running"


def test_stop_is_not_manage_scope(local_client, remote_client):
    """정지는 파괴적 작업이 아니므로 `remoteAllowDestructive` 없이도 허용된다."""
    source_id = make_source(local_client, name="스코프 확인")
    claimed = pair_device(local_client, remote_client)
    token = claimed["device_token"]
    server.SETTINGS["remoteAllowDestructive"] = False
    response = remote_client.post(
        "/api/sources/%d/stop" % source_id, json={}, headers=auth(token)
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "not_running"


def test_cancel_token_is_cleared_when_collection_fails(local_client, monkeypatch, tmp_path):
    """수집이 예외로 끝나도 취소 토큰/실행 표시가 남지 않는다."""

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(gdl_executor, "collect_with_failover", boom)
    monkeypatch.setattr(
        utils, "build_download_directory", lambda base, source: str(tmp_path / "download")
    )
    source_id = make_source(local_client, name="예외 소스")

    async def scenario():
        result = await server._collect_source(source_id)
        assert result["status"] == "error"
        assert source_id not in server._running
        assert source_id not in server._cancel_tokens

    asyncio.run(scenario())
