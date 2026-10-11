"""릴레이 테스트 픽스처.

실제 relayd 를 uvicorn 으로 임의 포트에 띄우고(스레드), 테스트는 sync httpx 로 붙는다.
`relay` 패키지는 저장소 루트 기준이므로 sys.path 를 먼저 채운다.
"""

from __future__ import annotations

import os
import secrets
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# relayd 를 import 하기 **전에** 레지스트리 경로를 임시 파일로 잡는다.
_TMP = tempfile.mkdtemp(prefix="laf-relay-tests-")
os.environ["LAF_RELAY_REGISTRY"] = str(Path(_TMP) / "registry.json")

import pytest  # noqa: E402
import uvicorn  # noqa: E402

from fake_agent import FakeAgent  # noqa: E402
from relay import mux, relayd  # noqa: E402
from relay.registry import Registry  # noqa: E402


@pytest.fixture(scope="session")
def relay_urls():
    """(http_base, ws_base) — 임의 포트로 뜬 실제 uvicorn 서버."""
    config = uvicorn.Config(
        relayd.app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "relayd 기동 실패"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    """테스트마다 빈 레지스트리 + 기본 한계값 + 세션 없는 상태로 되돌린다."""
    monkeypatch.setattr(relayd.hub, "registry", Registry(tmp_path / "registry.json"), raising=False)
    monkeypatch.setattr(mux, "MAX_BODY", 8 * 1024 * 1024, raising=False)
    monkeypatch.setattr(mux, "MAX_FRAME", 1 * 1024 * 1024, raising=False)
    monkeypatch.setattr(mux, "MAX_STREAMS", 16, raising=False)
    monkeypatch.setattr(mux, "HEADER_TIMEOUT", 30.0, raising=False)
    monkeypatch.setattr(mux, "STREAM_IDLE_TIMEOUT", 60.0, raising=False)
    monkeypatch.setattr(mux, "QUEUE_MAX", 32, raising=False)
    yield
    relayd.hub.sessions.clear()


@pytest.fixture
def host_id() -> str:
    """테스트마다 새 `host_id`(형식: [A-Za-z0-9_-]{16,})."""
    return "t" + uuid.uuid4().hex


@pytest.fixture
def token() -> str:
    return "tok-" + secrets.token_hex(16)


@pytest.fixture
def make_agent(relay_urls):
    """가짜 에이전트를 띄우고, 테스트 종료 시 정리한다."""
    agents: list[FakeAgent] = []

    def factory(host: str, tok: str, **kwargs) -> FakeAgent:
        agent = FakeAgent(relay_urls[1], host, tok, **kwargs)
        agents.append(agent)
        agent.start()
        return agent

    yield factory
    for agent in agents:
        agent.stop()
