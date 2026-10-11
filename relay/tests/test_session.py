"""WS 세션 통합 테스트 — HELLO 계약, 대체(replaced), 한계값(CANCEL/세션 종료)."""

from __future__ import annotations

import httpx
import pytest

from fake_agent import silent
from relay import mux

MIB = 1024 * 1024


@pytest.fixture
def client():
    with httpx.Client(timeout=30, trust_env=False) as http:
        yield http


def _proxied(relay_urls, host: str, path: str = "") -> str:
    return f"{relay_urls[0]}/h/{host}{path}"


# ── HELLO ───────────────────────────────────────────────────────────────
def test_hello_ok_payload(make_agent, host_id, token):
    agent = make_agent(host_id, token)
    assert agent.wait_hello()
    assert agent.hello_err is None
    assert agent.hello_ok["host_id"] == host_id
    assert agent.hello_ok["streams_max"] == 16
    assert len(agent.hello_ok["session"]) == 32
    assert agent.error is None


def test_rebind_with_other_token_rejected(make_agent, host_id, token):
    first = make_agent(host_id, token)
    assert first.wait_hello()
    second = make_agent(host_id, "other-" + token)
    assert second.wait_hello()
    assert second.hello_ok is None
    assert second.hello_err["code"] == "host_id_taken"
    # 최초 바인딩은 유지된다 — 첫 에이전트의 세션은 살아 있다.
    assert first.close_code is None


def test_bad_host_id_rejected(make_agent):
    agent = make_agent("too-short", "tok")
    assert agent.wait_hello()
    assert agent.hello_err["code"] == "bad_host_id"


def test_version_mismatch_rejected(make_agent, host_id, token):
    agent = make_agent(host_id, token, version="2")
    assert agent.wait_hello()
    assert agent.hello_err["code"] == "version"


# ── 재연결 대체 ─────────────────────────────────────────────────────────
def test_reconnect_replaces_previous_session(relay_urls, client, make_agent, host_id, token):
    first = make_agent(host_id, token)
    assert first.wait_hello()
    second = make_agent(host_id, token)
    assert second.wait_hello()

    assert first.wait_closed(timeout=10)
    assert first.close_code == 4000
    assert first.close_reason == "replaced"
    assert client.get(f"{relay_urls[0]}/healthz").json()["sessions"] == 1
    # 새 세션은 계속 동작한다.
    assert client.get(_proxied(relay_urls, host_id, "/after")).status_code == 200


# ── 한계값 ──────────────────────────────────────────────────────────────
def test_no_progress_cancels_stream_504(relay_urls, client, make_agent, host_id, token, monkeypatch):
    monkeypatch.setattr(mux, "STREAM_IDLE_TIMEOUT", 1.0)
    agent = make_agent(host_id, token, responder=silent)
    assert agent.wait_hello()

    response = client.get(_proxied(relay_urls, host_id, "/stall"))
    assert response.status_code == 504
    assert agent.wait_cancel(1, timeout=5)
    assert agent.cancels[0][1] == "no_progress"
    assert client.get(f"{relay_urls[0]}/healthz").json()["streams"] == 0


def test_header_timeout_cancels_stream_504(relay_urls, client, make_agent, host_id, token, monkeypatch):
    monkeypatch.setattr(mux, "HEADER_TIMEOUT", 1.0)
    agent = make_agent(host_id, token, responder=silent)
    assert agent.wait_hello()

    response = client.get(_proxied(relay_urls, host_id, "/no-headers"))
    assert response.status_code == 504
    assert agent.wait_cancel(1, timeout=5)
    assert agent.cancels[0][1] == "header_timeout"


def test_ws_heartbeat_timeout_closes_session(make_agent, host_id, token, monkeypatch):
    """PING 에 응답하지 않는 세션은 닫힌다(protocol.md §3.4/§5)."""
    monkeypatch.setattr(mux, "PING_INTERVAL", 0.3)
    monkeypatch.setattr(mux, "PING_TIMEOUT", 0.5)
    agent = make_agent(host_id, token, answer_pings=False)
    assert agent.wait_hello()

    assert agent.wait_closed(timeout=10)
    assert agent.close_code == 1001


def test_frame_over_1mib_closes_session(relay_urls, client, make_agent, host_id, token):
    oversized = 1 * MIB + 1
    agent = make_agent(
        host_id,
        token,
        responder=lambda request: {"status": 200, "headers": {}, "body": b"z" * oversized},
        body_chunk=oversized,  # 1 MiB 초과 프레임을 한 번에 보낸다
    )
    assert agent.wait_hello()

    try:
        client.get(_proxied(relay_urls, host_id, "/huge"))
    except httpx.HTTPError:
        pass  # 세션이 닫히며 응답이 끊길 수 있다

    assert agent.wait_closed(timeout=10)
    assert agent.close_code == 1002  # 프로토콜 오류로 세션 종료
    assert client.get(f"{relay_urls[0]}/healthz").json()["sessions"] == 0
    assert client.get(_proxied(relay_urls, host_id, "/after")).status_code == 503
