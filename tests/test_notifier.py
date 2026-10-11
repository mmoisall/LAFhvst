"""`core.notifier` 웹훅 테스트.

외부 네트워크를 쓰지 않는다. 모든 전송은 로컬 `http.server.ThreadingHTTPServer` 수신기로만 검증한다.

주의(환경): 검증에 사용한 Windows 환경에서는 loopback 에 새 연결을 반복해 열 때 간헐적으로
RST(WinError 10054/10022)가 발생한다(수정 전 베이스라인 앱에서도 동일하게 관측). 따라서 전송
성공(`ok`)이나 수신 횟수를 정확히 단언하는 테스트가 **가끔** 실패할 수 있다 — 실패하면 먼저
재실행해 보라. 제품 코드는 의도적으로 재시도하지 않는다: 응답을 못 받은 경우에도 수신 측이 이미
처리했을 수 있어 재전송하면 알림이 중복되기 때문이다(외부 웹훅은 멱등 키가 없다).
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import notifier


# ---------------------------------------------------------------------------
# 로컬 수신기
# ---------------------------------------------------------------------------


class _Receiver:
    """POST 본문을 그대로 기록하는 로컬 HTTP 수신기."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.1 + Content-Length 로 응답해 연결을 서버가 먼저 끊지 않게 한다.
            # (HTTP/1.0 기본값은 응답 직후 close → 클라이언트 read 와 경합해 간헐 RST 발생)
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler 규약)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with outer.lock:
                    outer.requests.append(
                        {"body": body, "headers": self.headers, "path": self.path}
                    )
                self.send_response(outer.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):  # 테스트 출력 오염 방지
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> "_Receiver":
        self.thread.start()
        self.port = self.server.server_address[1]
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook"

    @property
    def count(self) -> int:
        with self.lock:
            return len(self.requests)

    def last(self) -> dict:
        with self.lock:
            return self.requests[-1]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def receiver():
    recv = _Receiver().start()
    try:
        yield recv
    finally:
        recv.stop()


@pytest.fixture(autouse=True)
def _reset_throttle():
    notifier._last_sent.clear()
    yield
    notifier._last_sent.clear()


def _settings(url: str, **overrides) -> dict:
    cfg = {
        "notifyWebhookUrl": url,
        "notifyPreset": "json",
        "notifyOnSuccess": True,
        "notifyOnError": True,
        "notifyMinIntervalSec": 0,
    }
    cfg.update(overrides)
    return cfg


