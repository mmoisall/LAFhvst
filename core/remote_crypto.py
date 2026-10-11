"""종단간 암호화(E2E v2) — 릴레이가 내용을 읽을 수 없게 하는 앱/폰 공통 암호 계층.

계약: `relay/protocol.md` §9. 폰(PWA) 측 구현은 `frontend/js/e2e.js`(WebCrypto)이며,
양쪽이 같은 바이트 레이아웃을 써야 한다. 교차 검증 벡터는 `tests/vectors/e2e_v2.json`.

- 키 합의 ECDH P-256, KDF HKDF-SHA256, AEAD AES-256-GCM.
- 봉투(envelope)는 릴레이에게 불투명한 바이트열이다: 매직 + 기기 id + nonce + 암호문.
"""

from __future__ import annotations

import base64
import json
import os

try:  # cryptography 미설치 환경에서도 앱이 기동되도록(원격 E2E 만 비활성)
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    AVAILABLE = True
    MISSING_REASON = ""
except Exception as exc:  # noqa: BLE001
    hashes = serialization = ec = AESGCM = HKDF = None  # type: ignore[assignment]
    AVAILABLE = False
    MISSING_REASON = "cryptography 패키지가 필요합니다: " + str(exc)


def require_available() -> None:
    if not AVAILABLE:
        raise RuntimeError(MISSING_REASON or "cryptography 패키지가 필요합니다")

ENVELOPE_MAGIC = 0x02
PAIR_INFO = b"lafhvst-pair-v2"
SESSION_SALT = b"lafhvst-e2e-v1"
SESSION_INFO_PREFIX = b"device:"
AAD_PAIR_REQUEST = b"lafhvst-pair-v2|req|"
AAD_PAIR_RESPONSE = b"lafhvst-pair-v2|res|"
AAD_SESSION_PREFIX = b"lafhvst-e2e-v1"
DIRECTION_REQUEST = 0
DIRECTION_RESPONSE = 1
KEY_BYTES = 32
NONCE_BYTES = 12
MAX_PLAINTEXT_BYTES = 6 * 1024 * 1024


