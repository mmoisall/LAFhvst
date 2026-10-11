"""HTTP 프록시 통합 테스트 — 실제 uvicorn(relayd) + 가짜 에이전트."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

from fake_agent import silent

MIB = 1024 * 1024


@pytest.fixture
def client():
    with httpx.Client(timeout=30, trust_env=False) as http:
        yield http


def _proxied(relay_urls, host: str, path: str = "") -> str:
    return f"{relay_urls[0]}/h/{host}{path}"


# ── 헬스 ────────────────────────────────────────────────────────────────
def test_healthz_empty(relay_urls, client):
    response = client.get(f"{relay_urls[0]}/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "hosts": 0, "sessions": 0, "streams": 0}


def test_healthz_counts_after_bind(relay_urls, client, make_agent, host_id, token):
    agent = make_agent(host_id, token)
    assert agent.wait_hello()
    body = client.get(f"{relay_urls[0]}/healthz").json()
    assert body == {"ok": True, "hosts": 1, "sessions": 1, "streams": 0}


# ── 경로/쿼리/헤더 ──────────────────────────────────────────────────────
def test_path_prefix_stripped_and_query_passed(relay_urls, client, make_agent, host_id, token):
    agent = make_agent(host_id, token)
    assert agent.wait_hello()

    response = client.get(_proxied(relay_urls, host_id, "/api/items?a=1&b=two%20three"))
    assert response.status_code == 200
    assert agent.wait_requests(1)
    request = agent.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/api/items"  # /h/<host_id> 프리픽스 제거
    assert request["query"] == "a=1&b=two%20three"  # 원문 그대로
    assert request["has_body"] is False
    assert response.headers["x-laf-path"] == "/api/items"
    assert response.headers["x-laf-query"] == "a=1&b=two%20three"


def test_root_path_and_custom_host(relay_urls, client, make_agent, host_id, token):
    agent = make_agent(host_id, token)
    assert agent.wait_hello()

    assert client.get(_proxied(relay_urls, host_id)).status_code == 200
    assert client.get(_proxied(relay_urls, host_id, "/")).status_code == 200
    assert agent.wait_requests(2)
    assert [req["path"] for req in agent.requests] == ["/", "/"]
    # host 헤더는 공개 도메인이 아니라 루프백으로 재작성된다.
    assert agent.requests[0]["headers"]["host"] == "127.0.0.1"


def test_headers_passthrough_and_hop_by_hop_removed(relay_urls, client, make_agent, host_id, token):
    agent = make_agent(host_id, token)
    assert agent.wait_hello()

    response = client.get(
        _proxied(relay_urls, host_id, "/echo"),
        headers={
            "Authorization": "Bearer secret",
            "Cookie": "sid=1",
            "User-Agent": "laf-phone/1.0",
            "X-Custom": "yes",
            "Connection": "keep-alive",
            "Te": "trailers",
            "Proxy-Authorization": "Basic zzz",
            "Upgrade": "h2c",
        },
    )
    assert response.status_code == 200
    assert agent.wait_requests(1)
    headers = agent.requests[0]["headers"]

    # 변형 없이 통과
    assert headers["authorization"] == "Bearer secret"
    assert headers["cookie"] == "sid=1"
    assert headers["user-agent"] == "laf-phone/1.0"
    assert headers["x-custom"] == "yes"
    # 홉바이홉/프록시/WS 업그레이드 헤더 제거
    for name in ("connection", "te", "upgrade", "proxy-authorization", "keep-alive", "transfer-encoding"):
        assert name not in headers, name


def test_response_headers_hop_by_hop_removed(relay_urls, client, make_agent, host_id, token):
    def responder(request):
        return {
            "status": 201,
            "headers": {
                "connection": "keep-alive",
                "te": "trailers",
                "transfer-encoding": "chunked",
                "content-length": "9999",
                "x-ok": "1",
                "content-type": "text/plain",
            },
            "body": b"hi",
        }

    agent = make_agent(host_id, token, responder=responder)
    assert agent.wait_hello()

    response = client.get(_proxied(relay_urls, host_id, "/res-headers"))
    assert response.status_code == 201
    assert response.text == "hi"
    assert response.headers["x-ok"] == "1"
    for name in ("connection", "te", "keep-alive"):
        assert name not in response.headers, name
    assert response.headers.get("content-length") != "9999"  # 릴레이가 재계산/생략


def test_client_ip_injected(relay_urls, client, make_agent, host_id, token):
    agent = make_agent(host_id, token)
    assert agent.wait_hello()

    assert client.get(_proxied(relay_urls, host_id, "/ip")).status_code == 200
    assert agent.wait_requests(1)
    assert agent.requests[0]["headers"]["x-laf-client-ip"] == "127.0.0.1"


def test_client_ip_from_trusted_proxy_header(relay_urls, client, make_agent, host_id, token):
    """uvicorn 프록시 헤더 처리(신뢰 IP=127.0.0.1) 결과가 주입된다 — Caddy 배포 경로."""
    agent = make_agent(host_id, token)
    assert agent.wait_hello()

    response = client.get(
        _proxied(relay_urls, host_id, "/ip"),
        headers={"X-Forwarded-For": "203.0.113.7", "X-LAF-Client-IP": "6.6.6.6"},
    )
    assert response.status_code == 200
    assert agent.wait_requests(1)
    # 클라이언트가 위조한 값은 덮어쓴다.
    assert agent.requests[0]["headers"]["x-laf-client-ip"] == "203.0.113.7"


def test_request_body_roundtrip(relay_urls, client, make_agent, host_id, token):
    agent = make_agent(host_id, token)
    assert agent.wait_hello()

    payload = b"x" * (1024 * 1024)  # 1 MiB
    response = client.post(_proxied(relay_urls, host_id, "/upload"), content=payload)
    assert response.status_code == 200
    assert response.content == payload
    assert agent.wait_requests(1)
    assert agent.requests[0]["has_body"] is True
    assert agent.requests[0]["body"] == payload


# ── 오류 매핑 ───────────────────────────────────────────────────────────
def test_offline_host_503(relay_urls, client, host_id):
    response = client.get(_proxied(relay_urls, host_id, "/anything"))
    assert response.status_code == 503
    assert response.json()["detail"] == "host offline"


def test_bad_host_id_404(relay_urls, client):
    for path in ("/h/short/", "/h/../", "/h/has!illegal-chars-123456/"):
        response = client.get(f"{relay_urls[0]}{path}")
        assert response.status_code == 404, path


def test_body_over_8mib_413_declared(relay_urls, client, make_agent, host_id, token):
    agent = make_agent(host_id, token)
    assert agent.wait_hello()

    response = client.post(
        _proxied(relay_urls, host_id, "/big"), content=b"x" * (8 * MIB + 1), timeout=60
    )
    assert response.status_code == 413
    assert agent.wait_cancel(1)
    assert agent.cancels[0][1] == "too_large"
    assert agent.pending_heads, "REQ_HEAD 는 CANCEL 전에 전달되어야 한다"


def test_body_over_8mib_413_chunked(relay_urls, client, make_agent, host_id, token):
    """content-length 없이(청크) 흘러도 릴레이가 누적 계산해 끊는다."""
    agent = make_agent(host_id, token)
    assert agent.wait_hello()

    def chunks(total: int, size: int = MIB):
        sent = 0
        while sent < total:
            piece = b"y" * min(size, total - sent)
            sent += len(piece)
            yield piece

    response = client.post(
        _proxied(relay_urls, host_id, "/stream-big"),
        content=chunks(8 * MIB + MIB),
        timeout=60,
    )
    assert response.status_code == 413
    assert agent.wait_cancel(1)
    assert agent.cancels[0][1] == "too_large"


def test_too_many_streams_429(relay_urls, client, make_agent, host_id, token):
    hold = threading.Event()
    agent = make_agent(host_id, token, hold=hold)
    assert agent.wait_hello()

    results: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        with httpx.Client(timeout=60, trust_env=False) as http:
            response = http.get(_proxied(relay_urls, host_id, "/hold"))
            with lock:
                results.append(response.status_code)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    assert agent.wait_requests(16), "16개 스트림이 열려야 한다"

    health = client.get(f"{relay_urls[0]}/healthz").json()
    assert health["streams"] == 16

    extra = client.get(_proxied(relay_urls, host_id, "/hold"))
    assert extra.status_code == 429
    assert extra.json()["detail"] == "too many streams"

    hold.set()
    for thread in threads:
        thread.join(timeout=60)
    assert results == [200] * 16


def test_client_disconnect_propagates_cancel(relay_urls, make_agent, host_id, token):
    """클라이언트가 끊기면 해당 스트림만 CANCEL 되고 세션은 유지된다."""
    port = int(relay_urls[0].rsplit(":", 1)[1])
    agent = make_agent(host_id, token, responder=silent)
    assert agent.wait_hello()

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(f"GET /h/{host_id}/gone HTTP/1.1\r\nHost: relay\r\n\r\n".encode("ascii"))
        time.sleep(0.3)
    # 연결을 닫은 뒤 릴레이가 CANCEL{reason:"client_gone"} 을 전파한다.
    assert agent.wait_requests(1, timeout=10)  # REQ_END 까지 도달
    assert agent.wait_cancel(1, timeout=10)
    assert agent.cancels[0][1] == "client_gone"
    assert agent.close_code is None  # 세션은 유지
