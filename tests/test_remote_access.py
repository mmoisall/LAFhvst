"""원격 접속 인증/페어링/권한 테스트 (M1/M2 인수 기준).

인수 기준(docs/remote-access-plan.md §7 M1):
- 원격(비-loopback) 요청은 토큰 없이 401, 페어링 후 200, 회수 후 401.
- 파괴적 엔드포인트는 `remoteAllowDestructive` 없이는 원격에서 403.
- 릴레이 전용 리스너는 loopback 으로 들어와도 원격으로 판정된다.
"""

import pytest
from fastapi.testclient import TestClient

import server
from core import remote_access
from core.remote_listener import RemoteOnly

from conftest import enable_remote, pair_device, pair_token_from_link

REMOTE_IP_HEADER = {"X-LAF-Client-IP": "203.0.113.7"}


def auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


# --- 기본 접근 제어 --------------------------------------------------------
def test_loopback_bypasses_token(local_client):
    assert local_client.get("/api/settings").status_code == 200


def test_remote_disabled_blocks_remote_client(remote_client):
    response = remote_client.get("/api/settings")
    assert response.status_code == 403
    assert "꺼져" in response.json()["detail"]


def test_remote_enabled_without_token_is_401(local_client, remote_client):
    enable_remote(local_client)
    response = remote_client.get("/api/settings")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate", "").startswith("Bearer")


def test_public_paths_need_no_token(remote_client):
    assert remote_client.get("/api/version").status_code == 200
    assert remote_client.get("/").status_code == 200


# --- 페어링 ---------------------------------------------------------------
def test_pair_claim_grants_access(local_client, remote_client):
    payload = enable_remote(local_client)
    token = pair_token_from_link(payload["pair"]["link"])
    claimed = remote_client.post("/api/pair/claim", json={"t": token, "name": "폰"})
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["device"]["name"] == "폰"
    assert body["device"]["role"] == "control"
    assert len(body["device_token"]) > 20

    response = remote_client.get("/api/settings", headers=auth(body["device_token"]))
    assert response.status_code == 200


def test_pair_token_is_single_use(local_client, remote_client):
    payload = enable_remote(local_client)
    token = pair_token_from_link(payload["pair"]["link"])
    assert remote_client.post("/api/pair/claim", json={"t": token}).status_code == 200
    assert remote_client.post("/api/pair/claim", json={"t": token}).status_code == 401


def test_only_hashes_are_stored(local_client, remote_client):
    claimed = pair_device(local_client, remote_client)
    token = claimed["device_token"]
    devices = remote_access.list_devices()
    assert len(devices) == 1
    assert devices[0]["id"] == claimed["device"]["id"]
    assert token not in server.SETTINGS.values()
    session_hashes = _token_hashes()
    assert token not in session_hashes
    assert remote_access.digest(token) in session_hashes


def _token_hashes() -> list[str]:
    from core import models
    from core.database import get_session

    session = get_session()
    try:
        rows = session.execute(remote_access._all_devices_query()).scalars().all()
        assert all(isinstance(row, models.RemoteDevice) for row in rows)
        return [row.token_hash for row in rows]
    finally:
        session.close()


def test_expired_pair_token_rejected(local_client, remote_client):
    enable_remote(local_client)
    remote_access.issue_pair_token(ttl=-1)  # 즉시 만료
    assert remote_access.pair_state()["active"] is False
    assert remote_access.claim("anything") is None
    response = remote_client.post("/api/pair/claim", json={"t": "wrong"})
    assert response.status_code == 401


def test_pair_claim_is_rate_limited(local_client, remote_client):
    payload = enable_remote(local_client)
    token = pair_token_from_link(payload["pair"]["link"])
    for _ in range(remote_access.CLAIM_MAX_PER_WINDOW):
        assert remote_client.post("/api/pair/claim", json={"t": "wrong"}).status_code == 401
    # 같은 IP 는 창이 지날 때까지 차단된다(유효 토큰도 거부).
    assert remote_client.post("/api/pair/claim", json={"t": token}).status_code == 401
    # 다른 IP 에서는 여전히 유효하다(토큰이 소모되지 않았음).
    other = TestClient(server.app, client=("198.51.100.4", 51234))
    assert other.post("/api/pair/claim", json={"t": token}).status_code == 200


# --- 회수 ----------------------------------------------------------------
def test_revoked_device_is_rejected(local_client, remote_client):
    claimed = pair_device(local_client, remote_client)
    token = claimed["device_token"]
    assert remote_client.get("/api/settings", headers=auth(token)).status_code == 200

    device_id = claimed["device"]["id"]
    assert local_client.delete("/api/remote/devices/%d" % device_id).json()["ok"] is True
    assert remote_client.get("/api/settings", headers=auth(token)).status_code == 401


