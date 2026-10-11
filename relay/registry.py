"""릴레이 호스트 레지스트리 — `host_id` → `sha256(agent_token)` 영속 저장.

프로토콜: `relay/protocol.md` §3, §7.

설계
- 평문 토큰은 저장하지 않는다(SHA-256 hex digest 만).
- 최초 HELLO 가 `host_id` 를 선점(bind)하고, 이후에는 상수시간 비교로만 검증한다.
- 저장은 임시 파일 + `os.replace` 로 **원자적**으로 한다(부분 기록 방지).
- 파일이 깨져 있으면 조용히 무시하지 않고 `RegistryError` 로 실패한다(재바인딩 방지).
- 경로: 환경변수 `LAF_RELAY_REGISTRY`(기본 `relay/data/registry.json`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path

log = logging.getLogger("relay.registry")

ENV_PATH = "LAF_RELAY_REGISTRY"
DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "registry.json"

# 앱이 만드는 110bit 이상 랜덤 문자열 규약(protocol.md §3.2).
HOST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,}$")

BIND_BOUND = "bound"
BIND_OK = "ok"
BIND_MISMATCH = "mismatch"


class RegistryError(RuntimeError):
    """레지스트리 파일을 신뢰할 수 없을 때(손상/읽기 실패)."""


def is_valid_host_id(host_id: object) -> bool:
    """`[A-Za-z0-9_-]{16,}` 형식인지 검사한다."""
    return isinstance(host_id, str) and HOST_ID_PATTERN.match(host_id) is not None


def token_hash(token: str) -> str:
    """토큰의 SHA-256 hex digest(저장·비교 전용)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def registry_path() -> Path:
    """환경변수를 반영한 레지스트리 경로."""
    raw = os.environ.get(ENV_PATH, "").strip()
    return Path(raw) if raw else DEFAULT_PATH


class Registry:
    """`host_id` → 토큰 해시 매핑. 프로세스 내 공유, 파일로 영속."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else registry_path()
        self._lock = threading.Lock()
        self._hosts: dict[str, str] = {}
        self.load()

    # ── 조회 ────────────────────────────────────────────────────────────
    def get(self, host_id: str) -> str | None:
        """등록된 토큰 해시(미등록이면 None)."""
        with self._lock:
            return self._hosts.get(host_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._hosts)

    def hosts(self) -> dict[str, str]:
        with self._lock:
            return dict(self._hosts)

    # ── 바인딩/검증 ─────────────────────────────────────────────────────
    def bind_or_verify(self, host_id: object, token: object) -> str:
        """최초 선점(`bound`) / 일치(`ok`) / 불일치(`mismatch`) / 형식 오류(`bad_host_id`).

        형식 오류는 프로토콜 §3.2 의 `bad_host_id` 로 매핑된다.
        빈 토큰은 절대 바인딩하지 않는다(불일치로 취급).
        """
        if not is_valid_host_id(host_id):
            return "bad_host_id"
        if not isinstance(token, str) or not token:
            return BIND_MISMATCH
        assert isinstance(host_id, str)
        digest = token_hash(token)
        with self._lock:
            known = self._hosts.get(host_id)
            if known is None:
                self._hosts[host_id] = digest
                try:
                    self._save_locked()
                except OSError:
                    # 저장 실패 시 선점을 되돌린다(재시작 후 재바인딩 허용 방지).
                    self._hosts.pop(host_id, None)
                    raise
                log.info("host bound host_id=%s", host_id)
                return BIND_BOUND
            if not hmac.compare_digest(known, digest):
                log.warning("host token mismatch host_id=%s", host_id)
                return BIND_MISMATCH
            return BIND_OK

    # ── 영속화 ──────────────────────────────────────────────────────────
    def load(self) -> None:
        """파일에서 읽는다. 없거나 비어 있으면 빈 레지스트리(최초 실행)."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            with self._lock:
                self._hosts = {}
            return
        except OSError as exc:
            raise RegistryError(f"registry 읽기 실패: {self.path}: {exc}") from exc
        if not raw.strip():
            with self._lock:
                self._hosts = {}
            return
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise RegistryError(f"registry JSON 손상: {self.path}: {exc}") from exc
        hosts = data.get("hosts") if isinstance(data, dict) else None
        if not isinstance(hosts, dict):
            raise RegistryError(f"registry 구조 오류: {self.path}")
        clean = {
            str(key): str(value)
            for key, value in hosts.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        with self._lock:
            self._hosts = clean

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        """임시 파일에 쓰고 `os.replace` 로 교체한다(호출자가 락 보유)."""
        payload = json.dumps(
            {"version": 1, "hosts": self._hosts},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "wb", dir=parent, prefix=self.path.name + ".", suffix=".tmp", delete=False
        )
        tmp = Path(handle.name)
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
