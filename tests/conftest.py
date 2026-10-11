"""원격 접속 테스트 픽스처.

`server` 를 import 하기 **전에** 임시 DB 경로를 지정해야 한다
(`core.database` 가 import 시점에 엔진을 만든다).
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DIR = tempfile.mkdtemp(prefix="lafhvst-tests-")
os.environ["LAF_DB_PATH"] = os.path.join(_TMP_DIR, "lafhvst-test.db")
os.environ.setdefault("LAF_NO_GUI", "1")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from core import database, remote_access  # noqa: E402

LOCAL_CLIENT = ("127.0.0.1", 51234)
REMOTE_CLIENT = ("192.168.0.9", 51234)


@pytest.fixture(scope="session", autouse=True)
def _database():
    database.init_db()
    yield


def _reset_state():
    remote_access.revoke_all()
    remote_access.clear_pair_tokens()
    remote_access.reset_limits()
    server.SETTINGS.update(
        {
            "remoteEnabled": False,
            "remoteAllowDestructive": False,
            "remoteRelayUrl": "",
        }
    )


@pytest.fixture(autouse=True)
def _clean_remote_state():
    _reset_state()
    yield
    _reset_state()


@pytest.fixture
def local_client():
    """loopback(데스크톱 창) 클라이언트. lifespan 은 실행하지 않는다(스케줄러/업데이트 회피)."""
    return TestClient(server.app, client=LOCAL_CLIENT)


@pytest.fixture
def remote_client():
    """같은 네트워크의 다른 기기(=원격) 클라이언트."""
    return TestClient(server.app, client=REMOTE_CLIENT)


def enable_remote(client) -> dict:
    response = client.post("/api/remote/enable")
    assert response.status_code == 200, response.text
    return response.json()


def pair_token_from_link(link: str) -> str:
    """`.../pair.html#t=<token>&n=..&e=..` 에서 토큰을 꺼낸다."""
    fragment = link.split("#", 1)[1]
    pairs = dict(part.split("=", 1) for part in fragment.split("&") if "=" in part)
    return pairs["t"]


def pair_device(local, remote, name: str = "테스트폰") -> dict:
    payload = enable_remote(local)
    token = pair_token_from_link(payload["pair"]["link"])
    response = remote.post("/api/pair/claim", json={"t": token, "name": name})
    assert response.status_code == 200, response.text
    return response.json()
