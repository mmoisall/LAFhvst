"""릴레이 에이전트 테스트 (M5).

- 프레임 인코딩/디코딩, URL 매핑, 헤더 필터는 단위 테스트.
- 에이전트 경로는 **스펙(relay/protocol.md)만 구현한 스텁 릴레이** + 실제 원격 리스너로 검증한다.
  (릴레이 서버 구현과 독립적으로 에이전트의 계약 준수를 확인하기 위함)
"""

import asyncio
import threading
import json
import socket

import pytest
from websockets.asyncio.server import serve

import server
from core import remote_access, remote_tunnel
from core.remote_listener import RemoteListener

from test_remote_access import auth

HELLO = remote_tunnel.T_HELLO
HELLO_OK = remote_tunnel.T_HELLO_OK
HELLO_ERR = remote_tunnel.T_HELLO_ERR
REQ_HEAD = remote_tunnel.T_REQ_HEAD
REQ_END = remote_tunnel.T_REQ_END
RES_HEAD = remote_tunnel.T_RES_HEAD
RES_BODY = remote_tunnel.T_RES_BODY
RES_END = remote_tunnel.T_RES_END


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- 단위 ---------------------------------------------------------------
def test_frame_roundtrip():
    encoded = remote_tunnel.encode(REQ_HEAD, 7, b'{"a":1}')
    frame_type, stream_id, payload = remote_tunnel.decode(encoded)
    assert (frame_type, stream_id, payload) == (REQ_HEAD, 7, b'{"a":1}')
    frame_type, stream_id, payload = remote_tunnel.decode(
        remote_tunnel.encode_json(HELLO, 0, {"v": "1"})
    )
    assert (frame_type, stream_id) == (HELLO, 0)
    assert remote_tunnel.decode_json(payload) == {"v": "1"}


def test_frame_rejects_short_message():
    with pytest.raises(ValueError):
        remote_tunnel.decode(b"\x01\x00")


def test_agent_url_mapping():
    assert remote_tunnel.agent_url("https://relay.example.com") == "wss://relay.example.com/agent"
    assert remote_tunnel.agent_url("http://127.0.0.1:8788") == "ws://127.0.0.1:8788/agent"
    assert remote_tunnel.agent_url("relay.example.com") == "wss://relay.example.com/agent"
    assert remote_tunnel.public_url("https://relay.example.com/", "abc") == "https://relay.example.com/h/abc"


def test_header_filter_keeps_auth_drops_hop_by_hop():
    filtered = remote_tunnel._filtered_headers(
        {
            "Authorization": "Bearer x",
            "Cookie": "a=b",
            "Connection": "keep-alive",
            "Upgrade": "websocket",
            "Host": "relay.example.com",
            "Content-Type": "application/json",
        }
    )
    assert filtered["Authorization"] == "Bearer x"
    assert filtered["Cookie"] == "a=b"
    assert filtered["Content-Type"] == "application/json"
    assert "Connection" not in filtered
    assert "Upgrade" not in filtered
    assert "Host" not in filtered


