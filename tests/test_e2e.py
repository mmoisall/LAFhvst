"""E2E v2(종단간 암호화) 테스트.

파이썬으로 '폰' 역할을 구현해 페어링 봉투와 세션 봉투를 검증한다(폰의 실제 구현은
`frontend/js/e2e.js`(WebCrypto)이며 `tests/vectors/e2e_v2.json` 으로 교차 검증한다).

핵심 성질:
- 봉투에는 경로·헤더·기기 토큰이 평문으로 존재하지 않는다(릴레이가 볼 수 없음).
- 봉투 안의 요청은 **원격 리스너를 경유**하므로 미들웨어·스코프가 그대로 적용된다.
"""

import json

import pytest

import server
from core import remote_access, remote_crypto
from core.remote_listener import RemoteListener

from conftest import enable_remote
from test_remote_access import auth

DEVICE_NAME = "E2E 폰"


class Phone:
    """테스트용 폰 구현(계약 relay/protocol.md §9 그대로)."""

    def __init__(self, client, app_public: str, pair_token: str):
        self.client = client
        self.app_public = app_public
        self.pair_token = pair_token
        self.private, self.public = remote_crypto.generate_keypair()
        self.device_id = 0
        self.device_token = ""
        self.counter = 0

    # --- 페어링 --------------------------------------------------------
    def pair(self, name: str = DEVICE_NAME) -> dict:
        key = remote_crypto.pair_key(self.private, self.app_public, self.pair_token)
        payload = remote_crypto.encode_pair_request(key, self.public, self.pair_token, name)
        response = self.client.post("/api/pair/claim", json=payload)
        assert response.status_code == 200, response.text
        inner = remote_crypto.decode_pair_response(key, response.json())
        self.device_id = int(inner["device_id"])
        self.device_token = str(inner["device_token"])
        return inner

    # --- 세션 ----------------------------------------------------------
    def session_key(self) -> bytes:
        return remote_crypto.session_key(self.private, self.app_public, self.device_id)

    def envelope(self, method: str, path: str, *, query: str = "", body: bytes = b"") -> bytes:
        headers = {"authorization": "Bearer " + self.device_token, "accept": "application/json"}
        if body:
            headers["content-type"] = "application/json"
        blob = remote_crypto.encode_request(
            self.device_id,
            self.counter,
            self.session_key(),
            method=method,
            path=path,
            query=query,
            headers=headers,
            body=body,
        )
        self.counter += 1
        return blob

    def call(self, method: str, path: str, *, query: str = "", body: bytes = b"") -> dict:
        blob = self.envelope(method, path, query=query, body=body)
        response = self.client.post(
            "/api/e2e", content=blob, headers={"Content-Type": "application/octet-stream"}
        )
        assert response.status_code == 200, response.text
        decoded = remote_crypto.decode_response(self.device_id, self.session_key(), response.content)
        return decoded


@pytest.fixture
def paired_phone(local_client, remote_client):
    """E2E 로 페어링된 폰(테스트 클라이언트 = 원격 시점)."""
    enable_remote(local_client)
    payload = local_client.get("/api/remote/status").json()
    pair = payload["pair"]
    token = pair["link"].split("#t=")[1].split("&")[0]
    phone = Phone(remote_client, pair["app_public_key"], token)
    phone.pair()
    return phone


@pytest.fixture
def inner_listener():
    """E2E 봉투 안의 요청을 실행할 원격 리스너(앱 내부 1홉)."""
    listener = RemoteListener(server.app)
    port = server.SERVER_PORT + 1
    assert listener.start(port), "원격 리스너 기동 실패"
    previous = server._remote_listener
    server._remote_listener = listener
    try:
        yield listener
    finally:
        server._remote_listener = previous
        listener.stop()


