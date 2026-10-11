"""E2E v2 암호 계층 테스트(`core/remote_crypto.py`).

- 키 합의/파생 대칭성, 봉투 인코딩·디코딩, 방향·기기 바인딩, 변조 탐지, 페어링 봉투.
- `tests/vectors/e2e_v2.json` 은 폰(WebCrypto) 구현과의 **교차 검증 벡터**다.
  이 파일이 벡터를 복호화/재암호화해 바이트 일치를 확인한다(언어 간 드리프트 방지).
"""

import json
import os

import pytest
from cryptography.exceptions import InvalidTag

from core import remote_crypto as rc

VECTOR_PATH = os.path.join(os.path.dirname(__file__), "vectors", "e2e_v2.json")


def load_vectors() -> dict:
    with open(VECTOR_PATH, encoding="utf-8") as handle:
        return json.load(handle)


# --- 키 -------------------------------------------------------------------
def test_keypair_roundtrip_and_public_encoding():
    private, public_b64 = rc.generate_keypair()
    assert rc.public_key_b64(rc.load_private_key(rc.private_key_b64(private)).public_key()) == public_b64
    assert rc.load_public_key(public_b64).curve.name == "secp256r1"


def test_ecdh_is_symmetric_and_hkdf_is_deterministic():
    app_private, app_public = rc.generate_keypair()
    phone_private, phone_public = rc.generate_keypair()
    assert rc.ecdh(app_private, phone_public) == rc.ecdh(phone_private, app_public)
    shared = rc.ecdh(app_private, phone_public)
    assert rc.hkdf(shared, b"salt", b"info") == rc.hkdf(shared, b"salt", b"info")
    assert rc.hkdf(shared, b"salt", b"info") != rc.hkdf(shared, b"other", b"info")


def test_session_key_matches_on_both_sides():
    app_private, app_public = rc.generate_keypair()
    phone_private, phone_public = rc.generate_keypair()
    assert rc.session_key(app_private, phone_public, 3) == rc.session_key(phone_private, app_public, 3)
    assert rc.session_key(app_private, phone_public, 3) != rc.session_key(app_private, phone_public, 4)


def test_pair_key_depends_on_token():
    app_private, _app_public = rc.generate_keypair()
    _phone_private, phone_public = rc.generate_keypair()
    assert rc.pair_key(app_private, phone_public, "T1") != rc.pair_key(app_private, phone_public, "T2")


# --- 봉투 -----------------------------------------------------------------
def test_nonce_layout_binds_counter_and_direction():
    nonce = rc.nonce_for(0x0102030405060708, rc.DIRECTION_REQUEST)
    assert len(nonce) == 12
    assert int.from_bytes(nonce[:8], "big") == 0x0102030405060708
    assert nonce[8] == rc.DIRECTION_REQUEST
    assert nonce[9:] == b"\x00\x00\x00"


def test_request_envelope_roundtrip():
    private, _public = rc.generate_keypair()
    key = rc.hkdf(b"shared", rc.SESSION_SALT, b"device:7")
    blob = rc.encode_request(
        7,
        5,
        key,
        method="POST",
        path="/api/quick-download",
        query="a=1&b=2",
        headers={"authorization": "Bearer x", "content-type": "application/json"},
        body=b'{"url":"https://example.com"}',
    )
    assert blob[0] == rc.ENVELOPE_MAGIC
    decoded = rc.decode_request(7, key, blob)
    assert decoded["counter"] == 5
    assert decoded["method"] == "POST"
    assert decoded["path"] == "/api/quick-download"
    assert decoded["query"] == "a=1&b=2"
    assert decoded["headers"]["authorization"] == "Bearer x"
    assert decoded["body"] == b'{"url":"https://example.com"}'


def test_response_envelope_roundtrip():
    key = rc.hkdf(b"shared", rc.SESSION_SALT, b"device:7")
    blob = rc.encode_response(
        7, 3, key, status=401, headers={"content-type": "application/json"}, body=b'{"detail":"nope"}'
    )
    decoded = rc.decode_response(7, key, blob)
    assert decoded["counter"] == 3
    assert decoded["status"] == 401
    assert decoded["body"] == b'{"detail":"nope"}'


def test_wrong_key_or_aad_is_rejected():
    key = rc.hkdf(b"shared", rc.SESSION_SALT, b"device:7")
    blob = rc.encode_request(7, 1, key, method="GET", path="/api/version")
    with pytest.raises(InvalidTag):
        rc.decode_request(7, rc.hkdf(b"other", rc.SESSION_SALT, b"device:7"), blob)
    with pytest.raises(ValueError):
        rc.decode_request(8, key, blob)  # 기기 id 불일치(봉투 헤더)
    with pytest.raises(ValueError):
        rc.decode_response(7, key, blob)  # 방향 불일치