# --- 통합: 스텁 릴레이 + 실제 원격 리스너 -------------------------------
class StubRelay:
    """protocol.md v1 의 서버 측 최소 구현(에이전트 검증용)."""

    def __init__(self, hello_response="ok"):
        self.port = free_port()
        self.hello_response = hello_response
        self.hello: dict | None = None
        self.frames: list[tuple[int, int, bytes]] = []
        self.hello_event = asyncio.Event()
        self.ws = None

    async def handler(self, ws):
        self.ws = ws
        frame_type, _, payload = remote_tunnel.decode(await ws.recv())
        assert frame_type == HELLO
        self.hello = remote_tunnel.decode_json(payload)
        self.hello_event.set()
        if self.hello_response != "ok":
            await ws.send(
                remote_tunnel.encode_json(
                    HELLO_ERR, 0, {"code": self.hello_response, "message": "nope"}
                )
            )
            return
        await ws.send(remote_tunnel.encode_json(HELLO_OK, 0, {"session": "s1"}))
        async for message in ws:
            decoded = remote_tunnel.decode(message)
            self.frames.append(decoded)

    async def request(self, stream_id: int, method: str, path: str, headers=None):
        await self.ws.send(
            remote_tunnel.encode_json(
                REQ_HEAD,
                stream_id,
                {"method": method, "path": path, "query": "", "headers": headers or {}},
            )
        )
        await self.ws.send(remote_tunnel.encode(REQ_END, stream_id))

    def response(self, stream_id: int) -> tuple[dict, bytes]:
        head, body = {}, bytearray()
        for frame_type, sid, payload in self.frames:
            if sid != stream_id:
                continue
            if frame_type == RES_HEAD:
                head = remote_tunnel.decode_json(payload)
            elif frame_type == RES_BODY:
                body += payload
        return head, bytes(body)

    async def wait_agent(self, agent):
        await self.hello_event.wait()
        for _ in range(200):
            if agent.state()["running"]:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("에이전트가 연결되지 않았습니다")

    async def wait_streams(self, stream_ids, timeout=30.0):
        """지정한 스트림들이 모두 RES_END 를 받을 때까지 기다린다(응답은 교차 도착할 수 있다)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            done = {sid for frame_type, sid, _ in self.frames if frame_type == RES_END}
            if set(stream_ids) <= done:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("응답이 완료되지 않았습니다: %s" % sorted(done))

    def ended(self, stream_id: int) -> bool:
        return any(frame_type == RES_END and sid == stream_id for frame_type, sid, _ in self.frames)

    async def wait_body(self, stream_id: int, timeout=20.0) -> bytes:
        """RES_END 전에 RES_BODY 가 도착하는지 기다린다(스트리밍 확인)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for frame_type, sid, payload in self.frames:
                if sid == stream_id and frame_type == RES_BODY and payload:
                    return payload
            await asyncio.sleep(0.05)
        raise AssertionError("RES_BODY 가 도착하지 않았습니다(응답이 버퍼링됨)")


async def _agent_scenario():
    """스텁 릴레이를 통해 실제 앱 API 를 왕복시킨다."""
    listener = RemoteListener(server.app)
    app_port = free_port()
    assert listener.start(app_port), "원격 리스너 기동 실패"
    relay = StubRelay()
    server.SETTINGS["remoteEnabled"] = True
    try:
        async with serve(relay.handler, "127.0.0.1", relay.port):
            agent = remote_tunnel.RelayAgent()
            await agent.start(
                relay_url="http://127.0.0.1:%d" % relay.port,
                host_id="h" * 24,
                agent_token="agent-token",
                target_port=app_port,
                label="test",
            )
            await relay.wait_agent(agent)
            await relay.request(1, "GET", "/api/version")
            await relay.request(2, "GET", "/api/remote/status")
            await relay.wait_streams([1, 2])

            head, body = relay.response(1)
            assert head["status"] == 200
            assert "version" in json.loads(body)
            # 원격 리스너 경유이므로 loopback 이어도 기기 인증이 필요하다
            remote_head, remote_body = relay.response(2)
            assert remote_head["status"] == 401, (remote_head, remote_body[:300])
            assert relay.hello["version"] == remote_tunnel.PROTOCOL_VERSION
            assert relay.hello["host_id"] == "h" * 24
            assert agent.state()["stats"]["requests"] == 2
            await agent.stop()
            assert agent.state()["running"] is False
    finally:
        listener.stop()


def test_agent_forwards_through_stub_relay():
    asyncio.run(_agent_scenario())


async def _fatal_hello_scenario():
    relay = StubRelay(hello_response="host_id_taken")
    server.SETTINGS["remoteEnabled"] = True
    async with serve(relay.handler, "127.0.0.1", relay.port):
        agent = remote_tunnel.RelayAgent()
        await agent.start(
            relay_url="http://127.0.0.1:%d" % relay.port,
            host_id="h" * 24,
            agent_token="agent-token",
            target_port=free_port(),
            label="test",
        )
        for _ in range(200):
            if not agent.running:
                break
            await asyncio.sleep(0.05)
        state = agent.state()
        assert state["running"] is False
        assert "nope" in state["error"]
        # 치명 오류는 재시도하지 않는다
        assert state["stats"]["reconnects"] == 0


def test_agent_stops_on_fatal_hello_error():
    asyncio.run(_fatal_hello_scenario())