# --- 페어링 ---------------------------------------------------------------
def test_pair_link_carries_app_public_key(local_client):
    enable_remote(local_client)
    pair = local_client.get("/api/remote/status").json()["pair"]
    assert pair["e2e"] is True
    assert len(pair["app_public_key"]) > 40
    assert "pk=" in pair["link"] and "v=2" in pair["link"]
    assert "pk=" in pair["app_link"]
    # 키는 한 번 만들어지면 유지된다.
    assert server.SETTINGS["remoteE2EPublicKey"] == pair["app_public_key"]
    assert server.SETTINGS["remoteE2EPrivateKey"]


def test_v2_pairing_stores_phone_key(local_client, remote_client):
    enable_remote(local_client)
    pair = local_client.get("/api/remote/status").json()["pair"]
    token = pair["link"].split("#t=")[1].split("&")[0]
    phone = Phone(remote_client, pair["app_public_key"], token)
    inner = phone.pair()
    assert inner["device_token"] and inner["device_id"]
    assert inner["public_url"].startswith("http")

    device = remote_access.e2e_device(inner["device_id"])
    assert device is not None
    assert device["public_key"] == phone.public
    # 응답에는 폰 공개키만 저장되고 평문 토큰은 저장되지 않는다.
    assert remote_access.digest(inner["device_token"]) != phone.public


def test_v2_pairing_with_wrong_token_is_rejected(local_client, remote_client):
    enable_remote(local_client)
    pair = local_client.get("/api/remote/status").json()["pair"]
    phone = Phone(remote_client, pair["app_public_key"], "WRONG-TOKEN")
    key = remote_crypto.pair_key(phone.private, phone.app_public, "WRONG-TOKEN")
    payload = remote_crypto.encode_pair_request(key, phone.public, "WRONG-TOKEN", "폰")
    response = remote_client.post("/api/pair/claim", json=payload)
    assert response.status_code == 401


# --- 세션 봉투 ------------------------------------------------------------
def test_e2e_request_roundtrip(paired_phone, inner_listener):
    result = paired_phone.call("GET", "/api/settings")
    assert result["status"] == 200
    payload = json.loads(result["body"].decode("utf-8"))
    assert "remoteEnabled" in payload
    assert result["headers"].get("content-type", "").startswith("application/json")


def test_e2e_request_reaches_public_and_authenticated_paths(paired_phone, inner_listener):
    assert paired_phone.call("GET", "/api/version")["status"] == 200
    assert paired_phone.call("GET", "/api/dashboard/live")["status"] == 200
    assert paired_phone.call("GET", "/api/explorer/running")["status"] == 200


def test_e2e_post_body_is_forwarded(paired_phone, inner_listener):
    body = json.dumps({"source_id": 999999}).encode("utf-8")
    result = paired_phone.call("POST", "/api/sources/download", body=body)
    assert result["status"] == 200
    payload = json.loads(result["body"].decode("utf-8"))
    assert payload["ok"] is True


def test_e2e_enforces_scope_inside_envelope(paired_phone, inner_listener):
    """봉투 안이라도 파괴적 작업은 옵트인 전에는 거부된다(미들웨어 경유 확인)."""
    body = json.dumps({"items": []}).encode("utf-8")
    result = paired_phone.call("POST", "/api/explorer/batch-delete", body=body)
    assert result["status"] == 403
    server.SETTINGS["remoteAllowDestructive"] = True
    result = paired_phone.call("POST", "/api/explorer/batch-delete", body=body)
    assert result["status"] == 200


def test_e2e_rejects_replay(paired_phone, inner_listener):
    blob = paired_phone.envelope("GET", "/api/version")
    first = paired_phone.client.post("/api/e2e", content=blob)
    assert first.status_code == 200
    second = paired_phone.client.post("/api/e2e", content=blob)
    assert second.status_code == 401  # 같은 카운터 재사용


