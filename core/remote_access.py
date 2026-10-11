"""원격 접속 인증 코어 — 페어링 토큰 / 기기 토큰 발급·검증·회수.

계획 문서: `docs/remote-access-plan.md` §4.

설계 원칙
- 접근 *판정*은 `server.RemoteGuardMiddleware` 가 담당한다(원격 리스너 플래그 기반).
  이 모듈은 토큰 수명·저장·비교만 다룬다.
- 평문 토큰은 저장하지 않는다(SHA-256 해시만). 비교는 `hmac.compare_digest`.
- 페어링 토큰/레이트리밋은 프로세스 메모리, 기기 목록은 DB(`remote_devices`).
  GUI 모드와 원격 리스너는 같은 프로세스의 두 스레드이므로 메모리 상태를 공유한다.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from . import models

PAIR_TTL_SECONDS = 300
CLAIM_WINDOW_SECONDS = 60.0
CLAIM_MAX_PER_WINDOW = 5
CLAIM_MAX_FAILURES = 10
CLAIM_LOCKOUT_SECONDS = 900.0
TOUCH_INTERVAL_SECONDS = 60.0
DEVICE_ROLES = ("view", "control", "manage")
DEFAULT_DEVICE_ROLE = "control"


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_ip_literal(value) -> str | None:
    """IP 리터럴이면 정규화된 문자열, 아니면 None. (신뢰 헤더 검증용)"""
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii", "ignore")
        except Exception:
            return None
    text = str(value or "").strip()
    if not text or len(text) > 64:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def remote_client_ip(value) -> str | None:
    """릴레이가 전달한 클라이언트 IP. loopback 은 위조로 간주해 버린다."""
    text = valid_ip_literal(value)
    if text is None:
        return None
    try:
        if ipaddress.ip_address(text).is_loopback:
            return None
    except ValueError:
        return None
    return text


def _session():
    """DB 세션(순환 import 회피를 위해 지연 import)."""
    from .database import get_session

    return get_session()


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return str(value)


def _device_dict(row: models.RemoteDevice) -> dict:
    return {
        "id": row.id,
        "name": row.name or "",
        "role": row.role or DEFAULT_DEVICE_ROLE,
        "created_at": _iso(row.created_at),
        "last_seen_at": _iso(row.last_seen_at),
        "last_ip": row.last_ip or "",
        "user_agent": (row.user_agent or "")[:200],
    }


class RemoteAccess:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pair: dict[str, float] = {}          # digest -> expires_at
        self._current: dict | None = None          # 데스크톱 UI 재표시용 평문(메모리 한정)
        self._claims: dict[str, list[float]] = {}  # ip -> attempt timestamps
        self._failures: dict[str, tuple[float, int]] = {}
        self._lockout: dict[str, float] = {}
        self._touch: dict[int, float] = {}

    # --- 페어링 토큰 ---------------------------------------------------
    def issue_pair_token(self, ttl: int = PAIR_TTL_SECONDS) -> dict:
        """새 페어링 토큰을 발급하고 기존 토큰은 폐기한다(활성 토큰 1개)."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._pair = {digest(token): now + ttl}
            self._current = {"token": token, "expires_at": now + ttl}
        return {"token": token, "expires_at": now + ttl, "ttl": ttl}

    def clear_pair_tokens(self) -> int:
        with self._lock:
            count = len(self._pair)
            self._pair.clear()
            self._current = None
        return count

    def pair_state(self) -> dict:
        """활성 페어링 토큰 상태. `token` 은 데스크톱(loopback) UI 전용이다."""
        now = time.time()
        with self._lock:
            expired = [key for key, exp in self._pair.items() if exp <= now]
            for key in expired:
                self._pair.pop(key, None)
            current = self._current
            if current and current["expires_at"] <= now:
                current = self._current = None
            if not current:
                return {"active": False, "token": None, "expires_at": None, "expires_in": 0}
            return {
                "active": True,
                "token": current["token"],
                "expires_at": current["expires_at"],
                "expires_in": max(0, int(round(current["expires_at"] - now))),
            }

    # --- 레이트리밋 -----------------------------------------------------
    def reset_limits(self) -> None:
        """페어링 시도/실패/차단 상태를 모두 비운다(운영·테스트용)."""
        with self._lock:
            self._claims.clear()
            self._failures.clear()
            self._lockout.clear()

    def _rate_ok(self, ip: str) -> bool:
        """호출자가 self._lock 을 보유한 상태에서만 호출한다."""
        now = time.time()
        until = self._lockout.get(ip, 0.0)
        if until > now:
            return False
        if until:
            self._lockout.pop(ip, None)
            self._failures.pop(ip, None)
        attempts = [t for t in self._claims.get(ip, []) if now - t < CLAIM_WINDOW_SECONDS]
        if len(attempts) >= CLAIM_MAX_PER_WINDOW:
            self._claims[ip] = attempts
            return False
        attempts.append(now)
        self._claims[ip] = attempts
        return True

    def _note_failure(self, ip: str) -> None:
        """호출자가 self._lock 을 보유한 상태에서만 호출한다."""
        now = time.time()
        window_start, count = self._failures.get(ip, (now, 0))
        if now - window_start > CLAIM_LOCKOUT_SECONDS:
            window_start, count = now, 0
        count += 1
        self._failures[ip] = (window_start, count)
        if count >= CLAIM_MAX_FAILURES:
            self._lockout[ip] = now + CLAIM_LOCKOUT_SECONDS
            self._failures[ip] = (now, 0)

    def record_failure(self, ip: str) -> None:
        """페어링 실패 1회 기록(암호화 경로 등 외부에서 호출)."""
        key = valid_ip_literal(ip) or (ip or "")[:64]
        with self._lock:
            self._note_failure(key)

    # --- 기기 토큰 ------------------------------------------------------
    def claim(
        self,
        pair_token: str,
        *,
        name: str = "",
        ip: str = "",
        user_agent: str = "",
        public_key: str | None = None,
    ):
        """페어링 토큰을 소비해 기기를 등록한다. 실패 시 None."""
        ip = valid_ip_literal(ip) or (ip or "")[:64]
        now = time.time()
        with self._lock:
            if not self._rate_ok(ip):
                return None
            expected = self._pair.pop(digest(pair_token or ""), None)
            if expected is None or expected <= now:
                self._note_failure(ip)
                return None
            self._failures.pop(ip, None)
            if self._current and self._current.get("expires_at") == expected:
                self._current = None
        token = secrets.token_urlsafe(32)
        session = _session()
        try:
            row = models.RemoteDevice(
                name=(name or "").strip()[:120] or "원격 기기",
                token_hash=digest(token),
                role=DEFAULT_DEVICE_ROLE,
                public_key=(public_key or None),
                e2e_counter=-1,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            device = _device_dict(row)
        finally:
            session.close()
        self.touch(device["id"], ip=ip, user_agent=user_agent, force=True)
        # 기기를 하나 등록했으니 데스크톱 화면의 QR 을 새 토큰으로 갱신한다(재사용 불가).
        self.issue_pair_token()
        return {"token": token, "device": device}

    def verify(self, token: str | None) -> dict | None:
        """기기 토큰 검증. 유효하면 기기 dict, 아니면 None."""
        if not token:
            return None
        target = digest(token)
        session = _session()
        try:
            rows = session.execute(_all_devices_query()).scalars().all()
            for row in rows:
                if row.revoked_at is None and hmac.compare_digest(row.token_hash, target):
                    return _device_dict(row)
        finally:
            session.close()
        return None

    def touch(self, device_id: int, *, ip: str = "", user_agent: str = "", force=False) -> None:
        """last_seen 갱신(기기당 1분에 한 번)."""
        now = time.time()
        with self._lock:
            if not force and now - self._touch.get(device_id, 0.0) < TOUCH_INTERVAL_SECONDS:
                return
            self._touch[device_id] = now
        session = _session()
        try:
            row = session.get(models.RemoteDevice, device_id)
            if row is None or row.revoked_at is not None:
                return
            row.last_seen_at = datetime.now(timezone.utc)
            if ip:
                row.last_ip = ip[:64]
            if user_agent:
                row.user_agent = user_agent[:400]
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

    def list_devices(self) -> list[dict]:
        session = _session()
        try:
            rows = session.execute(_all_devices_query()).scalars().all()
            return [_device_dict(row) for row in rows if row.revoked_at is None]
        finally:
            session.close()

    def device_count(self) -> int:
        return len(self.list_devices())

    # --- E2E v2 ---------------------------------------------------------
    def e2e_device(self, device_id: int) -> dict | None:
        """E2E 에 필요한 기기 정보(공개키/마지막 카운터). 없거나 회수됐으면 None."""
        session = _session()
        try:
            row = session.get(models.RemoteDevice, int(device_id))
            if row is None or row.revoked_at is not None:
                return None
            return {
                "id": row.id,
                "name": row.name or "",
                "role": row.role or DEFAULT_DEVICE_ROLE,
                "public_key": row.public_key or "",
                "counter": int(row.e2e_counter if row.e2e_counter is not None else -1),
            }
        finally:
            session.close()

    def accept_counter(self, device_id: int, counter: int) -> bool:
        """재전송 방지: 카운터가 마지막 값보다 커야 하며, 통과하면 저장한다."""
        session = _session()
        try:
            row = session.get(models.RemoteDevice, int(device_id))
            if row is None or row.revoked_at is not None:
                return False
            last = int(row.e2e_counter if row.e2e_counter is not None else -1)
            if int(counter) <= last:
                return False
            row.e2e_counter = int(counter)
            row.last_seen_at = datetime.now(timezone.utc)
            session.commit()
            return True
        except Exception:
            session.rollback()
            return False
        finally:
            session.close()

    def revoke(self, device_id: int) -> bool:
        session = _session()
        try:
            row = session.get(models.RemoteDevice, device_id)
            if row is None or row.revoked_at is not None:
                return False
            row.revoked_at = datetime.now(timezone.utc)
            session.commit()
            return True
        finally:
            session.close()

    def revoke_all(self) -> int:
        session = _session()
        try:
            rows = session.execute(_all_devices_query()).scalars().all()
            now = datetime.now(timezone.utc)
            count = 0
            for row in rows:
                if row.revoked_at is None:
                    row.revoked_at = now
                    count += 1
            session.commit()
            return count
        finally:
            session.close()


def _all_devices_query():
    return select(models.RemoteDevice).order_by(models.RemoteDevice.id)


_access = RemoteAccess()

# 모듈 공개 API(호출부는 `remote_access.<fn>` 형태로 쓴다).
issue_pair_token = _access.issue_pair_token
clear_pair_tokens = _access.clear_pair_tokens
pair_state = _access.pair_state
reset_limits = _access.reset_limits
claim = _access.claim
verify = _access.verify
touch = _access.touch
list_devices = _access.list_devices
device_count = _access.device_count
revoke = _access.revoke
revoke_all = _access.revoke_all
record_failure = _access.record_failure
e2e_device = _access.e2e_device
accept_counter = _access.accept_counter