def _closed_port() -> int:
    """열려 있지 않은(=연결 실패할) 로컬 포트를 얻는다."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# payload_for
# ---------------------------------------------------------------------------


def test_payload_for_json():
    body, ctype = notifier.payload_for(
        "json", title="제목", message="본문", event="success",
        fields={"source_id": 7, "profile_id": 2},
    )
    data = json.loads(body)
    assert data["event"] == "success"
    assert data["title"] == "제목"
    assert data["message"] == "본문"
    assert data["source_id"] == 7
    assert data["profile_id"] == 2
    assert data["app"] == "LAFhvst"
    assert ctype == "application/json"


def test_payload_for_text_and_ntfy():
    for preset in ("text", "ntfy"):
        body, ctype = notifier.payload_for(
            preset, title="제목", message="본문", event="error"
        )
        assert body == "본문".encode("utf-8")
        assert ctype == "text/plain; charset=utf-8"


def test_payload_for_chat_presets():
    expected_key = {"discord": "content", "slack": "text", "telegram": "text"}
    for preset, key in expected_key.items():
        body, ctype = notifier.payload_for(
            preset, title="제목", message="본문", event="success"
        )
        assert json.loads(body)[key] == "제목\n본문"
        assert ctype == "application/json"


def test_payload_for_unknown_falls_back_to_json():
    body, ctype = notifier.payload_for(
        "없는프리셋", title="t", message="m", event="info"
    )
    assert json.loads(body)["app"] == "LAFhvst"
    assert ctype == "application/json"


def test_presets_constant():
    assert notifier.PRESETS == ("json", "text", "ntfy", "discord", "slack", "telegram")


# ---------------------------------------------------------------------------
# notify: 전송/스킵
# ---------------------------------------------------------------------------


def test_notify_posts_preset_body_to_receiver(receiver):
    cfg = _settings(receiver.url, notifyPreset="discord")
    res = notifier.notify("success", title="제목", message="본문", source_id=3, settings=cfg)
    assert res["ok"] is True, res
    assert res["status"] == 200
    assert res["skipped"] is None

    req = receiver.last()
    assert json.loads(req["body"])["content"] == "제목\n본문"
    assert req["headers"]["Content-Type"].startswith("application/json")
    assert req["headers"]["User-Agent"].startswith("LAFhvst/")


def test_notify_json_body_matches_payload_for(receiver):
    cfg = _settings(receiver.url, notifyPreset="json")
    notifier.notify(
        "success", title="T", message="M", source_id=11, profile_id=4, settings=cfg
    )
    body, _ = notifier.payload_for(
        "json", title="T", message="M", event="success",
        fields={"source_id": 11, "profile_id": 4},
    )
    assert receiver.last()["body"] == body


def test_disabled_url_skips_without_request(receiver):
    res = notifier.notify(
        "error", title="t", message="m", settings=_settings("")
    )
    assert res == {"ok": False, "skipped": "disabled", "status": None, "error": None}
    assert receiver.count == 0


def test_success_event_disabled(receiver):
    cfg = _settings(receiver.url, notifyOnSuccess=False)
    res = notifier.notify("success", title="t", message="m", settings=cfg)
    assert res["skipped"] == "event-disabled"
    assert receiver.count == 0


def test_error_event_disabled(receiver):
    cfg = _settings(receiver.url, notifyOnError=False)
    res = notifier.notify("error", title="t", message="m", settings=cfg)
    assert res["skipped"] == "event-disabled"
    assert receiver.count == 0


def test_notify_success_and_error_wrappers(receiver):
    cfg = _settings(receiver.url)
    res = notifier.notify_success("소스A", source_id=5, message="끝", settings=cfg)
    assert res["ok"] is True, res
    data = json.loads(receiver.last()["body"])
    assert "소스A" in data["title"]
    assert data["source_id"] == 5

    res = notifier.notify_error("소스B", source_id=6, message="실패", settings=cfg)
    assert res["ok"] is True, res
    assert "소스B" in json.loads(receiver.last()["body"])["title"]


# ---------------------------------------------------------------------------
# 스로틀
# ---------------------------------------------------------------------------


def test_throttle_blocks_second_send(receiver):
    cfg = _settings(receiver.url, notifyMinIntervalSec=60)
    first = notifier.notify("success", title="t", message="m", source_id=1, settings=cfg)
    second = notifier.notify("success", title="t", message="m", source_id=1, settings=cfg)
    assert first["ok"] is True
    assert second["skipped"] == "throttled"
    assert receiver.count == 1


def test_throttle_zero_sends_twice(receiver):
    cfg = _settings(receiver.url, notifyMinIntervalSec=0)
    assert notifier.notify("success", title="t", message="m", source_id=1, settings=cfg)["ok"]
    assert notifier.notify("success", title="t", message="m", source_id=1, settings=cfg)["ok"]
    assert receiver.count == 2


def test_throttle_keyed_by_event_and_source(receiver):
    cfg = _settings(receiver.url, notifyMinIntervalSec=3600)
    assert notifier.notify("success", title="t", message="m", source_id=1, settings=cfg)["ok"]
    # 다른 source -> 스로틀되지 않음
    assert notifier.notify("success", title="t", message="m", source_id=2, settings=cfg)["ok"]
    # 다른 event -> 스로틀되지 않음
    assert notifier.notify("error", title="t", message="m", source_id=1, settings=cfg)["ok"]
    assert receiver.count == 3


def test_failed_send_does_not_update_throttle(receiver):
    receiver.status = 500
    cfg = _settings(receiver.url, notifyMinIntervalSec=3600)
    first = notifier.notify("success", title="t", message="m", source_id=1, settings=cfg)
    second = notifier.notify("success", title="t", message="m", source_id=1, settings=cfg)
    assert first["ok"] is False
    assert second["skipped"] != "throttled"  # 실패는 스로틀을 갱신하지 않음
    assert receiver.count == 2


# ---------------------------------------------------------------------------
# 오류 처리
# ---------------------------------------------------------------------------


def test_http_500_returns_status_without_exception(receiver):
    receiver.status = 500
    res = notifier.notify("error", title="t", message="m", source_id=1, settings=_settings(receiver.url))
    assert res["ok"] is False
    assert res["status"] == 500
    assert res["error"] == "http-500"


def test_connection_refused_without_exception():
    port = _closed_port()
    cfg = _settings(f"http://127.0.0.1:{port}/hook")
    res = notifier.notify("error", title="t", message="m", settings=cfg)
    assert res["ok"] is False
    assert res["status"] is None
    assert res["error"]


def test_invalid_url_without_exception():
    cfg = _settings("not-a-url")
    res = notifier.notify("error", title="t", message="m", settings=cfg)
    assert res["ok"] is False
    assert res["error"]


# ---------------------------------------------------------------------------
# send_test / status
# ---------------------------------------------------------------------------


def test_send_test_ignores_throttle(receiver):
    cfg = _settings(receiver.url, notifyMinIntervalSec=3600)
    assert notifier.notify("success", title="t", message="m", source_id=1, settings=cfg)["ok"]
    assert receiver.count == 1
    res = notifier.send_test(settings=cfg)
    assert res["ok"] is True, res
    assert receiver.count == 2


def test_send_test_without_url():
    res = notifier.send_test(settings=_settings(""))
    assert res["ok"] is False
    assert res["error"] == "no-url"


def test_send_test_invalid_url():
    res = notifier.send_test(settings=_settings("ftp://example.invalid/hook"))
    assert res["ok"] is False
    assert res["error"] == "invalid-url"


def test_status_shape():
    st = notifier.status()
    assert set(st) == {"enabled", "preset", "url_host", "throttled_keys"}
    assert isinstance(st["enabled"], bool)
    assert isinstance(st["throttled_keys"], int)