async def _cancel_scenario():
    listener = RemoteListener(server.app)
    app_port = free_port()
    assert listener.start(app_port)
    relay = StubRelay()
    try:
        async with serve(relay.handler, "127.0.0.1", relay.port):
            agent = remote_tunnel.RelayAgent()
            await agent.start(
                relay_url="http://127.0.0.1:%d" % relay.port,
                host_id="h" * 24,
                agent_token="token",
                target_port=app_port,
            )
            await relay.wait_agent(agent)
            await relay.ws.send(
                remote_tunnel.encode_json(remote_tunnel.T_CANCEL, 99, {"reason": "client gone"})
            )
            await asyncio.sleep(0.1)
            assert agent.state()["running"] is True  # 스트림 취소가 세션을 죽이지 않는다
            await agent.stop()
    finally:
        listener.stop()


def test_unknown_stream_cancel_keeps_session():
    asyncio.run(_cancel_scenario())


# --- 로컬 연결 풀: 유휴 연결 재사용/재시도 --------------------------------
def _serve_tiny_app(port: int, keep_alive: int = 1):
    """`/ping` 만 있는 초소형 uvicorn 앱(keep-alive 타임아웃 조절용)."""
    import uvicorn
    from fastapi import FastAPI

    tiny = FastAPI()

    @tiny.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    @tiny.get("/chunked")
    def chunked():
        from fastapi.responses import StreamingResponse

        async def body():
            yield b"chunked-"
            yield b"body"

        # content-length 없이 스트리밍 → chunked 전송이 된다(회귀 조건).
        return StreamingResponse(body(), media_type="text/plain")

    config = uvicorn.Config(
        tiny, host="127.0.0.1", port=port, log_level="error", timeout_keep_alive=keep_alive
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="tiny-app", daemon=True).start()
    from server import wait_for_server

    assert wait_for_server(port, timeout=20), "초소형 앱 기동 실패"
    return server


def test_local_pool_retries_connection_closed_by_server():
    """서버가 keep-alive 로 닫은 유휴 연결을 잡아도 요청은 성공해야 한다(재시도)."""
    port = free_port()
    server = _serve_tiny_app(port, keep_alive=1)

    async def scenario():
        pool = remote_tunnel.LocalPool("127.0.0.1", port, idle_ttl=60)
        status, _headers, body = await _pool_get(pool, "/ping")
        assert status == 200 and b"ok" in body
        await asyncio.sleep(1.6)  # 서버가 유휴 연결을 닫는다
        status, _headers, body = await _pool_get(pool, "/ping")
        assert status == 200 and b"ok" in body, "닫힌 유휴 연결 재사용 실패"
        pool.close()

    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True


def test_local_pool_drops_idle_connections_after_ttl():
    port = free_port()
    server = _serve_tiny_app(port, keep_alive=30)

    async def scenario():
        pool = remote_tunnel.LocalPool("127.0.0.1", port, idle_ttl=0)
        for _ in range(3):
            status, _headers, body = await _pool_get(pool, "/ping")
            assert status == 200 and b"ok" in body
        pool.close()

    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True


async def _pool_get(pool, path):
    reader, writer, status, headers, pooled = await pool.request("GET", path, "", {}, b"")
    body = bytearray()
    async for chunk in remote_tunnel._iter_body(reader, headers):
        body += chunk
    pool.release(reader, writer, pooled, remote_tunnel._framing(headers) != "close")
    return status, headers, bytes(body)


def test_iter_body_consumes_chunked_terminator():
    """chunked 종료 청크의 트레일러까지 소비해야 연결 재사용이 안전하다."""

    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(b"5\r\nhello\r\n0\r\n\r\nNEXT")
        reader.feed_eof()
        body = bytearray()
        async for chunk in remote_tunnel._iter_body(reader, {"transfer-encoding": "chunked"}):
            body += chunk
        assert bytes(body) == b"hello"
        # 종료 CRLF 를 소비했으므로 다음 바이트가 그대로 읽힌다(오염 없음).
        assert await reader.readexactly(4) == b"NEXT"

    asyncio.run(scenario())


def test_iter_body_consumes_chunked_trailers():
    async def scenario():
        reader = asyncio.StreamReader()
        reader.feed_data(b"3\r\nabc\r\n0\r\nx-trailer: 1\r\n\r\nTAIL")
        reader.feed_eof()
        body = bytearray()
        async for chunk in remote_tunnel._iter_body(reader, {"transfer-encoding": "chunked"}):
            body += chunk
        assert bytes(body) == b"abc"
        assert await reader.readexactly(4) == b"TAIL"

    asyncio.run(scenario())