# --- 인코딩 ---------------------------------------------------------------
def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    padded = str(text or "") + "=" * (-len(str(text or "")) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


# --- 키 -------------------------------------------------------------------
def generate_keypair() -> tuple["ec.EllipticCurvePrivateKey", str]:
    require_available()
    """(개인키, 공개키 b64url SPKI)."""
    private = ec.generate_private_key(ec.SECP256R1())
    return private, public_key_b64(private.public_key())


def public_key_b64(public: ec.EllipticCurvePublicKey) -> str:
    return b64url_encode(
        public.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def private_key_b64(private: ec.EllipticCurvePrivateKey) -> str:
    return b64url_encode(
        private.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def load_private_key(text: str) -> ec.EllipticCurvePrivateKey:
    require_available()
    key = serialization.load_der_private_key(b64url_decode(text), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("EC 개인키가 아닙니다")
    return key


def load_public_key(text: str) -> ec.EllipticCurvePublicKey:
    require_available()
    key = serialization.load_der_public_key(b64url_decode(text))
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValueError("EC 공개키가 아닙니다")
    return key


def ecdh(private: ec.EllipticCurvePrivateKey, peer_public_b64: str) -> bytes:
    require_available()
    return private.exchange(ec.ECDH(), load_public_key(peer_public_b64))


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = KEY_BYTES) -> bytes:
    require_available()
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def pair_key(private: ec.EllipticCurvePrivateKey, peer_public_b64: str, pair_token: str) -> bytes:
    """페어링 전용 키(폰↔앱). salt = 페어링 토큰(양쪽이 아는 비밀)."""
    return hkdf(
        ecdh(private, peer_public_b64),
        salt=str(pair_token or "").encode("utf-8"),
        info=PAIR_INFO,
    )


def session_key(private: ec.EllipticCurvePrivateKey, peer_public_b64: str, device_id: int) -> bytes:
    return hkdf(
        ecdh(private, peer_public_b64),
        salt=SESSION_SALT,
        info=SESSION_INFO_PREFIX + str(int(device_id)).encode("ascii"),
    )


# --- 봉투 공통 ------------------------------------------------------------
def nonce_for(counter: int, direction: int) -> bytes:
    return int(counter).to_bytes(8, "big") + bytes([direction & 0xFF]) + b"\x00\x00\x00"


def aad_for(direction: int, device_id: int) -> bytes:
    tag = b"|req|" if direction == DIRECTION_REQUEST else b"|res|"
    return AAD_SESSION_PREFIX + tag + str(int(device_id)).encode("ascii")


def seal(key: bytes, nonce: bytes, plaintext: bytes, aad: bytes) -> bytes:
    require_available()
    return AESGCM(key).encrypt(nonce, plaintext, aad)


def unseal(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    require_available()
    return AESGCM(key).decrypt(nonce, ciphertext, aad)


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


# --- 세션 봉투 ------------------------------------------------------------
def encode_request(
    device_id: int,
    counter: int,
    key: bytes,
    *,
    method: str,
    path: str,
    query: str = "",
    headers: dict | None = None,
    body: bytes = b"",
) -> bytes:
    if len(body) > MAX_PLAINTEXT_BYTES:
        raise ValueError("요청 본문이 너무 큽니다")
    plaintext = _json_bytes(
        {
            "method": method,
            "path": path,
            "query": query,
            "headers": headers or {},
            "body_b64": b64url_encode(body),
        }
    )
    nonce = nonce_for(counter, DIRECTION_REQUEST)
    ciphertext = seal(key, nonce, plaintext, aad_for(DIRECTION_REQUEST, device_id))
    return bytes([ENVELOPE_MAGIC]) + int(device_id).to_bytes(4, "big") + nonce + ciphertext


def decode_request(device_id: int, key: bytes, blob: bytes) -> dict:
    if not blob or blob[0] != ENVELOPE_MAGIC:
        raise ValueError("알 수 없는 봉투 형식입니다")
    if len(blob) < 1 + 4 + NONCE_BYTES:
        raise ValueError("봉투가 너무 짧습니다")
    envelope_device = int.from_bytes(blob[1:5], "big")
    if envelope_device != int(device_id):
        raise ValueError("기기 id 가 일치하지 않습니다")
    nonce = blob[5 : 5 + NONCE_BYTES]
    ciphertext = blob[5 + NONCE_BYTES :]
    if nonce[8] != DIRECTION_REQUEST:
        raise ValueError("방향이 잘못된 봉투입니다")
    data = json.loads(
        unseal(key, nonce, ciphertext, aad_for(DIRECTION_REQUEST, device_id)).decode("utf-8")
    )
    return {
        "counter": int.from_bytes(nonce[:8], "big"),
        "method": str(data.get("method") or "GET").upper(),
        "path": str(data.get("path") or "/"),
        "query": str(data.get("query") or ""),
        "headers": {str(k): str(v) for k, v in (data.get("headers") or {}).items()},
        "body": b64url_decode(data.get("body_b64") or ""),
    }


def encode_response(
    device_id: int,
    counter: int,
    key: bytes,
    *,
    status: int,
    headers: dict | None = None,
    body: bytes = b"",
) -> bytes:
    plaintext = _json_bytes(
        {
            "status": int(status),
            "headers": headers or {},
            "body_b64": b64url_encode(body),
        }
    )
    nonce = nonce_for(counter, DIRECTION_RESPONSE)
    ciphertext = seal(key, nonce, plaintext, aad_for(DIRECTION_RESPONSE, device_id))
    return bytes([ENVELOPE_MAGIC]) + nonce + ciphertext


def decode_response(device_id: int, key: bytes, blob: bytes) -> dict:
    if not blob or blob[0] != ENVELOPE_MAGIC:
        raise ValueError("알 수 없는 봉투 형식입니다")
    nonce = blob[1 : 1 + NONCE_BYTES]
    ciphertext = blob[1 + NONCE_BYTES :]
    if nonce[8] != DIRECTION_RESPONSE:
        raise ValueError("방향이 잘못된 봉투입니다")
    data = json.loads(
        unseal(key, nonce, ciphertext, aad_for(DIRECTION_RESPONSE, device_id)).decode("utf-8")
    )
    return {
        "counter": int.from_bytes(nonce[:8], "big"),
        "status": int(data.get("status") or 0),
        "headers": {str(k): str(v) for k, v in (data.get("headers") or {}).items()},
        "body": b64url_decode(data.get("body_b64") or ""),
    }


# --- 페어링 봉투 ----------------------------------------------------------
def seal_pair(pair_key_bytes: bytes, plaintext: bytes, aad: bytes) -> str:
    """`nonce(12) || ciphertext` 를 b64url 로 감싼다."""
    nonce = os.urandom(NONCE_BYTES)
    return b64url_encode(nonce + seal(pair_key_bytes, nonce, plaintext, aad))


def open_pair(pair_key_bytes: bytes, encoded: str, aad: bytes) -> bytes:
    raw = b64url_decode(encoded)
    if len(raw) <= NONCE_BYTES:
        raise ValueError("페어링 봉투가 너무 짧습니다")
    return unseal(pair_key_bytes, raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad)


def encode_pair_request(pair_key_bytes: bytes, phone_public_b64: str, token: str, name: str) -> dict:
    return {
        "v": 2,
        "pk": phone_public_b64,
        "enc": seal_pair(
            pair_key_bytes,
            _json_bytes({"t": token, "name": name}),
            AAD_PAIR_REQUEST,
        ),
    }


def decode_pair_request(pair_key_bytes: bytes, payload: dict) -> dict:
    data = json.loads(open_pair(pair_key_bytes, payload.get("enc") or "", AAD_PAIR_REQUEST))
    return {"token": str(data.get("t") or ""), "name": str(data.get("name") or "")}


def encode_pair_response(pair_key_bytes: bytes, payload: dict) -> dict:
    return {"v": 2, "enc": seal_pair(pair_key_bytes, _json_bytes(payload), AAD_PAIR_RESPONSE)}


def decode_pair_response(pair_key_bytes: bytes, payload: dict) -> dict:
    return json.loads(open_pair(pair_key_bytes, payload.get("enc") or "", AAD_PAIR_RESPONSE))