def test_disable_drops_remote_access(local_client, remote_client):
    claimed = pair_device(local_client, remote_client)
    token = claimed["device_token"]
    assert local_client.post("/api/remote/disable").status_code == 200
    assert remote_client.get("/api/settings", headers=auth(token)).status_code == 403


# --- 권한 범위 -----------------------------------------------------------
def test_destructive_requires_optin(local_client, remote_client):
    claimed = pair_device(local_client, remote_client)
    token = claimed["device_token"]
    body = {"items": []}

    denied = remote_client.post("/api/explorer/batch-delete", json=body, headers=auth(token))
    assert denied.status_code == 403
    assert "manage" in denied.json()["detail"]

    server.SETTINGS["remoteAllowDestructive"] = True
    allowed = remote_client.post("/api/explorer/batch-delete", json=body, headers=auth(token))
    assert allowed.status_code == 200


def test_local_only_endpoints_reject_remote_device(local_client, remote_client):
    claimed = pair_device(local_client, remote_client)
    token = claimed["device_token"]
    response = remote_client.post("/api/remote/enable", headers=auth(token))
    assert response.status_code == 403


def test_reveal_is_forbidden_for_remote_device(local_client, remote_client):
    claimed = pair_device(local_client, remote_client)
    token = claimed["device_token"]
    response = remote_client.post("/api/sources/999999/reveal", headers=auth(token))
    assert response.status_code == 403
    assert "지원하지 않는" in response.json()["detail"]


# --- 상태 조회 -----------------------------------------------------------
def test_status_hides_pairing_info_from_devices(local_client, remote_client):
    claimed = pair_device(local_client, remote_client)
    token = claimed["device_token"]
    local_status = local_client.get("/api/remote/status").json()
    assert local_status["enabled"] is True
    assert local_status["pair"]["link"].startswith("http")
    assert len(local_status["devices"]) == 1

    remote_status = remote_client.get("/api/remote/status", headers=auth(token)).json()
    assert remote_status["pair"] is None
    assert [d["id"] for d in remote_status["devices"]] == [claimed["device"]["id"]]


def test_qr_png_is_png(local_client):
    enable_remote(local_client)
    response = local_client.get("/api/remote/qr.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_qr_requires_active_pair_link(local_client):
    assert local_client.get("/api/remote/qr.png").status_code == 404


# --- 릴레이 전용 리스너(M2) ----------------------------------------------
def test_remote_listener_forces_remote_classification(local_client):
    """릴레이 리스너는 TCP peer 가 loopback 이어도 원격으로 취급해야 한다."""
    wrapped = TestClient(RemoteOnly(server.app), client=("127.0.0.1", 44444))
    server.SETTINGS["remoteEnabled"] = True
    response = wrapped.get("/api/settings", headers=REMOTE_IP_HEADER)
    assert response.status_code == 401  # loopback 이면 통과했을 것

    remote_access.issue_pair_token()
    claimed = wrapped.post(
        "/api/pair/claim", json={"t": remote_access.pair_state()["token"]}, headers=REMOTE_IP_HEADER
    )
    assert claimed.status_code == 200, claimed.text
    token = claimed.json()["device_token"]

    # 원격이므로 데스크톱 전용 기능은 거부된다.
    assert wrapped.post("/api/sources/1/reveal", headers=auth(token)).status_code == 403
    # 인증된 원격 요청은 정상 동작하고, 마지막 접속 IP 는 검증된 헤더 값이다.
    assert wrapped.get("/api/settings", headers=auth(token)).status_code == 200
    devices = remote_access.list_devices()
    assert devices[0]["last_ip"] == "203.0.113.7"


def test_remote_listener_discards_forged_ip_header(local_client):
    wrapped = TestClient(RemoteOnly(server.app), client=("127.0.0.1", 44445))
    server.SETTINGS["remoteEnabled"] = True
    remote_access.issue_pair_token()
    claimed = wrapped.post(
        "/api/pair/claim",
        json={"t": remote_access.pair_state()["token"]},
        headers={"X-LAF-Client-IP": "127.0.0.1"},
    )
    assert claimed.status_code == 200
    assert remote_access.list_devices()[0]["last_ip"] == "0.0.0.0"
    token = claimed.json()["device_token"]
    assert wrapped.get("/api/settings", headers=auth(token)).status_code == 200


def test_invalid_bearer_token_is_rejected(local_client, remote_client):
    enable_remote(local_client)
    assert remote_client.get("/api/settings", headers=auth("nope")).status_code == 401
    assert remote_client.get("/api/settings", headers={"Authorization": "Basic xyz"}).status_code == 401


@pytest.mark.parametrize("path", ["/api/logs/clear", "/api/explorer/batch-move"])
def test_other_destructive_endpoints_are_guarded(local_client, remote_client, path):
    claimed = pair_device(local_client, remote_client)
    response = remote_client.post(path, json={}, headers=auth(claimed["device_token"]))
    assert response.status_code == 403
