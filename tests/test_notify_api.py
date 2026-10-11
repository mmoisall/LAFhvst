"""알림(웹훅) 연동 테스트(M6).

- `/api/notify/*` 엔드포인트(상태/테스트 전송, 원격 스코프).
- 수집 완료/실패가 실제로 웹훅을 호출하는지(`_collect_source` 배선).
로컬 수신기(ThreadingHTTPServer)만 사용한다 — 외부 네트워크 없음.

주의(환경): 이 Windows 환경의 loopback 은 새 연결을 반복해 열 때 간헐 RST 를 낸다
(`tests/test_notifier.py` 헤더 참고). 웹훅 수신 횟수를 단언하는 테스트는 드물게 실패할 수 있다.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import server
from core import gdl_executor, remote_access, utils

from conftest import pair_device

from test_remote_access import auth
from test_remote_stop import make_source, stub_request


class Receiver:
    """웹훅 수신기(요청 기록)."""

    def __init__(self):
        self.requests = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                receiver.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                self.send_response(200)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):  # 조용히
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/hook" % self.port

    def payloads(self) -> list[dict]:
        out = []
        for entry in self.requests:
            try:
                out.append(json.loads(entry["body"].decode("utf-8")))
            except ValueError:
                out.append({})
        return out

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def receiver():
    instance = Receiver()
    yield instance
    instance.close()


@pytest.fixture(autouse=True)
def _notify_defaults():
    server.SETTINGS.update(
        {
            "notifyWebhookUrl": "",
            "notifyPreset": "json",
            "notifyOnSuccess": True,
            "notifyOnError": True,
            "notifyMinIntervalSec": 0,
        }
    )
    yield


# --- 엔드포인트 ----------------------------------------------------------
def test_notify_status_shape(local_client):
    payload = local_client.get("/api/notify/status").json()
    assert payload["enabled"] is False
    assert payload["preset"] == "json"
    assert "url_host" in payload


def test_notify_test_sends_to_webhook(local_client, receiver):
    server.SETTINGS["notifyWebhookUrl"] = receiver.url
    response = local_client.post("/api/notify/test", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["status"] == 200, body
    assert len(receiver.requests) == 1
    payload = receiver.payloads()[0]
    assert payload["app"] == "LAFhvst"
    assert payload["message"], payload


def test_notify_test_without_url_reports_error(local_client):
    body = local_client.post("/api/notify/test", json={}).json()
    assert body["ok"] is False
    assert body["error"] == "no-url"


def test_notify_test_from_remote_device(local_client, remote_client, receiver):
    server.SETTINGS["notifyWebhookUrl"] = receiver.url
    claimed = pair_device(local_client, remote_client)
    token = claimed["device_token"]
    assert remote_client.post("/api/notify/test", json={}).status_code == 401
    response = remote_client.post("/api/notify/test", json={}, headers=auth(token))
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True


def test_notify_status_is_readable_by_remote_device(local_client, remote_client):
    claimed = pair_device(local_client, remote_client)
    response = remote_client.get("/api/notify/status", headers=auth(claimed["device_token"]))
    assert response.status_code == 200


# --- 수집 배선 ----------------------------------------------------------
@pytest.fixture
def fake_collect(monkeypatch, tmp_path):
    """gallery-dl 실행을 대체하는 가짜 수집(결과를 테스트가 지정)."""
    result = {"ok": True, "usage_delta": 1, "errors": [], "attempts": 1}

    async def collect(*args, **kwargs):
        return dict(result)

    monkeypatch.setattr(gdl_executor, "collect_with_failover", collect)
    monkeypatch.setattr(
        utils, "build_download_directory", lambda base, source: str(tmp_path / "download")
    )
    monkeypatch.setitem(server.SETTINGS, "recommendAutoIndex", False)
    return result


def test_collection_success_notifies(local_client, receiver, fake_collect):
    server.SETTINGS["notifyWebhookUrl"] = receiver.url
    source_id = make_source(local_client, name="알림 성공")
    result = asyncio.run(server._collect_source(source_id))
    assert result["status"] == "done"
    payloads = receiver.payloads()
    assert payloads, "성공 알림이 전송되지 않았습니다"
    assert payloads[0]["event"] == "success"
    assert "알림 성공" in payloads[0]["title"]
    assert payloads[0]["source_id"] == source_id


def test_collection_failure_notifies(local_client, receiver, fake_collect):
    server.SETTINGS["notifyWebhookUrl"] = receiver.url
    fake_collect.update(
        {"ok": False, "usage_delta": 0, "errors": [{"code": "404", "message": "not found"}]}
    )
    source_id = make_source(local_client, name="알림 실패")
    result = asyncio.run(server._collect_source(source_id))
    assert result["status"] == "error"
    payloads = receiver.payloads()
    assert payloads, "실패 알림이 전송되지 않았습니다"
    assert payloads[0]["event"] == "error"
    assert "알림 실패" in payloads[0]["title"]


def test_no_notification_when_url_is_empty(local_client, receiver, fake_collect):
    server.SETTINGS["notifyWebhookUrl"] = ""
    source_id = make_source(local_client, name="알림 없음")
    asyncio.run(server._collect_source(source_id))
    assert receiver.requests == []


def test_success_notification_can_be_disabled(local_client, receiver, fake_collect):
    server.SETTINGS["notifyWebhookUrl"] = receiver.url
    server.SETTINGS["notifyOnSuccess"] = False
    source_id = make_source(local_client, name="성공 알림 끔")
    asyncio.run(server._collect_source(source_id))
    assert receiver.requests == []


def test_cancelled_collection_does_not_notify(local_client, receiver, monkeypatch, tmp_path):
    async def blocked(*args, **kwargs):
        token = kwargs.get("cancel_token")
        while token is not None and not token.cancelled:
            await asyncio.sleep(0.05)
        return {"ok": False, "cancelled": True, "errors": [], "usage_delta": 0}

    monkeypatch.setattr(gdl_executor, "collect_with_failover", blocked)
    monkeypatch.setattr(
        utils, "build_download_directory", lambda base, source: str(tmp_path / "download")
    )
    server.SETTINGS["notifyWebhookUrl"] = receiver.url
    source_id = make_source(local_client, name="취소 알림 없음")

    async def scenario():
        task = asyncio.create_task(server._collect_source(source_id))
        for _ in range(300):
            if source_id in server._running and server._cancel_tokens.get(source_id) is not None:
                break
            await asyncio.sleep(0.02)
        server.stop_source(source_id, stub_request())
        return await asyncio.wait_for(task, timeout=15)

    result = asyncio.run(scenario())
    assert result["status"] == "cancelled"
    assert receiver.requests == []