def test_tampering_is_detected():
    key = rc.hkdf(b"shared", rc.SESSION_SALT, b"device:7")
    blob = bytearray(rc.encode_request(7, 1, key, method="GET", path="/api/version"))
    blob[-1] ^= 0x01
    with pytest.raises(InvalidTag):
        rc.decode_request(7, key, bytes(blob))


def test_bad_magic_and_short_envelope():
    key = rc.hkdf(b"shared", rc.SESSION_SALT, b"device:7")
    with pytest.raises(ValueError):
        rc.decode_request(7, key, b"\x01" + b"\x00" * 20)
    with pytest.raises(ValueError):
        rc.decode_request(7, key, b"\x02\x00")


def test_request_size_limit():
    key = rc.hkdf(b"shared", rc.SESSION_SALT, b"device:7")
    with pytest.raises(ValueError):
        rc.encode_request(7, 1, key, method="POST", path="/x", body=b"a" * (rc.MAX_PLAINTEXT_BYTES + 1))


# --- 페어링 봉투 ----------------------------------------------------------
def test_pair_envelopes_roundtrip():
    pair_key_bytes = rc.hkdf(b"shared", b"TOKEN", rc.PAIR_INFO)
    request = rc.encode_pair_request(pair_key_bytes, "PHONEPUB", "TOKEN", "폰")
    assert request["v"] == 2 and request["pk"] == "PHONEPUB"
    decoded = rc.decode_pair_request(pair_key_bytes, request)
    assert decoded == {"token": "TOKEN", "name": "폰"}

    response = rc.encode_pair_response(pair_key_bytes, {"device_id": 7, "device_token": "T"})
    assert response["v"] == 2
    assert rc.decode_pair_response(pair_key_bytes, response) == {"device_id": 7, "device_token": "T"}


def test_pair_envelope_rejects_wrong_key_and_tampering():
    key_a = rc.hkdf(b"shared", b"TOKEN-A", rc.PAIR_INFO)
    key_b = rc.hkdf(b"shared", b"TOKEN-B", rc.PAIR_INFO)
    request = rc.encode_pair_request(key_a, "PK", "TOKEN-A", "폰")
    with pytest.raises(InvalidTag):
        rc.decode_pair_request(key_b, request)
    tampered = bytearray(rc.b64url_decode(request["enc"]))
    tampered[-1] ^= 0x01
    request["enc"] = rc.b64url_encode(bytes(tampered))
    with pytest.raises(InvalidTag):
        rc.decode_pair_request(key_a, request)


# --- 교차 검증 벡터 -------------------------------------------------------
def test_vectors_are_decryptable_by_this_implementation():
    vectors = load_vectors()
    app_private = rc.load_private_key(vectors["app"]["private"])
    phone_private = rc.load_private_key(vectors["phone"]["private"])
    device_id = int(vectors["device_id"])

    assert rc.public_key_b64(app_private.public_key()) == vectors["app"]["public"]
    assert rc.public_key_b64(phone_private.public_key()) == vectors["phone"]["public"]

    assert (
        rc.session_key(app_private, vectors["phone"]["public"], device_id).hex()
        == vectors["session_key_hex"]
    )
    assert (
        rc.session_key(phone_private, vectors["app"]["public"], device_id).hex()
        == vectors["session_key_hex"]
    )
    assert (
        rc.pair_key(app_private, vectors["phone"]["public"], vectors["pair_token"]).hex()
        == vectors["pair_key_hex"]
    )

    session_key_bytes = bytes.fromhex(vectors["session_key_hex"])
    request = rc.decode_request(device_id, session_key_bytes, rc.b64url_decode(vectors["request"]["envelope"]))
    assert request["counter"] == vectors["request"]["counter"]
    assert request["path"] == vectors["request"]["plaintext"]["path"]
    assert request["headers"] == vectors["request"]["plaintext"]["headers"]

    response = rc.decode_response(device_id, session_key_bytes, rc.b64url_decode(vectors["response"]["envelope"]))
    assert response["status"] == vectors["response"]["plaintext"]["status"]
    assert response["body"] == rc.b64url_decode(vectors["response"]["plaintext"]["body_b64"])

    pair_key_bytes = bytes.fromhex(vectors["pair_key_hex"])
    assert rc.decode_pair_request(pair_key_bytes, vectors["pair_request"]) == {
        "token": vectors["pair_token"],
        "name": vectors["pair_request"]["plaintext"]["name"],
    }
    assert rc.decode_pair_response(pair_key_bytes, vectors["pair_response"]) == vectors["pair_response"]["plaintext"]