def test_e2e_rejects_tampering_and_unknown_devices(paired_phone, inner_listener):
    blob = bytearray(paired_phone.envelope("GET", "/api/version"))
    blob[-1] ^= 0x01
    assert paired_phone.client.post("/api/e2e", content=bytes(blob)).status_code == 401

    other = Phone(paired_phone.client, paired_phone.app_public, "x")
    other.device_id = 999999
    other.device_token = paired_phone.device_token
    assert paired_phone.client.post("/api/e2e", content=other.envelope("GET", "/api/version")).status_code == 401

    assert paired_phone.client.post("/api/e2e", content=b"\x01\x00\x00").status_code == 400


def test_e2e_rejects_nested_envelope(paired_phone, inner_listener):
    result = paired_phone.client.post("/api/e2e", content=paired_phone.envelope("GET", "/api/e2e"))
    assert result.status_code == 400


def test_revoked_device_cannot_use_e2e(paired_phone, inner_listener, local_client):
    assert paired_phone.call("GET", "/api/version")["status"] == 200
    assert local_client.delete("/api/remote/devices/%d" % paired_phone.device_id).json()["ok"] is True
    response = paired_phone.client.post("/api/e2e", content=paired_phone.envelope("GET", "/api/version"))
    assert response.status_code == 401


def test_e2e_requires_running_remote_listener(paired_phone):
    """원격 리스너가 없으면 봉투 안의 요청을 실행할 수 없다(릴레이 미사용 상태)."""
    server._remote_listener = None
    response = paired_phone.client.post("/api/e2e", content=paired_phone.envelope("GET", "/api/version"))
    assert response.status_code == 503


def test_envelope_hides_plaintext_from_relay(paired_phone):
    """릴레이가 보는 바이트에 경로·토큰·헤더가 평문으로 존재하지 않는다."""
    blob = paired_phone.envelope("POST", "/api/quick-download", body=b'{"url":"https://example.com"}')
    assert blob[0] == remote_crypto.ENVELOPE_MAGIC
    assert paired_phone.device_token.encode("ascii") not in blob
    assert b"Bearer" not in blob
    assert b"/api/" not in blob
    assert b"example.com" not in blob
    assert b"authorization" not in blob


def test_plain_bearer_still_works_for_v1_devices(local_client, remote_client):
    """E2E 를 쓰지 않는(v1) 기기는 기존 Bearer 경로로 동작해야 한다(하위 호환)."""
    enable_remote(local_client)
    pair = local_client.get("/api/remote/status").json()["pair"]
    token = pair["link"].split("#t=")[1].split("&")[0]
    claimed = remote_client.post("/api/pair/claim", json={"t": token, "name": "v1 폰"})
    assert claimed.status_code == 200
    device_token = claimed.json()["device_token"]
    assert remote_client.get("/api/settings", headers=auth(device_token)).status_code == 200


def test_relay_target_change_restarts_transport(local_client):
    """릴레이 주소를 바꾸면 기존 연결을 정리하고 새 목적지로 다시 시작해야 한다."""
    try:
        local_client.post("/api/settings", json={"remoteRelayUrl": "http://127.0.0.1:1"})
        local_client.post("/api/remote/enable")
        first = local_client.get("/api/remote/status").json()
        assert "127.0.0.1:1" in first["url"], first
        assert first["transport"] == "relay"

        local_client.post("/api/settings", json={"remoteRelayUrl": "http://127.0.0.1:2"})
        local_client.post("/api/remote/enable")
        second = local_client.get("/api/remote/status").json()
        assert "127.0.0.1:2" in second["url"], second

        # LAN 전용으로 전환하면 릴레이 연결이 정리된다.
        local_client.post("/api/settings", json={"remoteRelayUrl": ""})
        local_client.post("/api/remote/enable")
        third = local_client.get("/api/remote/status").json()
        assert third["transport"] == "lan", third
        assert third["url"].startswith("http://") and "/h/" not in third["url"], third
    finally:
        local_client.post("/api/remote/disable")
        local_client.post("/api/settings", json={"remoteRelayUrl": ""})