def test_pool_reuse_after_chunked_response():
    """chunked 응답 뒤 같은 연결로 보낸 다음 요청이 200 이어야 한다(오염 회귀 방지)."""
    port = free_port()
    server = _serve_tiny_app(port, keep_alive=30)

    async def scenario():
        pool = remote_tunnel.LocalPool("127.0.0.1", port)
        for _ in range(3):
            status, _headers, body = await _pool_get(pool, "/chunked")
            assert status == 200, status
            assert body == b"chunked-body", body
        # 같은 풀에서 일반 응답도 이어서 정상이어야 한다.
        status, _headers, body = await _pool_get(pool, "/ping")
        assert status == 200 and b"ok" in body
        pool.close()

    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True


def test_local_pool_retry_policy_is_safe(monkeypatch):
    """미전송 실패는 재시도, 응답 단계 실패는 멱등 메서드만 재시도한다."""
    calls = {"count": 0}
    port = free_port()
    server = _serve_tiny_app(port, keep_alive=30)

    async def failing_send(reader, writer, host, port_, method, path, query, headers, body):
        calls["count"] += 1
        if calls["count"] == 1:
            raise remote_tunnel.LocalRequestNotSent("closed before send")
        return 200, {"content-type": "text/plain", "content-length": "2"}

    async def scenario():
        pool = remote_tunnel.LocalPool("127.0.0.1", port)
        monkeypatch.setattr(remote_tunnel, "_send_request", failing_send)
        # 미전송 실패 → POST 도 재시도
        reader, writer, status, _headers, pooled = await pool.request("POST", "/x", "", {}, b"{}")
        assert status == 200 and calls["count"] == 2
        pool.release(reader, writer, pooled, True)

        calls["count"] = 0

        async def response_stage_failure(reader, writer, host, port_, method, path, query, headers, body):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("boom after send")
            return 200, {"content-type": "text/plain", "content-length": "2"}

        monkeypatch.setattr(remote_tunnel, "_send_request", response_stage_failure)
        # 응답 단계 실패 + 비멱등 → 재시도하지 않는다(중복 부작용 방지)
        try:
            await pool.request("POST", "/x", "", {}, b"{}")
            raise AssertionError("재시도하면 안 된다")
        except RuntimeError:
            pass
        assert calls["count"] == 1
        pool.close()

    try:
        asyncio.run(scenario())
    finally:
        server.should_exit = True


async def _streaming_scenario():
    """SSE(`text/event-stream`)가 버퍼링 없이 조각 단위로 전달되는지 확인한다."""
    listener = RemoteListener(server.app)
    app_port = free_port()
    assert listener.start(app_port)
    server.SETTINGS["remoteEnabled"] = True
    remote_access.issue_pair_token()
    claimed = remote_access.claim(remote_access.pair_state()["token"], name="stream")
    token = claimed["token"]
    relay = StubRelay()
    try:
        async with serve(relay.handler, "127.0.0.1", relay.port):
            agent = remote_tunnel.RelayAgent()
            await agent.start(
                relay_url="http://127.0.0.1:%d" % relay.port,
                host_id="h" * 24,
                agent_token="token",
                target_port=app_port,
            )
            await relay.wait_agent(agent)
            await relay.request(
                1, "GET", "/api/events", headers={"authorization": "Bearer " + token}
            )

            head = {}
            first = await relay.wait_body(1)
            for frame_type, sid, payload in relay.frames:
                if sid == 1 and frame_type == RES_HEAD:
                    head = remote_tunnel.decode_json(payload)
            assert head["status"] == 200, head
            assert "text/event-stream" in head["headers"].get("content-type", ""), head
            assert b": connected" in first
            # 스트림이 아직 끝나지 않았는데 본문이 도착했다 = 버퍼링하지 않는다.
            assert relay.ended(1) is False

            # 릴레이가 스트림을 취소해도 세션은 살아 있어야 한다.
            await relay.ws.send(
                remote_tunnel.encode_json(remote_tunnel.T_CANCEL, 1, {"reason": "test"})
            )
            await asyncio.sleep(0.3)
            assert agent.state()["running"] is True

            await relay.request(2, "GET", "/api/version", headers={"authorization": "Bearer " + token})
            await relay.wait_streams([2])
            version_head, version_body = relay.response(2)
            assert version_head["status"] == 200
            assert b"version" in version_body
            await agent.stop()
    finally:
        listener.stop()


def test_agent_streams_event_stream_incrementally():
    asyncio.run(_streaming_scenario())
