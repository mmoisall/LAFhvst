import hashlib
import json
import os
import random
import shutil
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from . import kde, site_url, utils


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        text = str(value).strip().split("+")[0].split("Z")[0]
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _load_json(text) -> dict:
    if not text:
        return {}
    if isinstance(text, dict):
        return text
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _dump_json(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value or {}, ensure_ascii=False)


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    return value


def _now() -> datetime:
    return datetime.now()


# config 안에 '직접 설정한 필드' 목록을 기록하는 키.
#   - 값이 없으면(키 없음) 해당 필드는 상속(부모/전역) 을 따른다.
#   - 목록에 있으면 직접 설정한 값이며, 그 값이 전역 기본값과 같으면 "전역 설정"으로 본다.
EXPLICIT_KEY = "__explicit"


def _explicit_set(raw_config) -> set:
    values = (raw_config or {}).get(EXPLICIT_KEY)
    if isinstance(values, dict):
        return {str(key) for key, flag in values.items() if flag}
    if isinstance(values, (list, tuple, set)):
        return {str(item) for item in values}
    return set()


def explicit_fields(own) -> set:
    """아이템 config.__explicit 에 기록된 '직접 설정' 필드 집합."""
    return _explicit_set(_load_json(getattr(own, "config", None)))


def with_explicit(config: dict, field: str, enabled: bool) -> dict:
    """config 사본에서 field 를 직접 설정 목록에 넣거나 뺀다."""
    result = dict(config or {})
    current = _explicit_set(result)
    if enabled:
        current.add(field)
    else:
        current.discard(field)
    if current:
        result[EXPLICIT_KEY] = sorted(current)
    else:
        result.pop(EXPLICIT_KEY, None)
    return result


DEFAULT_PROFILE_STATUS = "정상"
DEFAULT_LEARNED_CAPACITY = 500
DEFAULT_LEARNED_COOLDOWN_MINUTES = 15

# --- 유기적 적응 제어기 하이퍼파라미터 ---------------------------------------
CAPACITY_ALPHA_MIN = 0.08          # 관측이 쌓일수록 학습률 하한
CAPACITY_ALPHA_MAX = 0.5           # 초기 관측 학습률 상한
CAPACITY_FLOOR = 1                 # 안전 한도 하한
SCALE_UP_USAGE_RATIO = 0.85        # 상향을 위한 실제 헤드룸 증거 기준
SCALE_UP_FACTOR = 1.1              # 상향 목표 배율
SCALE_UP_BETA_RATIO = 0.5          # 상향 학습률 = α * 0.5 (비대칭)
STABLE_RUNS_TO_SCALE = 3           # 이 횟수만큼 연속 성공 시 쿨다운 base 완화
COOLDOWN_BASE_MIN_MINUTES = 5
COOLDOWN_BASE_MAX_MINUTES = 180
COOLDOWN_BACKOFF_MAX_EXPONENT = 3  # 2**3 = 8배 상한
COOLDOWN_JITTER = 0.15             # ±15% 지터
USAGE_WINDOW_MINUTES = 15          # 사이트 카운팅 윈도우(사용량 lazy reset)
GROUP_PRESSURE_WINDOW_MINUTES = 10
GROUP_PRESSURE_THRESHOLD = 2       # 그룹 내 N개 계정이 동시에 터지면 공유 한도 의심
GROUP_BASE_COOLDOWN_MINUTES = 5
GROUP_COOLDOWN_MAX_MINUTES = 60
GROUP_PACE_MAX_SECONDS = 10.0


class Base(DeclarativeBase):
    pass


profile_group_members = Table(
    "profile_group_members",
    Base.metadata,
    Column(
        "profile_group_id",
        ForeignKey("profile_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "profile_id",
        ForeignKey("profiles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Folder(Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id"), nullable=True)
    config: Mapped[str] = mapped_column(Text, default="{}")
    cyc_option: Mapped[int] = mapped_column(Integer, default=0)
    cyc: Mapped[int] = mapped_column(Integer, default=1)
    log_level: Mapped[str] = mapped_column(String(20), default="INFO")
    tag_list: Mapped[str] = mapped_column(Text, default="")
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("profile_groups.id"), nullable=True
    )
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True
    )
    order_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    parent: Mapped["Folder | None"] = relationship(
        remote_side=[id], back_populates="children"
    )
    children: Mapped[list["Folder"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    sources: Mapped[list["Source"]] = relationship(
        back_populates="folder", cascade="all, delete-orphan"
    )
    profile_group: Mapped["ProfileGroup | None"] = relationship(back_populates="folders")


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folders.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str] = mapped_column(String(1024), default="")
    site: Mapped[str | None] = mapped_column(String(200), nullable=True)
    key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    config: Mapped[str] = mapped_column(Text, default="{}")
    cyc_option: Mapped[int] = mapped_column(Integer, default=0)
    cyc: Mapped[int] = mapped_column(Integer, default=1)
    log_level: Mapped[str] = mapped_column(String(20), default="INFO")
    tag_list: Mapped[str] = mapped_column(Text, default="")
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="idle")
    recent_file_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recent_file_mtime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recent_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    oldest_err_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    profile_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("profile_groups.id"), nullable=True
    )
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="SET NULL"), nullable=True
    )
    order_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- AI 업로드 예측 (KDE) --------------------------------------------
    learned_upload_cycle: Mapped[int] = mapped_column(
        Integer, default=kde.DEFAULT_BASE_CYCLE_MINUTES
    )
    last_new_file_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    upload_time_history: Mapped[str] = mapped_column(Text, default="[]")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    folder: Mapped["Folder | None"] = relationship(back_populates="sources")
    errors: Mapped[list["GdlErrorLog"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )
    profile_group: Mapped["ProfileGroup | None"] = relationship(back_populates="sources")


class GdlErrorLog(Base):
    """아이템/시스템 로그. 기존 'gallery-dl 오류 로그'를 확장한 통합 로그 테이블.

    - 오류(ERROR/WARN): 무제한 저장 (동일 내용도 별도 행 허용)
    - 정상(INFO/DEBUG/SUCCESS 등): fingerprint 동일 시 최초 1건 유지 + 반복 횟수/최근 시각 갱신
    - status 로 미해결(open)/해결(resolved)/무시(ignored) 를 추적한다.
    """

    __tablename__ = "gdl_error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    profile_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    folder_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    level: Mapped[str] = mapped_column(String(20), default="ERROR")
    category: Mapped[str] = mapped_column(String(40), default="")
    scope: Mapped[str] = mapped_column(String(20), default="system")
    status: Mapped[str] = mapped_column(String(20), default="open")
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    repeat_count: Mapped[int] = mapped_column(Integer, default=1)
    context: Mapped[str] = mapped_column(Text, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    last_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    source: Mapped["Source | None"] = relationship(back_populates="errors")


class Profile(Base):
    """단일 계정 프로파일.

    Playwright 영구 컨텍스트(user_data_dir)에 로그인 세션/쿠키를 보관하며,
    한도 초과 감지 시 스스로 안전 한도와 쿨다운을 학습한다.
    """

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    site: Mapped[str | None] = mapped_column(String(200), nullable=True)
    context_dir: Mapped[str] = mapped_column(String(1024), default="")
    proxy_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    account_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    account_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=DEFAULT_PROFILE_STATUS)
    is_site_default: Mapped[bool] = mapped_column(Boolean, default=False)

    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    learned_capacity: Mapped[int] = mapped_column(Integer, default=DEFAULT_LEARNED_CAPACITY)
    learned_cooldown_minutes: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_LEARNED_COOLDOWN_MINUTES
    )
    recent_usage_count: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_stable_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # --- 유기적 학습 상태 ---------------------------------------------------
    observations: Mapped[int] = mapped_column(Integer, default=0)
    learned_rate_per_hour: Mapped[float] = mapped_column(Float, default=0.0)
    window_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cooldown_probe_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    capacity_lo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capacity_hi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success_streak: Mapped[int] = mapped_column(Integer, default=0)
    observed_recovery_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    order_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    groups: Mapped[list["ProfileGroup"]] = relationship(
        secondary=profile_group_members, back_populates="profiles"
    )


class ProfileGroup(Base):
    """합동 프로파일 group (여러 단일 Profile을 묶어 돌려쓴다)."""

    __tablename__ = "profile_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    site: Mapped[str | None] = mapped_column(String(200), nullable=True)
    account_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    account_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # --- 그룹(사이트) 공유 한도 조정 ---------------------------------------
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    concurrent_limit: Mapped[int] = mapped_column(Integer, default=1)
    pace_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    shared_limit: Mapped[bool] = mapped_column(Boolean, default=True)
    order_index: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    profiles: Mapped[list["Profile"]] = relationship(
        secondary=profile_group_members, back_populates="groups"
    )
    folders: Mapped[list["Folder"]] = relationship(back_populates="profile_group")
    sources: Mapped[list["Source"]] = relationship(back_populates="profile_group")


class ProfileLearningEvent(Base):
    """프로파일 한도/쿨다운 학습 관측 기록 (UI 학습 곡선 + 감사)."""

    __tablename__ = "profile_learning_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), nullable=True
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(40), default="")
    usage_delta: Mapped[int] = mapped_column(Integer, default=0)
    capacity_before: Mapped[int] = mapped_column(Integer, default=0)
    capacity_after: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=0)
    rate_per_hour: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ReactiveRule(Base):
    """반응형 감시 규칙.

    브라우저 감시자(browser_watcher)가 실시간으로 포착한 알림 텍스트를
    ``keyword_pattern`` 과 대조하여 조건이 맞으면 ``target_folder`` 로
    즉시 gallery-dl 단발 수집을 트리거한다.
    """

    __tablename__ = "reactive_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    site: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # JSON 배열 문자열. 각 항목은 일반 키워드(대소문자 무시 부분일치) 또는
    # "/정규식/플래그" 형식의 정규표현식 룰.
    keyword_pattern: Mapped[str] = mapped_column(Text, default="[]")
    target_folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="null")


def get_app_settings() -> dict:
    from .database import get_session

    session = get_session()
    try:
        rows = session.execute(select(AppSetting)).scalars().all()
        result = {}
        for row in rows:
            try:
                result[row.key] = json.loads(row.value)
            except (TypeError, ValueError):
                result[row.key] = row.value
        return result
    finally:
        session.close()


def update_app_settings(values: dict) -> dict:
    from .database import get_session

    session = get_session()
    try:
        for key, value in values.items():
            row = session.get(AppSetting, key)
            encoded = json.dumps(value, ensure_ascii=False)
            if row is None:
                session.add(AppSetting(key=key, value=encoded))
            else:
                row.value = encoded
        session.commit()
        return get_app_settings()
    finally:
        session.close()


def folder_chain(session, folder_id):
    chain: list[Folder] = []
    current = folder_id
    while current is not None:
        folder = session.get(Folder, current)
        if folder is None:
            break
        chain.append(folder)
        current = folder.parent_id
    chain.reverse()
    return chain


def _merge_config(ancestor_configs, own_config) -> dict:
    effective: dict = {}
    for raw in ancestor_configs:
        effective.update(_load_json(raw))
    effective.update(own_config or {})
    return effective


def _profile_ref(node):
    """노드에 설정된 프로파일 참조를 (kind, id)로 반환한다 (단일 우선)."""
    if node is None:
        return None
    profile_id = getattr(node, "profile_id", None)
    if profile_id is not None:
        return ("profile", profile_id)
    group_id = getattr(node, "profile_group_id", None)
    if group_id is not None:
        return ("group", group_id)
    return None


def resolve_profile_ref(own, ancestor_nodes):
    """자기 자신 → 가까운 조상 순으로 처음 만나는 프로파일 참조를 반환."""
    for node in [own] + list(ancestor_nodes or []):
        ref = _profile_ref(node)
        if ref is not None:
            return ref
    return None


def default_profile_for_site(session, site) -> "Profile | None":
    """해당 사이트의 디폴트 단일 프로파일(사이트당 1개)을 찾는다."""
    norm = site_url.normalize_site(site)
    if not norm:
        return None
    rows = (
        session.execute(
            select(Profile).where(Profile.is_site_default.is_(True)).order_by(Profile.id)
        )
        .scalars()
        .all()
    )
    for profile in rows:
        candidate = site_url.normalize_site(profile.site)
        if not candidate:
            site_name, _key = site_url.url_to_site_key(profile.site or "")
            candidate = site_url.normalize_site(site_name)
        if candidate and candidate == norm:
            return profile
    return None


def _nearest_ancestor_raw(ancestor_nodes, field):
    """가장 가까운 조상의 raw 값 중 '전역 디폴트가 아닌' 첫 값을 반환."""
    defaults = item_defaults()
    default_log = (defaults.get("log_level") or "INFO").upper()
    default_cyc = int(defaults.get("cyc_option") or 0)
    for node in ancestor_nodes or []:
        value = getattr(node, field, None)
        if value is None:
            continue
        if field == "log_level":
            text = (value or "").upper()
            if text and text != default_log:
                return value
        elif field == "cyc_option":
            if int(value or 0) != default_cyc:
                return value
        elif field in ("profile_id", "profile_group_id"):
            if value is not None:
                return value
        elif field == "config":
            if _load_json(value):
                return value
        else:
            return value
    return None


def item_defaults() -> dict:
    """아이템 전역 디폴트값 (app_settings 우선, 없으면 코드 기본값)."""
    defaults = {"cyc_option": 0, "cyc": 1, "log_level": "INFO", "config": {}}
    try:
        stored = get_app_settings()
    except Exception:
        stored = {}
    if "itemCycOption" in stored:
        defaults["cyc_option"] = int(stored.get("itemCycOption") or 0)
    if "itemCyc" in stored:
        defaults["cyc"] = max(1, int(stored.get("itemCyc") or 1))
    if "itemLogLevel" in stored:
        defaults["log_level"] = str(stored.get("itemLogLevel") or "INFO").upper()
    if "itemConfig" in stored and isinstance(stored.get("itemConfig"), dict):
        defaults["config"] = stored.get("itemConfig") or {}
    return defaults


def item_gdl_defaults() -> dict:
    """아이템 수집용 gdl 기본 세팅(app_settings 우선)."""
    result = {
        "sleep": None,
        "sleep_request": None,
        "retries": None,
        "timeout": None,
        "limit_rate": None,
        "date_basis": "filter",
        "metadata_yaml": False,
    }
    try:
        stored = get_app_settings()
    except Exception:
        stored = {}

    def _num(key):
        value = stored.get(key)
        try:
            return float(value) if value is not None and value != "" else None
        except (TypeError, ValueError):
            return None

    def _range(min_key, max_key):
        lo = _num(min_key)
        hi = _num(max_key)
        if lo is None and hi is None:
            return None
        lo = lo if lo is not None else hi
        hi = hi if hi is not None else lo
        return (lo, hi)

    result["sleep"] = _range("itemSleepMin", "itemSleepMax")
    result["sleep_request"] = _range("itemSleepRequestMin", "itemSleepRequestMax")
    result["retries"] = _num("itemRetries")
    result["timeout"] = _num("itemTimeout")
    result["limit_rate"] = stored.get("itemLimitRate") or None
    if stored.get("itemDateBasis"):
        result["date_basis"] = str(stored.get("itemDateBasis"))
    result["metadata_yaml"] = stored.get("itemMetadataYaml", False) is True
    # URL/메타데이터의 title 을 아이템 이름으로 사용(전역 디폴트 ON)
    result["title_as_name"] = stored.get("itemTitleAsName", True) is not False
    result["name_update_on_collect"] = stored.get("itemNameUpdateOnCollect", True) is not False
    return result


def item_retry_delay() -> float:
    try:
        stored = get_app_settings()
    except Exception:
        stored = {}
    try:
        return max(0.0, float(stored.get("itemRetryDelay", 10)))
    except (TypeError, ValueError):
        return 10.0


def _compute_inherited(own, ancestor_nodes, profile_source=None, own_is_root=False) -> dict:
    """자식 값이 전역 디폴트/null이고, 조상 raw 값이 전역 디폴트와 다를 때만 상속 True."""
    defaults = item_defaults()
    default_cyc = int(defaults.get("cyc_option") or 0)
    default_log = (defaults.get("log_level") or "INFO").upper()
    explicit = explicit_fields(own)
    cyc_value = int(getattr(own, "cyc_option", 0) or 0)
    log_value = (getattr(own, "log_level", None) or "INFO").upper()
    nearest_cyc = _nearest_ancestor_raw(ancestor_nodes, "cyc_option")
    nearest_log = _nearest_ancestor_raw(ancestor_nodes, "log_level")
    cyc_marked = "cyc_option" in explicit
    log_marked = "log_level" in explicit
    cyc_inherited = bool(
        not cyc_marked and cyc_value == default_cyc and nearest_cyc is not None
    )
    log_inherited = bool(
        not log_marked and log_value == default_log and nearest_log is not None
    )
    profile_inherited = bool(
        getattr(own, "profile_id", None) is None
        and getattr(own, "profile_group_id", None) is None
        and profile_source == "explicit"
    )
    config_inherited = bool(
        not _load_json(getattr(own, "config", None))
        and _nearest_ancestor_raw(ancestor_nodes, "config") is not None
    )
    save_path_inherited = bool(
        not own_is_root
        and _nearest_ancestor_raw(ancestor_nodes, "name") is not None
        and any((getattr(node, "name", None) or "").strip() for node in (ancestor_nodes or []))
    )
    inherited = {
        "cyc": cyc_inherited,
        "log_level": log_inherited,
        "profile": profile_inherited,
        "config": config_inherited,
        "save_path": save_path_inherited,
    }

    # overridden(자체 설정): 값이 전역 디폴트와 다르면 직접 설정(레거시 마커 없음 포함)
    cyc_overridden = bool(cyc_value != default_cyc)
    log_overridden = bool(log_value != default_log)
    # global(전역 설정): 마커가 있고 값이 전역 디폴트와 같은 경우
    cyc_global = bool(cyc_marked and cyc_value == default_cyc)
    log_global = bool(log_marked and log_value == default_log)
    own_profile = getattr(own, "profile_id", None) is not None or getattr(own, "profile_group_id", None) is not None
    profile_overridden = bool(not profile_inherited and own_profile)
    config_overridden = bool(
        not config_inherited and bool(_load_json(getattr(own, "config", None)))
    )
    save_path_overridden = bool(own_is_root and (getattr(own, "name", None) or "").strip())
    return {"inherited": inherited, "overridden": {
        "cyc": cyc_overridden,
        "log_level": log_overridden,
        "profile": profile_overridden,
        "config": config_overridden,
        "save_path": save_path_overridden,
    }, "global": {
        "cyc": cyc_global,
        "log_level": log_global,
    }, "effective": {
        "cyc_option": int(nearest_cyc) if nearest_cyc is not None else default_cyc,
        "log_level": (str(nearest_log).upper() if nearest_log else default_log),
    }, "explicit": sorted(explicit)}


def _resolve_effective_profile(session, own, ancestor_nodes, site=None) -> dict:
    """유효 프로파일 참조: ①근접 단일 ②근접 그룹 ③사이트 디폴트 ④없음."""
    ref = resolve_profile_ref(own, ancestor_nodes)
    if ref is not None:
        if ref[0] == "profile":
            return {"profile_id": ref[1], "profile_group_id": None, "profile_source": "explicit"}
        return {"profile_id": None, "profile_group_id": ref[1], "profile_source": "explicit"}
    if site:
        default = default_profile_for_site(session, site)
        if default is not None:
            return {
                "profile_id": default.id,
                "profile_group_id": None,
                "profile_source": "site_default",
            }
    return {"profile_id": None, "profile_group_id": None, "profile_source": None}


def folder_context(session, folder_id) -> dict:
    chain = folder_chain(session, folder_id)
    if not chain:
        return {
            "save_path": utils.compute_base_path(""),
            "effective_tags": [],
            "effective_config": {},
            "date_basis_order": list(utils.DEFAULT_DATE_BASIS_ORDER),
            "root_name": "",
            "profile_id": None,
            "profile_group_id": None,
            "profile_source": None,
            "inherited": {"cyc": False, "log_level": False, "profile": False, "config": False, "save_path": False},
            "overridden": {"cyc": False, "log_level": False, "profile": False, "config": False, "save_path": False},
            "global": {},
            "effective": {
                "cyc_option": int(item_defaults().get("cyc_option") or 0),
                "log_level": (item_defaults().get("log_level") or "INFO").upper(),
            },
            "explicit": [],
        }
    root = chain[0]
    descendants = [item.name for item in chain[1:]]
    save_path = utils.join_save_path(root.name, descendants)
    ancestor_raws = [item.tag_list for item in chain[:-1]]
    effective = utils.resolve_effective_tags(chain[-1].tag_list, ancestor_raws)
    effective_config = _merge_config(
        [item.config for item in chain[:-1]], _load_json(chain[-1].config)
    )
    profile_ref = _resolve_effective_profile(
        session, chain[-1], list(reversed(chain[:-1]))
    )
    flags = _compute_inherited(
        chain[-1], list(reversed(chain[:-1])), profile_ref["profile_source"],
        own_is_root=(len(chain) == 1),
    )
    return {
        "save_path": save_path,
        "effective_tags": effective,
        "effective_config": effective_config,
        "date_basis_order": utils.normalize_date_basis_order(
            effective_config.get("date_basis_order")
        ),
        "root_name": root.name,
        "profile_id": profile_ref["profile_id"],
        "profile_group_id": profile_ref["profile_group_id"],
        "profile_source": profile_ref["profile_source"],
        "inherited": flags["inherited"],
        "overridden": flags["overridden"],
        "global": flags.get("global", {}),
        "effective": flags.get("effective", {}),
        "explicit": flags.get("explicit", []),
    }


def source_context(session, source: Source) -> dict:
    if source.folder_id is None:
        effective_config = _merge_config([], _load_json(source.config))
        profile_ref = _resolve_effective_profile(session, source, [], source.site)
        return {
            "save_path": utils.compute_base_path(""),
            "effective_tags": utils.resolve_effective_tags(source.tag_list, []),
            "effective_config": effective_config,
            "date_basis_order": utils.normalize_date_basis_order(
                effective_config.get("date_basis_order")
            ),
            "root_name": "",
            "profile_id": profile_ref["profile_id"],
            "profile_group_id": profile_ref["profile_group_id"],
            "profile_source": profile_ref["profile_source"],
            "inherited": {"cyc": False, "log_level": False, "profile": False, "config": False, "save_path": False},
            "overridden": {
                "cyc": int(source.cyc_option or 0) != int(item_defaults().get("cyc_option") or 0),
                "log_level": (source.log_level or "INFO").upper() != (item_defaults().get("log_level") or "INFO").upper(),
                "profile": source.profile_id is not None or source.profile_group_id is not None,
                "config": bool(_load_json(source.config)),
                "save_path": False,
            },
            "global": {},
            "effective": {
                "cyc_option": int(item_defaults().get("cyc_option") or 0),
                "log_level": (item_defaults().get("log_level") or "INFO").upper(),
            },
            "explicit": sorted(explicit_fields(source)),
        }
    chain = folder_chain(session, source.folder_id)
    root = chain[0]
    descendants = [item.name for item in chain[1:]]
    save_path = utils.join_save_path(root.name, descendants)
    effective = utils.resolve_effective_tags(source.tag_list, [item.tag_list for item in chain])
    effective_config = _merge_config(
        [item.config for item in chain], _load_json(source.config)
    )
    profile_ref = _resolve_effective_profile(
        session, source, list(reversed(chain)), source.site
    )
    flags = _compute_inherited(
        source, list(reversed(chain)), profile_ref["profile_source"], own_is_root=False
    )
    return {
        "save_path": save_path,
        "effective_tags": effective,
        "effective_config": effective_config,
        "date_basis_order": utils.normalize_date_basis_order(
            effective_config.get("date_basis_order")
        ),
        "root_name": root.name,
        "profile_id": profile_ref["profile_id"],
        "profile_group_id": profile_ref["profile_group_id"],
        "profile_source": profile_ref["profile_source"],
        "inherited": flags["inherited"],
        "overridden": flags["overridden"],
        "global": flags.get("global", {}),
        "effective": flags.get("effective", {}),
        "explicit": flags.get("explicit", []),
    }


def folder_to_dict(session, folder: Folder, counts: dict | None = None) -> dict:
    context = folder_context(session, folder.id)
    sub_count = item_count = 0
    if counts is not None:
        sub_count = counts.get(folder.id, (0, 0))[0]
        item_count = counts.get(folder.id, (0, 0))[1]
    return {
        "type": "folder",
        "id": folder.id,
        "name": folder.name,
        "parent_id": folder.parent_id,
        "config": _load_json(folder.config),
        "cyc_option": folder.cyc_option or 0,
        "cyc": folder.cyc or 1,
        "log_level": folder.log_level or "INFO",
        "memo": folder.memo,
        "tag_list": utils.tag_names(folder.tag_list),
        "effective_tags": context["effective_tags"],
        "effective_tag_list": [tag["name"] for tag in context["effective_tags"]],
        "effective_config": context["effective_config"],
        "date_basis_order": context["date_basis_order"],
        "save_path": context["save_path"],
        "profile_group_id": folder.profile_group_id,
        "profile_id": folder.profile_id,
        "effective_profile_group_id": context.get("profile_group_id"),
        "effective_profile_id": context.get("profile_id"),
        "profile_source": context.get("profile_source"),
        "inherited": context.get("inherited", {}),
        "overridden": context.get("overridden", {}),
        "global": context.get("global", {}),
        "effective_columns": context.get("effective", {}),
        "explicit_fields": context.get("explicit", []),
        "order_index": folder.order_index,
        "sub_folder_count": sub_count,
        "item_count": item_count,
        "created_at": _iso(folder.created_at),
    }


def source_to_dict(session, source: Source) -> dict:
    context = source_context(session, source)
    download_directory = utils.build_download_directory(
        context["save_path"],
        {"site": source.site, "key": source.key, "name": source.name or source.url, "url": source.url},
    )
    return {
        "type": "source",
        "id": source.id,
        "name": source.name,
        "url": source.url,
        "folder_id": source.folder_id,
        "site": source.site,
        "key": source.key,
        "config": _load_json(source.config),
        "cyc_option": source.cyc_option or 0,
        "cyc": source.cyc or 1,
        "log_level": source.log_level or "INFO",
        "memo": source.memo,
        "status": source.status or "idle",
        "tag_list": utils.tag_names(source.tag_list),
        "effective_tags": context["effective_tags"],
        "effective_tag_list": [tag["name"] for tag in context["effective_tags"]],
        "effective_config": context["effective_config"],
        "date_basis_order": context["date_basis_order"],
        "save_path": context["save_path"],
        "profile_group_id": source.profile_group_id,
        "profile_id": source.profile_id,
        "effective_profile_group_id": context.get("profile_group_id"),
        "effective_profile_id": context.get("profile_id"),
        "profile_source": context.get("profile_source"),
        "inherited": context.get("inherited", {}),
        "overridden": context.get("overridden", {}),
        "global": context.get("global", {}),
        "effective_columns": context.get("effective", {}),
        "explicit_fields": context.get("explicit", []),
        "order_index": source.order_index,
        "download_directory": download_directory,
        "file_count": utils.count_directory_files(download_directory),
        "recent_file_date": _iso(source.recent_file_date),
        "recent_file_mtime": _iso(source.recent_file_mtime),
        "recent_run_at": _iso(source.recent_run_at),
        "last_attempt_at": _iso(source.last_attempt_at),
        "last_success_at": _iso(source.last_success_at),
        "oldest_err_at": _iso(source.oldest_err_at),
        "learned_upload_cycle": int(source.learned_upload_cycle or kde.DEFAULT_BASE_CYCLE_MINUTES),
        "last_new_file_at": _iso(source.last_new_file_at),
        "upload_history_count": len(kde.parse_history(source.upload_time_history)),
        "ai_state": _source_ai_state(source),
        "created_at": _iso(source.created_at),
    }


def _source_ai_state(source: Source) -> dict | None:
    if (source.cyc_option or 0) != 2:
        return None
    entries = kde.parse_history(source.upload_time_history)
    prediction = kde.predict(entries, source.learned_upload_cycle, _now())
    return {
        "label": prediction["label"],
        "next_wait": prediction["next_wait"],
        "base_cycle": prediction["base_cycle"],
        "relative_density": prediction["relative_density"],
        "history_count": prediction["history_count"],
    }


def _child_counts(session) -> dict:
    counts: dict[int, list[int]] = {}
    for folder in session.execute(select(Folder)).scalars().all():
        if folder.parent_id is not None:
            counts.setdefault(folder.parent_id, [0, 0])[0] += 1
    for source in session.execute(select(Source)).scalars().all():
        if source.folder_id is not None:
            counts.setdefault(source.folder_id, [0, 0])[1] += 1
    return {key: (value[0], value[1]) for key, value in counts.items()}


def list_explorer(session, folder_id):
    folders = (
        session.execute(
            select(Folder).where(Folder.parent_id == folder_id).order_by(*_order_clause(Folder))
        )
        .scalars()
        .all()
    )
    sources = (
        session.execute(
            select(Source).where(Source.folder_id == folder_id).order_by(*_order_clause(Source))
        )
        .scalars()
        .all()
    )
    counts = _child_counts(session)
    return (
        [folder_to_dict(session, folder, counts) for folder in folders],
        [source_to_dict(session, source) for source in sources],
    )


def _order_clause(model):
    """수동 순서(order_index) 우선, 미지정은 뒤로, 동률은 id로 안정 정렬."""
    return (model.order_index.is_(None), model.order_index, model.id)


def breadcrumb(session, folder_id) -> list[dict]:
    chain = [{"id": None, "name": "root"}]
    for folder in folder_chain(session, folder_id):
        chain.append({"id": folder.id, "name": folder.name})
    return chain


def all_folders_flat(session) -> list[dict]:
    rows = (
        session.execute(select(Folder).order_by(*_order_clause(Folder))).scalars().all()
    )
    return [{"id": row.id, "name": row.name, "parent_id": row.parent_id} for row in rows]


def get_folder(session, folder_id) -> dict | None:
    folder = session.get(Folder, folder_id)
    if folder is None:
        return None
    return folder_to_dict(session, folder, _child_counts(session))


def create_folder(session, payload: dict) -> dict:
    defaults = item_defaults()
    folder = Folder(
        name=(payload.get("name") or "New Folder").strip(),
        parent_id=payload.get("parent_id"),
        config=_dump_json(_merge_config([defaults.get("config")], payload.get("config"))),
        cyc_option=int(payload.get("cyc_option") if payload.get("cyc_option") is not None else defaults.get("cyc_option") or 0),
        cyc=int(payload.get("cyc") or defaults.get("cyc") or 1),
        log_level=payload.get("log_level") or defaults.get("log_level") or "INFO",
        tag_list=",".join(payload.get("tag_list") or []),
        memo=payload.get("memo"),
        profile_group_id=payload.get("profile_group_id"),
        profile_id=payload.get("profile_id"),
    )
    session.add(folder)
    session.commit()
    session.refresh(folder)
    return folder_to_dict(session, folder, _child_counts(session))


def _move_real_directory(old_dir, new_dir):
    """실제 폴더를 이동한다(rename 우선, 다른 볼륨이면 복사 후 삭제)."""
    if not old_dir or not new_dir:
        return {"ok": True, "moved": False}
    old_dir = os.path.normpath(old_dir)
    new_dir = os.path.normpath(new_dir)
    if old_dir == new_dir or not os.path.isdir(old_dir):
        return {"ok": True, "moved": False}
    if os.path.exists(new_dir):
        return {"ok": False, "error": "대상 경로가 이미 존재합니다: " + new_dir}
    utils.ensure_dir(os.path.dirname(new_dir) or ".")
    try:
        os.rename(old_dir, new_dir)
        return {"ok": True, "moved": True, "method": "rename", "from": old_dir, "to": new_dir}
    except OSError:
        pass
    try:
        shutil.move(old_dir, new_dir)
        return {"ok": True, "moved": True, "method": "move", "from": old_dir, "to": new_dir}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "from": old_dir, "to": new_dir}


def move_real_directory(old_dir, new_dir):
    """아이템 실제 폴더 이동(공개 별칭). rename 우선, 실패 시 복사 후 삭제."""
    return _move_real_directory(old_dir, new_dir)


def _file_digest(path: str) -> str:
    """파일 내용 해시(중복 판정용). 읽기 실패 시 빈 문자열."""
    digest = hashlib.sha1()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def merge_directory(src, dst):
    """``src`` 의 파일을 ``dst`` 로 병합한다(하위 경로 유지).
    - 대상에 없는 파일은 이동
    - 같은 경로에 내용이 같은 파일이 있으면 중복으로 보고 원본 삭제
    - 같은 경로에 다른 내용이면 ``<name>.dup<N>`` 로 보존
    - 비게 된 폴더는 정리
    """
    if not src or not dst:
        return {"ok": True, "moved": 0, "skipped": 0, "renamed": 0, "errors": [], "removed": False}
    src_norm = os.path.normpath(src)
    dst_norm = os.path.normpath(dst)
    if src_norm == dst_norm or not os.path.isdir(src_norm):
        return {"ok": True, "moved": 0, "skipped": 0, "renamed": 0, "errors": [], "removed": False}
    utils.ensure_dir(dst_norm)
    moved = skipped = renamed = 0
    errors: list[str] = []
    for root, _dirs, files in os.walk(src_norm):
        rel = os.path.relpath(root, src_norm)
        target_root = dst_norm if rel == "." else os.path.join(dst_norm, rel)
        for name in files:
            source_path = os.path.join(root, name)
            target_path = os.path.join(target_root, name)
            try:
                if not os.path.exists(target_path):
                    utils.ensure_dir(target_root)
                    os.replace(source_path, target_path)
                    moved += 1
                    continue
                if os.path.getsize(source_path) == os.path.getsize(target_path):
                    # 크기가 같아도 내용이 다를 수 있으므로 해시로 확인(같을 때만 중복 처리)
                    if _file_digest(source_path) == _file_digest(target_path):
                        os.remove(source_path)
                        skipped += 1
                        continue
                candidate = target_path + ".dup"
                index = 1
                while os.path.exists(candidate):
                    candidate = "%s.dup%d" % (target_path, index)
                    index += 1
                os.replace(source_path, candidate)
                renamed += 1
            except OSError as exc:
                errors.append("%s: %s" % (source_path, exc))
    for root, _dirs, _files in os.walk(src_norm, topdown=False):
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass
    return {
        "ok": not errors,
        "moved": moved,
        "skipped": skipped,
        "renamed": renamed,
        "errors": errors,
        "removed": not os.path.isdir(src_norm),
    }


def update_folder(session, folder_id, payload: dict) -> dict | None:
    folder = session.get(Folder, folder_id)
    if folder is None:
        return None
    move_files = bool(payload.get("move_files"))
    move_result = None
    old_dir = None
    if move_files and ("parent_id" in payload or payload.get("name") is not None):
        old_dir = folder_context(session, folder_id)["save_path"]
    if payload.get("name") is not None:
        folder.name = str(payload["name"]).strip()
    if "parent_id" in payload:
        target = payload["parent_id"]
        if target is not None:
            target = int(target)
            if target == folder_id:
                raise ValueError("cannot move a folder into itself")
            cursor = target
            while cursor is not None:
                if cursor == folder_id:
                    raise ValueError("cannot move a folder into its descendant")
                parent = session.get(Folder, cursor)
                cursor = parent.parent_id if parent else None
        folder.parent_id = target
    if payload.get("config") is not None:
        folder.config = _dump_json(payload["config"])
    if payload.get("cyc_option") is not None:
        folder.cyc_option = int(payload["cyc_option"])
    if payload.get("cyc") is not None:
        folder.cyc = int(payload["cyc"])
    if payload.get("log_level") is not None:
        folder.log_level = payload["log_level"]
    if payload.get("tag_list") is not None:
        folder.tag_list = ",".join(payload["tag_list"])
    if "memo" in payload:
        folder.memo = payload["memo"]
    if "profile_group_id" in payload:
        target = payload["profile_group_id"]
        folder.profile_group_id = int(target) if target is not None else None
    if "profile_id" in payload:
        target = payload["profile_id"]
        folder.profile_id = int(target) if target is not None else None
    session.commit()
    session.refresh(folder)
    result = folder_to_dict(session, folder, _child_counts(session))
    if move_files and old_dir is not None:
        new_dir = folder_context(session, folder_id)["save_path"]
        move_result = _move_real_directory(old_dir, new_dir)
        result["move"] = move_result
        if not move_result.get("ok"):
            raise ValueError("폴더 이동 실패: " + str(move_result.get("error")))
    return result


def delete_folder(session, folder_id) -> bool:
    folder = session.get(Folder, folder_id)
    if folder is None:
        return False
    session.query(ReactiveRule).filter(
        ReactiveRule.target_folder_id == folder_id
    ).update({"target_folder_id": None})
    session.delete(folder)
    session.commit()
    return True


def get_source(session, source_id) -> dict | None:
    source = session.get(Source, source_id)
    if source is None:
        return None
    return source_to_dict(session, source)


def create_source(session, payload: dict) -> dict:
    defaults = item_defaults()
    source = Source(
        name=(payload.get("name") or "").strip(),
        url=(payload.get("url") or "").strip(),
        folder_id=payload.get("folder_id"),
        site=payload.get("site"),
        key=payload.get("key"),
        config=_dump_json(_merge_config([defaults.get("config")], payload.get("config"))),
        cyc_option=int(payload.get("cyc_option") if payload.get("cyc_option") is not None else defaults.get("cyc_option") or 0),
        cyc=int(payload.get("cyc") or defaults.get("cyc") or 1),
        log_level=payload.get("log_level") or defaults.get("log_level") or "INFO",
        tag_list=",".join(payload.get("tag_list") or []),
        memo=payload.get("memo"),
        status=payload.get("status") or "idle",
        recent_file_date=parse_dt(payload.get("recent_file_date")),
        recent_run_at=parse_dt(payload.get("recent_run_at")),
        oldest_err_at=parse_dt(payload.get("oldest_err_at")),
        profile_group_id=payload.get("profile_group_id"),
        profile_id=payload.get("profile_id"),
        learned_upload_cycle=int(
            payload.get("learned_upload_cycle")
            or max(kde.DEFAULT_BASE_CYCLE_MINUTES, int(payload.get("cyc") or 1) * 60)
        ),
        upload_time_history="[]",
    )
    if not source.name:
        source.name = source.url
    session.add(source)
    session.commit()
    session.refresh(source)
    return source_to_dict(session, source)


def update_source(session, source_id, payload: dict) -> dict | None:
    source = session.get(Source, source_id)
    if source is None:
        return None
    move_files = bool(payload.get("move_files"))
    old_dir = None
    if move_files:
        ctx = source_context(session, source)
        old_dir = utils.build_download_directory(
            ctx["save_path"],
            {"site": source.site, "key": source.key, "name": source.name or source.url, "url": source.url},
        )
    if payload.get("name") is not None:
        source.name = str(payload["name"]).strip()
    if payload.get("url") is not None:
        source.url = str(payload["url"]).strip()
    if "folder_id" in payload:
        target = payload["folder_id"]
        source.folder_id = int(target) if target is not None else None
    if payload.get("site") is not None:
        source.site = payload["site"]
    if payload.get("key") is not None:
        source.key = payload["key"]
    if payload.get("config") is not None:
        source.config = _dump_json(payload["config"])
    if payload.get("cyc_option") is not None:
        source.cyc_option = int(payload["cyc_option"])
    if payload.get("cyc") is not None:
        source.cyc = int(payload["cyc"])
    if payload.get("log_level") is not None:
        source.log_level = payload["log_level"]
    if payload.get("tag_list") is not None:
        source.tag_list = ",".join(payload["tag_list"])
    if "memo" in payload:
        source.memo = payload["memo"]
    if payload.get("status") is not None:
        source.status = payload["status"]
    if payload.get("recent_file_date") is not None:
        source.recent_file_date = parse_dt(payload["recent_file_date"])
    if payload.get("recent_run_at") is not None:
        source.recent_run_at = parse_dt(payload["recent_run_at"])
    if payload.get("oldest_err_at") is not None:
        source.oldest_err_at = parse_dt(payload["oldest_err_at"])
    if "profile_group_id" in payload:
        target = payload["profile_group_id"]
        source.profile_group_id = int(target) if target is not None else None
    if "profile_id" in payload:
        target = payload["profile_id"]
        source.profile_id = int(target) if target is not None else None
    if payload.get("learned_upload_cycle") is not None:
        source.learned_upload_cycle = max(1, int(payload["learned_upload_cycle"]))
    if not source.name:
        source.name = source.url
    session.commit()
    session.refresh(source)
    result = source_to_dict(session, source)
    if move_files and old_dir is not None:
        new_dir = result.get("download_directory")
        move_result = _move_real_directory(old_dir, new_dir)
        result["move"] = move_result
        if not move_result.get("ok"):
            raise ValueError("소스 폴더 이동 실패: " + str(move_result.get("error")))
    return result


def delete_source(session, source_id) -> bool:
    source = session.get(Source, source_id)
    if source is None:
        return False
    session.delete(source)
    session.commit()
    return True


def move_item(session, item_type: str, item_id: int, target_folder_id):
    if target_folder_id is not None:
        target_folder_id = int(target_folder_id)
    if item_type == "source":
        return update_source(session, item_id, {"folder_id": target_folder_id})
    if item_type == "folder":
        return update_folder(session, item_id, {"parent_id": target_folder_id})
    raise ValueError("unknown item type: " + str(item_type))


def apply_batch_fields(session, item_type: str, item_id: int, fields: dict) -> None:
    allowed = {
        "config",
        "cyc_option",
        "cyc",
        "log_level",
        "tag_list",
        "memo",
        "status",
        "site",
        "key",
        "profile_group_id",
    }
    payload = {key: value for key, value in fields.items() if key in allowed}
    if "explicit_fields" in fields:
        item = session.get(Source if item_type == "source" else Folder, item_id)
        if item is not None:
            config = _load_json(item.config) or {}
            marks = _explicit_set(config)
            raw = fields.get("explicit_fields")
            if isinstance(raw, dict):
                for name, flag in raw.items():
                    if flag:
                        marks.add(str(name))
                    else:
                        marks.discard(str(name))
            elif raw is not None:
                marks = {str(mark) for mark in raw}
            if marks:
                config[EXPLICIT_KEY] = sorted(marks)
            else:
                config.pop(EXPLICIT_KEY, None)
            payload["config"] = config
    if not payload:
        return
    if item_type == "source":
        update_source(session, item_id, payload)
    else:
        update_folder(session, item_id, payload)


def expand_batch_items(session, items, depth=None) -> list[dict]:
    """일괄 작업 대상 확장.

    depth=None  → 선택한 항목만
    depth=0     → 선택한 항목 + 모든 하위(재귀)
    depth=n(>0) → 선택한 항목 + n단계 하위
    """
    base = []
    for item in items or []:
        item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        item_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        if item_type in ("folder", "source") and item_id is not None:
            base.append({"type": item_type, "id": int(item_id)})
    if depth is None:
        return base

    collected = {(item["type"], item["id"]): item for item in base}
    frontier = list(base)
    level = 0
    while frontier and (int(depth) == 0 or level < int(depth)):
        level += 1
        nxt = []
        for item in frontier:
            if item["type"] != "folder":
                continue
            children = (
                session.execute(select(Folder).where(Folder.parent_id == item["id"]))
                .scalars()
                .all()
            )
            for child in children:
                key = ("folder", child.id)
                if key not in collected:
                    collected[key] = {"type": "folder", "id": child.id}
                    nxt.append(collected[key])
            sources = (
                session.execute(select(Source).where(Source.folder_id == item["id"]))
                .scalars()
                .all()
            )
            for source in sources:
                key = ("source", source.id)
                if key not in collected:
                    collected[key] = {"type": "source", "id": source.id}
                    nxt.append(collected[key])
        frontier = nxt
    return list(collected.values())


# ---------------------------------------------------------------------------
# 수동 순서 (DnD 재정렬) — 번호가 꼬이지 않도록 스코프 전체를 0..n-1로 재부여
# ---------------------------------------------------------------------------


def _reindex_scope(session, rows, ordered_ids) -> int:
    """주어진 순서를 우선 적용하고, 누락된 형제는 기존 순서로 뒤에 append한 뒤
    0..n-1 연속 인덱스를 부여한다(중복/누락/gap 원천 차단)."""
    by_id = {row.id: row for row in rows}
    ordered: list[int] = []
    seen: set[int] = set()
    for raw in ordered_ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value in by_id and value not in seen:
            ordered.append(value)
            seen.add(value)
    remainder = [
        row.id
        for row in sorted(
            rows,
            key=lambda row: (
                row.order_index is None,
                row.order_index if row.order_index is not None else 0,
                row.id,
            ),
        )
        if row.id not in seen
    ]
    final = ordered + remainder
    for index, row_id in enumerate(final):
        by_id[row_id].order_index = index
    return len(final)


def reorder_children(session, item_type, parent_id, ordered_ids) -> int:
    if item_type == "folder":
        rows = (
            session.execute(select(Folder).where(Folder.parent_id == parent_id))
            .scalars()
            .all()
        )
    elif item_type == "source":
        rows = (
            session.execute(select(Source).where(Source.folder_id == parent_id))
            .scalars()
            .all()
        )
    else:
        raise ValueError("unknown item type: " + str(item_type))
    count = _reindex_scope(session, rows, ordered_ids)
    session.commit()
    return count


def reorder_profiles(session, ordered_ids) -> int:
    rows = session.execute(select(Profile)).scalars().all()
    count = _reindex_scope(session, rows, ordered_ids)
    session.commit()
    return count


def reorder_profile_groups(session, ordered_ids) -> int:
    rows = session.execute(select(ProfileGroup)).scalars().all()
    count = _reindex_scope(session, rows, ordered_ids)
    session.commit()
    return count


def mark_run_started(session, source_id, when=None) -> dict | None:
    """수집 시도 시작: 시도 시각만 갱신(성공 기준시점 recent_run_at은 보존)."""
    source = session.get(Source, source_id)
    if source is None:
        return None
    source.last_attempt_at = when or _now()
    source.status = "running"
    session.commit()
    session.refresh(source)
    return source_to_dict(session, source)


def mark_source_success(session, source_id, mtime=None, when=None) -> dict | None:
    """수집 성공: 성공 기준시점(recent_run_at)을 이때 갱신한다."""
    source = session.get(Source, source_id)
    if source is None:
        return None
    now = when or _now()
    source.status = "done"
    source.last_success_at = now
    source.recent_run_at = mtime or now
    if mtime is not None:
        source.recent_file_mtime = mtime
        source.recent_file_date = mtime
    session.commit()
    session.refresh(source)
    return source_to_dict(session, source)


def mark_source_failure(session, source_id, errors) -> dict | None:
    source = session.get(Source, source_id)
    if source is None:
        return None
    now = _utcnow()
    source.status = "error"
    if source.oldest_err_at is None:
        source.oldest_err_at = now
    entries = errors or [{"code": None, "message": "gallery-dl exited with a non-zero status"}]
    for entry in entries:
        session.add(
            GdlErrorLog(
                source_id=source_id,
                error_code=entry.get("code"),
                message=entry.get("message") or "",
                created_at=now,
            )
        )
    session.commit()
    session.refresh(source)
    return source_to_dict(session, source)


def list_all_sources(session) -> list[dict]:
    rows = session.execute(select(Source).order_by(Source.id)).scalars().all()
    return [source_to_dict(session, row) for row in rows]


def record_upload_event(session, source_id, when=None) -> dict | None:
    """새 파일 발견 시각을 KDE 이력에 누적하고 마지막 업로드 시각을 갱신한다."""
    source = session.get(Source, source_id)
    if source is None:
        return None
    moment = when or _now()
    entries = kde.parse_history(source.upload_time_history)
    entries.append(
        {"day": moment.weekday(), "hour": round(moment.hour + moment.minute / 60.0, 3)}
    )
    if len(entries) > kde.HISTORY_LIMIT:
        entries = entries[-kde.HISTORY_LIMIT:]
    source.upload_time_history = kde.dump_history(entries)
    source.last_new_file_at = moment
    session.commit()
    session.refresh(source)
    return source_to_dict(session, source)


def _history_has_near(entries, day, hour, tolerance_hours=0.25) -> bool:
    for entry in entries:
        if int(entry.get("day", -1)) != int(day):
            continue
        if abs(float(entry.get("hour", 0.0)) - float(hour)) <= tolerance_hours:
            return True
    return False


def record_upload_events(session, source_id, moments, replace=False) -> dict | None:
    """여러 업로드 시각을 KDE 이력에 병합(중복 근접 시각 스킵)."""
    source = session.get(Source, source_id)
    if source is None:
        return None
    entries = [] if replace else kde.parse_history(source.upload_time_history)
    added = 0
    last = (
        None
        if replace
        else _naive(source.last_new_file_at)
    )
    for moment in moments or []:
        if moment is None:
            continue
        moment = _naive(moment)
        if moment is None:
            continue
        day = moment.weekday()
        hour = round(moment.hour + moment.minute / 60.0, 3)
        if _history_has_near(entries, day, hour):
            continue
        entries.append({"day": day, "hour": hour})
        added += 1
        if last is None or moment > last:
            last = moment
    if len(entries) > kde.HISTORY_LIMIT:
        entries = sorted(entries, key=lambda e: (e.get("day", 0), e.get("hour", 0.0)))
        entries = entries[-kde.HISTORY_LIMIT:]
    source.upload_time_history = kde.dump_history(entries)
    if last is not None:
        source.last_new_file_at = last
    session.commit()
    session.refresh(source)
    result = source_to_dict(session, source)
    result["_added"] = added
    return result


def backfill_source_learning(session, source_id, cluster_minutes=5.0, replace=False) -> dict | None:
    """디렉터리 스캔(정보 json date/mtime)으로 과거 이력을 재학습한다."""
    source = session.get(Source, source_id)
    if source is None:
        return None
    directory = utils.build_download_directory(
        source_context(session, source)["save_path"],
        {"site": source.site, "key": source.key, "name": source.name or source.url, "url": source.url},
    )
    moments = utils.list_media_events(directory, cluster_minutes)
    updated = record_upload_events(session, source_id, moments, replace=replace)
    if updated is None:
        return None
    return {
        "source_id": source_id,
        "directory": directory,
        "scanned": len(moments),
        "added": updated.get("_added", 0),
        "history_count": updated.get("upload_history_count", 0),
        "learned_upload_cycle": updated.get("learned_upload_cycle"),
    }


def set_source_cycle(session, source_id, cycle) -> dict | None:
    source = session.get(Source, source_id)
    if source is None:
        return None
    source.learned_upload_cycle = max(1, int(cycle))
    session.commit()
    session.refresh(source)
    return source_to_dict(session, source)


# ---------------------------------------------------------------------------
# Profile / ProfileGroup management
# ---------------------------------------------------------------------------


def profile_to_dict(session, profile: Profile) -> dict:
    now = _now()
    cooldown = _naive(profile.cooldown_until)
    cooldown_seconds = int((cooldown - now).total_seconds()) if cooldown and cooldown > now else 0
    capacity = int(profile.learned_capacity or 0)
    usage = int(profile.recent_usage_count or 0)
    ratio = 0.0
    if capacity > 0:
        ratio = min(1.0, max(0.0, usage / float(capacity)))
    observations = int(profile.observations or 0)
    probe_at = _naive(profile.cooldown_probe_at)
    return {
        "id": profile.id,
        "name": profile.name,
        "site": profile.site,
        "context_dir": profile.context_dir,
        "proxy_url": profile.proxy_url,
        "account_key": profile.account_key,
        "account_url": profile.account_url,
        "status": profile.status or DEFAULT_PROFILE_STATUS,
        "is_site_default": bool(profile.is_site_default),
        "cooldown_until": _iso(profile.cooldown_until),
        "cooldown_seconds": max(0, cooldown_seconds),
        "in_cooldown": cooldown_seconds > 0,
        "learned_capacity": capacity,
        "learned_cooldown_minutes": int(profile.learned_cooldown_minutes or 0),
        "recent_usage_count": usage,
        "remaining_capacity": max(0, capacity - usage),
        "usage_ratio": ratio,
        "consecutive_errors": int(profile.consecutive_errors or 0),
        "last_error_at": _iso(profile.last_error_at),
        "last_stable_at": _iso(profile.last_stable_at),
        "observations": observations,
        "confidence": round(min(1.0, observations / 20.0), 2),
        "learned_rate_per_hour": round(float(profile.learned_rate_per_hour or 0.0), 2),
        "success_streak": int(profile.success_streak or 0),
        "capacity_lo": int(profile.capacity_lo if profile.capacity_lo is not None else capacity),
        "capacity_hi": int(profile.capacity_hi if profile.capacity_hi is not None else capacity),
        "observed_recovery_minutes": int(profile.observed_recovery_minutes or 0),
        "last_error_kind": profile.last_error_kind,
        "probe_at": _iso(profile.cooldown_probe_at),
        "probe_pending": bool(probe_at is not None and cooldown_seconds <= 0),
        "window_started_at": _iso(profile.window_started_at),
        "group_ids": [group.id for group in profile.groups],
        "order_index": profile.order_index,
        "created_at": _iso(profile.created_at),
    }


def list_profiles(session) -> list[dict]:
    rows = (
        session.execute(select(Profile).order_by(*_order_clause(Profile))).scalars().all()
    )
    return [profile_to_dict(session, row) for row in rows]


def get_profile(session, profile_id) -> dict | None:
    profile = session.get(Profile, profile_id)
    if profile is None:
        return None
    return profile_to_dict(session, profile)


def create_profile(session, payload: dict) -> dict:
    profile = Profile(
        name=(payload.get("name") or "New Profile").strip(),
        site=payload.get("site"),
        context_dir=(payload.get("context_dir") or "").strip(),
        proxy_url=payload.get("proxy_url") or None,
        account_key=payload.get("account_key") or None,
        account_url=payload.get("account_url") or None,
        status=payload.get("status") or DEFAULT_PROFILE_STATUS,
        learned_capacity=int(payload.get("learned_capacity") or DEFAULT_LEARNED_CAPACITY),
        learned_cooldown_minutes=int(
            payload.get("learned_cooldown_minutes") or DEFAULT_LEARNED_COOLDOWN_MINUTES
        ),
    )
    session.add(profile)
    session.commit()
    session.refresh(profile)
    profile.window_started_at = _now()
    profile.capacity_lo = profile.learned_capacity
    profile.capacity_hi = profile.learned_capacity
    if not profile.context_dir:
        profile.context_dir = utils.default_profile_dir(profile.id)
    session.commit()
    session.refresh(profile)
    return profile_to_dict(session, profile)


def update_profile(session, profile_id, payload: dict) -> dict | None:
    profile = session.get(Profile, profile_id)
    if profile is None:
        return None
    if payload.get("name") is not None:
        profile.name = str(payload["name"]).strip()
    if "site" in payload:
        profile.site = payload["site"]
    if payload.get("context_dir") is not None:
        profile.context_dir = str(payload["context_dir"]).strip()
    if "proxy_url" in payload:
        profile.proxy_url = payload["proxy_url"] or None
    if "account_key" in payload:
        profile.account_key = payload["account_key"] or None
    if "account_url" in payload:
        profile.account_url = payload["account_url"] or None
    if payload.get("status") is not None:
        profile.status = payload["status"]
    if payload.get("learned_capacity") is not None:
        profile.learned_capacity = max(1, int(payload["learned_capacity"]))
    if payload.get("learned_cooldown_minutes") is not None:
        profile.learned_cooldown_minutes = max(1, int(payload["learned_cooldown_minutes"]))
    if payload.get("recent_usage_count") is not None:
        profile.recent_usage_count = max(0, int(payload["recent_usage_count"]))
    if payload.get("consecutive_errors") is not None:
        profile.consecutive_errors = max(0, int(payload["consecutive_errors"]))
    if payload.get("learned_rate_per_hour") is not None:
        profile.learned_rate_per_hour = max(0.0, float(payload["learned_rate_per_hour"]))
    if "cooldown_until" in payload:
        profile.cooldown_until = parse_dt(payload["cooldown_until"])
    if "group_ids" in payload:
        _assign_profile_groups(session, profile, payload["group_ids"])
    if payload.get("is_site_default") is not None:
        _mark_site_default(session, profile, bool(payload["is_site_default"]))
    session.commit()
    session.refresh(profile)
    return profile_to_dict(session, profile)


def _mark_site_default(session, profile: Profile, value: bool) -> None:
    """사이트당 1개만 디폴트가 되도록 같은 사이트의 다른 프로파일을 해제."""
    norm = site_url.normalize_site(profile.site)
    if value and norm:
        for other in session.execute(select(Profile)).scalars().all():
            if other.id != profile.id and site_url.normalize_site(other.site) == norm:
                other.is_site_default = False
    profile.is_site_default = bool(value and norm)


def set_site_default(session, profile_id, value=True) -> dict | None:
    profile = session.get(Profile, profile_id)
    if profile is None:
        return None
    _mark_site_default(session, profile, bool(value))
    session.commit()
    session.refresh(profile)
    return profile_to_dict(session, profile)


def delete_profile(session, profile_id) -> bool:
    profile = session.get(Profile, profile_id)
    if profile is None:
        return False
    session.query(Folder).filter(Folder.profile_id == profile_id).update(
        {"profile_id": None}
    )
    session.query(Source).filter(Source.profile_id == profile_id).update(
        {"profile_id": None}
    )
    session.delete(profile)
    session.commit()
    return True


def _load_profiles(session, profile_ids) -> list[Profile]:
    profiles: list[Profile] = []
    for raw in profile_ids or []:
        profile = session.get(Profile, int(raw))
        if profile is not None:
            profiles.append(profile)
    return profiles


def _assign_profile_groups(session, profile: Profile, group_ids) -> None:
    profile.groups = _load_groups(session, group_ids)


def _load_groups(session, group_ids) -> list[ProfileGroup]:
    groups: list[ProfileGroup] = []
    for raw in group_ids or []:
        group = session.get(ProfileGroup, int(raw))
        if group is not None:
            groups.append(group)
    return groups


def profile_group_to_dict(session, group: ProfileGroup) -> dict:
    now = _now()
    cooldown = _naive(group.cooldown_until)
    cooldown_seconds = int((cooldown - now).total_seconds()) if cooldown and cooldown > now else 0
    return {
        "id": group.id,
        "name": group.name,
        "site": group.site,
        "account_key": group.account_key,
        "account_url": group.account_url,
        "profile_ids": [profile.id for profile in group.profiles],
        "profiles": [profile_to_dict(session, profile) for profile in group.profiles],
        "cooldown_until": _iso(group.cooldown_until),
        "cooldown_seconds": max(0, cooldown_seconds),
        "in_cooldown": cooldown_seconds > 0,
        "concurrent_limit": int(group.concurrent_limit or 1),
        "pace_seconds": round(float(group.pace_seconds or 0.0), 2),
        "shared_limit": bool(group.shared_limit) if group.shared_limit is not None else True,
        "order_index": group.order_index,
        "created_at": _iso(group.created_at),
    }


def list_profile_groups(session) -> list[dict]:
    rows = (
        session.execute(select(ProfileGroup).order_by(*_order_clause(ProfileGroup)))
        .scalars()
        .all()
    )
    return [profile_group_to_dict(session, row) for row in rows]


def get_profile_group(session, group_id) -> dict | None:
    group = session.get(ProfileGroup, group_id)
    if group is None:
        return None
    return profile_group_to_dict(session, group)


def create_profile_group(session, payload: dict) -> dict:
    group = ProfileGroup(
        name=(payload.get("name") or "New Group").strip(),
        site=payload.get("site"),
        account_key=payload.get("account_key") or None,
        account_url=payload.get("account_url") or None,
        concurrent_limit=max(1, int(payload.get("concurrent_limit") or 1)),
        pace_seconds=max(0.0, float(payload.get("pace_seconds") or 0.0)),
        shared_limit=bool(payload.get("shared_limit", True)),
    )
    if "cooldown_until" in payload:
        group.cooldown_until = parse_dt(payload.get("cooldown_until"))
    group.profiles = _load_profiles(session, payload.get("profile_ids"))
    session.add(group)
    session.commit()
    session.refresh(group)
    return profile_group_to_dict(session, group)


def update_profile_group(session, group_id, payload: dict) -> dict | None:
    group = session.get(ProfileGroup, group_id)
    if group is None:
        return None
    if payload.get("name") is not None:
        group.name = str(payload["name"]).strip()
    if "site" in payload:
        group.site = payload["site"]
    if "account_key" in payload:
        group.account_key = payload["account_key"] or None
    if "account_url" in payload:
        group.account_url = payload["account_url"] or None
    if payload.get("concurrent_limit") is not None:
        group.concurrent_limit = max(1, int(payload["concurrent_limit"]))
    if payload.get("pace_seconds") is not None:
        group.pace_seconds = max(0.0, float(payload["pace_seconds"]))
    if payload.get("shared_limit") is not None:
        group.shared_limit = bool(payload["shared_limit"])
    if "cooldown_until" in payload:
        group.cooldown_until = parse_dt(payload.get("cooldown_until"))
    if "profile_ids" in payload:
        group.profiles = _load_profiles(session, payload["profile_ids"])
    session.commit()
    session.refresh(group)
    return profile_group_to_dict(session, group)


def reset_profile_group(session, group_id) -> dict | None:
    group = session.get(ProfileGroup, group_id)
    if group is None:
        return None
    group.cooldown_until = None
    group.pace_seconds = 0.0
    session.commit()
    session.refresh(group)
    return profile_group_to_dict(session, group)


def delete_profile_group(session, group_id) -> bool:
    group = session.get(ProfileGroup, group_id)
    if group is None:
        return False
    session.delete(group)
    session.commit()
    return True


# ---------------------------------------------------------------------------
# Organic adaptive learning & failover rotation
# ---------------------------------------------------------------------------


def _clamp(value, low, high):
    return max(low, min(high, value))


def _adaptive_alpha(observations) -> float:
    n = max(0, int(observations or 0))
    return _clamp(1.0 / (n + 2), CAPACITY_ALPHA_MIN, CAPACITY_ALPHA_MAX)


def _profile_usage(profile: Profile) -> int:
    return int(profile.recent_usage_count or 0)


def _profile_capacity(profile: Profile) -> int:
    return int(profile.learned_capacity or DEFAULT_LEARNED_CAPACITY)


def _profile_cooling(profile: Profile, now: datetime) -> bool:
    cooldown = _naive(profile.cooldown_until)
    return bool(cooldown and cooldown > now)


def _profile_is_probe(profile: Profile, now: datetime) -> bool:
    probe = _naive(profile.cooldown_probe_at)
    if probe is None:
        return False
    return probe <= now and not _profile_cooling(profile, now)


def _maybe_reset_window(session, profile: Profile, now: datetime) -> bool:
    """사이트 카운팅 윈도우가 지났으면 사용량을 리셋한다(에러와 무관)."""
    window = _naive(profile.window_started_at)
    if window is None:
        profile.window_started_at = now
        return True
    if (now - window).total_seconds() >= USAGE_WINDOW_MINUTES * 60:
        profile.recent_usage_count = 0
        profile.window_started_at = now
        return True
    return False


def _record_learning_event(
    session, profile, *, source_id=None, kind="", usage_delta=0, capacity_before=0,
    capacity_after=0, cooldown_minutes=0, rate_per_hour=0.0, started_at=None, ended_at=None,
) -> None:
    session.add(
        ProfileLearningEvent(
            profile_id=profile.id,
            source_id=source_id,
            kind=kind or "",
            usage_delta=int(usage_delta or 0),
            capacity_before=int(capacity_before or 0),
            capacity_after=int(capacity_after or 0),
            cooldown_minutes=int(cooldown_minutes or 0),
            rate_per_hour=float(rate_per_hour or 0.0),
            started_at=started_at,
            ended_at=ended_at,
            created_at=_now(),
        )
    )


def acquire_profile_for_group(session, group_id, site=None) -> dict | None:
    """선제적 한도 회피 (Proactive Selection, 그룹 공유 한도 인지).

    - 그룹 쿨다운(사이트 공유 한도) 중이면 None.
    - status == '정상', 쿨다운 아님, usage < capacity 계정 우선.
    - 모두 한도 근접 시 사용률(usage/capacity)이 가장 낮은 계정으로 우회.
    - 쿨다운이 끝난 계정은 probe로 표시하여 결과로 즉시 보정한다.
    """
    if group_id is None:
        return None
    group = session.get(ProfileGroup, group_id)
    if group is None:
        return None
    now = _now()
    group_cooldown = _naive(group.cooldown_until)
    if group_cooldown and group_cooldown > now:
        return None

    changed = False
    for profile in group.profiles:
        if _maybe_reset_window(session, profile, now):
            changed = True
    if changed:
        session.commit()

    normal = [
        profile
        for profile in group.profiles
        if (profile.status or DEFAULT_PROFILE_STATUS) == DEFAULT_PROFILE_STATUS
    ]
    if not normal:
        return None
    available = [
        profile
        for profile in normal
        if not _profile_cooling(profile, now)
        and _profile_usage(profile) < _profile_capacity(profile)
    ]
    if not available:
        not_cooling = [profile for profile in normal if not _profile_cooling(profile, now)]
        if not_cooling:
            available = not_cooling
        else:
            return None
    available.sort(
        key=lambda profile: (
            _profile_usage(profile) / float(max(1, _profile_capacity(profile))),
            _profile_usage(profile),
            profile.id,
        )
    )
    chosen = available[0]
    result = profile_to_dict(session, chosen)
    result["is_probe"] = _profile_is_probe(chosen, now)
    return result


def seconds_until_next_available(session, group_id) -> float | None:
    if group_id is None:
        return None
    group = session.get(ProfileGroup, group_id)
    if group is None:
        return None
    now = _now()
    best: float | None = None
    group_cooldown = _naive(group.cooldown_until)
    if group_cooldown and group_cooldown > now:
        best = (group_cooldown - now).total_seconds()
    for profile in group.profiles:
        if (profile.status or DEFAULT_PROFILE_STATUS) != DEFAULT_PROFILE_STATUS:
            continue
        cooldown = _naive(profile.cooldown_until)
        if cooldown and cooldown > now:
            wait = (cooldown - now).total_seconds()
            best = wait if best is None else min(best, wait)
    return best


def acquire_single_profile(session, profile_id) -> dict | None:
    """단일 프로파일 사용 가능 여부 판정 (회전 없음).

    - 상태가 '정상'이고 쿨다운이 아니면 반환한다(한도 근접이어도 사용).
    - 잠금/차단이거나 쿨다운 중이면 None.
    """
    profile = session.get(Profile, profile_id)
    if profile is None:
        return None
    now = _now()
    if _maybe_reset_window(session, profile, now):
        session.commit()
    if (profile.status or DEFAULT_PROFILE_STATUS) != DEFAULT_PROFILE_STATUS:
        return None
    if _profile_cooling(profile, now):
        return None
    result = profile_to_dict(session, profile)
    result["is_probe"] = _profile_is_probe(profile, now)
    return result


def single_profile_wait_seconds(session, profile_id) -> float | None:
    """단일 프로파일의 쿨다운 해제까지 남은 시간(초). 쿨다운 아니면 None."""
    if profile_id is None:
        return None
    profile = session.get(Profile, profile_id)
    if profile is None:
        return None
    if (profile.status or DEFAULT_PROFILE_STATUS) != DEFAULT_PROFILE_STATUS:
        return None
    now = _now()
    cooldown = _naive(profile.cooldown_until)
    if cooldown and cooldown > now:
        return (cooldown - now).total_seconds()
    return None


def group_profile_count(session, group_id) -> int:
    if group_id is None:
        return 0
    group = session.get(ProfileGroup, group_id)
    if group is None:
        return 0
    return len(group.profiles)


def distinct_sites(session) -> list[str]:
    """소스/프로파일/그룹에 등록된 사이트 값의 합집합 (자동완성 '사용됨')."""
    sites: set[str] = set()
    queries = (
        select(Source.site).where(Source.site.is_not(None)),
        select(Profile.site).where(Profile.site.is_not(None)),
        select(ProfileGroup.site).where(ProfileGroup.site.is_not(None)),
    )
    for statement in queries:
        for (value,) in session.execute(statement).all():
            text = (value or "").strip()
            if text:
                sites.add(text)
    return sorted(sites, key=lambda item: item.lower())


def get_group_runtime(session, group_id) -> dict | None:
    if group_id is None:
        return None
    group = session.get(ProfileGroup, group_id)
    if group is None:
        return None
    data = profile_group_to_dict(session, group)
    return {
        "id": data["id"],
        "name": data["name"],
        "site": data["site"],
        "shared_limit": data["shared_limit"],
        "concurrent_limit": data["concurrent_limit"],
        "pace_seconds": data["pace_seconds"],
        "cooldown_until": data["cooldown_until"],
        "cooldown_seconds": data["cooldown_seconds"],
        "in_cooldown": data["in_cooldown"],
    }


def profile_apply_rate_limit(session, profile_id, kind="rate_limit", source_id=None) -> dict | None:
    """한도 초과(Code 88/429) 적응 학습 (Reactive Learning).

    - 신뢰도 기반 EMA로 안전 한도 하향(관측이 쌓일수록 보수적으로 완만).
    - 쿨다운 = 학습된 base * 지수 백오프(상한 8x) * 지터(±15%).
    - 반복 위반 + 즉시 초과(usage==0)면 base 자체를 상향 학습.
    - recent_usage_count 윈도우 리셋 및 probe 예약.
    """
    profile = session.get(Profile, profile_id)
    if profile is None:
        return None
    now = _now()
    usage = _profile_usage(profile)
    capacity_before = _profile_capacity(profile)
    observations = int(profile.observations or 0) + 1
    alpha = _adaptive_alpha(observations - 1)
    if kind == "short_rate":
        alpha = alpha * 0.5  # 단기(429)는 더 완만하게

    if usage > 0:
        learned = capacity_before + alpha * (float(usage) - capacity_before)
        learned = max(CAPACITY_FLOOR, min(capacity_before, int(round(learned))))
        profile.learned_capacity = learned
        lo = profile.capacity_lo if profile.capacity_lo is not None else learned
        hi = profile.capacity_hi if profile.capacity_hi is not None else capacity_before
        profile.capacity_lo = min(lo, learned)
        profile.capacity_hi = max(hi, capacity_before)
    else:
        lo = profile.capacity_lo if profile.capacity_lo is not None else capacity_before
        hi = profile.capacity_hi if profile.capacity_hi is not None else capacity_before
        profile.capacity_lo = min(lo, capacity_before)
        profile.capacity_hi = max(hi, capacity_before)

    profile.observations = observations
    profile.consecutive_errors = int(profile.consecutive_errors or 0) + 1

    base = int(_clamp(
        int(profile.learned_cooldown_minutes or DEFAULT_LEARNED_COOLDOWN_MINUTES),
        COOLDOWN_BASE_MIN_MINUTES,
        COOLDOWN_BASE_MAX_MINUTES,
    ))
    exponent = min(profile.consecutive_errors - 1, COOLDOWN_BACKOFF_MAX_EXPONENT)
    jitter = 1.0 + random.uniform(-COOLDOWN_JITTER, COOLDOWN_JITTER)
    cooldown_minutes = max(1, int(round(base * (2 ** exponent) * jitter)))

    profile.cooldown_until = now + timedelta(minutes=cooldown_minutes)
    profile.cooldown_probe_at = profile.cooldown_until
    profile.recent_usage_count = 0
    profile.window_started_at = now
    profile.last_error_at = now
    profile.last_error_kind = kind
    profile.success_streak = 0

    if profile.consecutive_errors >= 2 and usage == 0:
        # 쿨다운 직후 즉시 초과 → base 자체가 짧았다고 학습
        profile.learned_cooldown_minutes = int(_clamp(
            round(base * 1.5), base, COOLDOWN_BASE_MAX_MINUTES
        ))

    _record_learning_event(
        session,
        profile,
        source_id=source_id,
        kind=kind,
        usage_delta=usage,
        capacity_before=capacity_before,
        capacity_after=profile.learned_capacity,
        cooldown_minutes=cooldown_minutes,
        rate_per_hour=profile.learned_rate_per_hour or 0.0,
        ended_at=now,
    )
    session.commit()
    session.refresh(profile)
    return profile_to_dict(session, profile)


def profile_add_usage(session, profile_id, amount) -> dict | None:
    profile = session.get(Profile, profile_id)
    if profile is None:
        return None
    now = _now()
    _maybe_reset_window(session, profile, now)
    profile.recent_usage_count = max(0, _profile_usage(profile) + int(amount or 0))
    session.commit()
    session.refresh(profile)
    return profile_to_dict(session, profile)


def profile_record_recovery(
    session,
    profile_id,
    usage_delta=0,
    run_seconds=None,
    observed_wait_minutes=None,
    source_id=None,
    started_at=None,
    ended_at=None,
) -> dict | None:
    """성공 복구 및 한도/쿨다운 상향 학습 (Recovery & Scale-up).

    - 실측 처리량(rate) EMA 학습.
    - 실측 회복 대기시간으로 learned_cooldown_minutes EMA 보정(관측 회복).
    - 실제 헤드룸이 확인된 경우에만 한도 소폭 상향(비대칭).
    - 연속 안정 시 쿨다운 base를 완만히 완화.
    """
    profile = session.get(Profile, profile_id)
    if profile is None:
        return None
    now = _now()
    capacity_before = _profile_capacity(profile)
    observations = int(profile.observations or 0) + 1
    alpha = _adaptive_alpha(observations - 1)

    if run_seconds and run_seconds > 0:
        observed_rate = float(max(0, int(usage_delta or 0))) / (float(run_seconds) / 3600.0)
        current_rate = float(profile.learned_rate_per_hour or 0.0)
        if current_rate > 0:
            profile.learned_rate_per_hour = current_rate + alpha * (observed_rate - current_rate)
        else:
            profile.learned_rate_per_hour = observed_rate

    if observed_wait_minutes is None and int(profile.consecutive_errors or 0) > 0:
        last_error = _naive(profile.last_error_at)
        if last_error is not None:
            observed_wait_minutes = (now - last_error).total_seconds() / 60.0
    if observed_wait_minutes is not None and int(profile.consecutive_errors or 0) > 0:
        base = float(_clamp(
            int(profile.learned_cooldown_minutes or DEFAULT_LEARNED_COOLDOWN_MINUTES),
            COOLDOWN_BASE_MIN_MINUTES,
            COOLDOWN_BASE_MAX_MINUTES,
        ))
        learned = base + alpha * (float(observed_wait_minutes) - base)
        profile.learned_cooldown_minutes = int(_clamp(
            round(learned), COOLDOWN_BASE_MIN_MINUTES, COOLDOWN_BASE_MAX_MINUTES
        ))
        profile.observed_recovery_minutes = int(round(observed_wait_minutes))
        profile.consecutive_errors = 0

    usage = int(usage_delta or 0)
    if usage > 0 and capacity_before > 0 and (usage / float(capacity_before)) >= SCALE_UP_USAGE_RATIO:
        beta = alpha * SCALE_UP_BETA_RATIO
        target = capacity_before * SCALE_UP_FACTOR
        new_capacity = int(round(capacity_before + beta * (target - capacity_before)))
        profile.learned_capacity = max(capacity_before, min(int(target), new_capacity))
        profile.capacity_hi = max(profile.capacity_hi or 0, profile.learned_capacity)

    profile.success_streak = int(profile.success_streak or 0) + 1
    if profile.success_streak >= STABLE_RUNS_TO_SCALE:
        profile.success_streak = 0
        profile.learned_cooldown_minutes = int(_clamp(
            round(float(profile.learned_cooldown_minutes or DEFAULT_LEARNED_COOLDOWN_MINUTES) * 0.95),
            COOLDOWN_BASE_MIN_MINUTES,
            COOLDOWN_BASE_MAX_MINUTES,
        ))

    profile.observations = observations
    profile.last_stable_at = now
    profile.last_error_kind = None
    profile.cooldown_probe_at = None

    _record_learning_event(
        session,
        profile,
        source_id=source_id,
        kind="success",
        usage_delta=usage,
        capacity_before=capacity_before,
        capacity_after=profile.learned_capacity,
        cooldown_minutes=int(profile.learned_cooldown_minutes or 0),
        rate_per_hour=profile.learned_rate_per_hour or 0.0,
        started_at=started_at,
        ended_at=ended_at or now,
    )
    session.commit()
    session.refresh(profile)
    return profile_to_dict(session, profile)


def profile_record_success(session, profile_id) -> dict | None:
    """하위 호환: 성공 처리도 유기적 복구 학습으로 위임."""
    return profile_record_recovery(session, profile_id)


def group_register_rate_limit(session, group_id, profile_id=None) -> dict | None:
    """그룹 내 여러 계정이 동시에 한도 초과하면 사이트 공유 한도로 간주하고
    그룹 쿨다운을 에스컬레이션한다."""
    if group_id is None:
        return None
    group = session.get(ProfileGroup, group_id)
    if group is None or not group.shared_limit:
        return None
    profile_ids = [profile.id for profile in group.profiles]
    if not profile_ids:
        return None
    now = _now()
    cutoff = now - timedelta(minutes=GROUP_PRESSURE_WINDOW_MINUTES)
    rows = session.execute(
        select(ProfileLearningEvent.profile_id)
        .where(ProfileLearningEvent.profile_id.in_(profile_ids))
        .where(ProfileLearningEvent.kind.in_(("rate_limit", "short_rate")))
        .where(ProfileLearningEvent.created_at >= cutoff)
    ).scalars().all()
    distinct = len({row for row in rows if row is not None})
    if distinct < GROUP_PRESSURE_THRESHOLD:
        return None
    exponent = min(distinct - GROUP_PRESSURE_THRESHOLD, 4)
    cooldown_minutes = min(
        GROUP_COOLDOWN_MAX_MINUTES, GROUP_BASE_COOLDOWN_MINUTES * (2 ** exponent)
    )
    group.cooldown_until = now + timedelta(minutes=cooldown_minutes)
    group.pace_seconds = min(GROUP_PACE_MAX_SECONDS, float(group.pace_seconds or 0.0) + 0.5)
    session.commit()
    session.refresh(group)
    return profile_group_to_dict(session, group)


def list_profile_learning_events(session, profile_id, limit=50) -> list[dict]:
    rows = (
        session.execute(
            select(ProfileLearningEvent)
            .where(ProfileLearningEvent.profile_id == profile_id)
            .order_by(ProfileLearningEvent.id.desc())
            .limit(max(1, min(int(limit), 500)))
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": row.id,
            "profile_id": row.profile_id,
            "source_id": row.source_id,
            "kind": row.kind,
            "usage_delta": row.usage_delta,
            "capacity_before": row.capacity_before,
            "capacity_after": row.capacity_after,
            "cooldown_minutes": row.cooldown_minutes,
            "rate_per_hour": round(float(row.rate_per_hour or 0.0), 2),
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


def reset_profile_learning(session, profile_id) -> dict | None:
    profile = session.get(Profile, profile_id)
    if profile is None:
        return None
    profile.learned_capacity = DEFAULT_LEARNED_CAPACITY
    profile.learned_cooldown_minutes = DEFAULT_LEARNED_COOLDOWN_MINUTES
    profile.recent_usage_count = 0
    profile.consecutive_errors = 0
    profile.cooldown_until = None
    profile.last_error_at = None
    profile.observations = 0
    profile.learned_rate_per_hour = 0.0
    profile.window_started_at = _now()
    profile.cooldown_probe_at = None
    profile.capacity_lo = DEFAULT_LEARNED_CAPACITY
    profile.capacity_hi = DEFAULT_LEARNED_CAPACITY
    profile.success_streak = 0
    profile.observed_recovery_minutes = None
    profile.last_error_kind = None
    session.commit()
    session.refresh(profile)
    return profile_to_dict(session, profile)


# ---------------------------------------------------------------------------
# Reactive rules (Playwright 반응형 알림 감시)
# ---------------------------------------------------------------------------


def _parse_keyword_pattern(raw) -> list[str]:
    """keyword_pattern 필드를 문자열 리스트로 정규화."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list):
            items = parsed
        else:
            items = [line for line in text.splitlines()]
    result: list[str] = []
    for item in items:
        token = str(item or "").strip()
        if token and token not in result:
            result.append(token)
    return result


def _dump_keyword_pattern(value) -> str:
    return json.dumps(_parse_keyword_pattern(value), ensure_ascii=False)


def reactive_rule_to_dict(session, rule: ReactiveRule) -> dict:
    folder_name = None
    if rule.target_folder_id is not None:
        folder = session.get(Folder, rule.target_folder_id)
        folder_name = folder.name if folder else None
    return {
        "id": rule.id,
        "name": rule.name,
        "site": rule.site,
        "keyword_pattern": _parse_keyword_pattern(rule.keyword_pattern),
        "target_folder_id": rule.target_folder_id,
        "target_folder_name": folder_name,
        "is_active": bool(rule.is_active) if rule.is_active is not None else True,
        "created_at": _iso(rule.created_at),
    }


def list_reactive_rules(session) -> list[dict]:
    rows = (
        session.execute(select(ReactiveRule).order_by(ReactiveRule.id))
        .scalars()
        .all()
    )
    return [reactive_rule_to_dict(session, row) for row in rows]


def active_reactive_rules(session) -> list[dict]:
    rows = (
        session.execute(
            select(ReactiveRule)
            .where(ReactiveRule.is_active.is_(True))
            .order_by(ReactiveRule.id)
        )
        .scalars()
        .all()
    )
    return [reactive_rule_to_dict(session, row) for row in rows]


def get_reactive_rule(session, rule_id) -> dict | None:
    rule = session.get(ReactiveRule, rule_id)
    if rule is None:
        return None
    return reactive_rule_to_dict(session, rule)


def create_reactive_rule(session, payload: dict) -> dict:
    rule = ReactiveRule(
        name=(payload.get("name") or "New Rule").strip(),
        site=(payload.get("site") or None),
        keyword_pattern=_dump_keyword_pattern(payload.get("keyword_pattern")),
        target_folder_id=payload.get("target_folder_id"),
        is_active=bool(payload.get("is_active", True)),
    )
    if rule.target_folder_id is not None:
        rule.target_folder_id = int(rule.target_folder_id)
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return reactive_rule_to_dict(session, rule)


def update_reactive_rule(session, rule_id, payload: dict) -> dict | None:
    rule = session.get(ReactiveRule, rule_id)
    if rule is None:
        return None
    if payload.get("name") is not None:
        rule.name = str(payload["name"]).strip()
    if "site" in payload:
        rule.site = payload["site"] or None
    if payload.get("keyword_pattern") is not None:
        rule.keyword_pattern = _dump_keyword_pattern(payload["keyword_pattern"])
    if "target_folder_id" in payload:
        target = payload["target_folder_id"]
        rule.target_folder_id = int(target) if target is not None else None
    if payload.get("is_active") is not None:
        rule.is_active = bool(payload["is_active"])
    session.commit()
    session.refresh(rule)
    return reactive_rule_to_dict(session, rule)


def delete_reactive_rule(session, rule_id) -> bool:
    rule = session.get(ReactiveRule, rule_id)
    if rule is None:
        return False
    session.delete(rule)
    session.commit()
    return True


# ---------------------------------------------------------------------------
# 통합 로그 (아이템/시스템) — 조회/해결/중복 관리
# ---------------------------------------------------------------------------

LOG_LEVELS = ("DEBUG", "INFO", "SYSTEM", "GDL", "DB", "SUCCESS", "WARN", "ERROR")
UNRESOLVED_LEVELS = ("ERROR", "WARN")


def source_log_level(session, source_id) -> str:
    source = session.get(Source, source_id)
    if source is None:
        return (item_defaults().get("log_level") or "INFO").upper()
    return (source.log_level or "INFO").upper()


def _log_level_rank(level: str) -> int:
    try:
        return LOG_LEVELS.index((level or "INFO").upper())
    except ValueError:
        return LOG_LEVELS.index("INFO")


def should_record(level: str, item_level: str | None) -> bool:
    """아이템 log_level 기준 저장 여부. ERROR/WARN 은 항상 저장.

    log_level(임계값) 은 해당 레벨 '이상'(더 심각한 것 포함)을 기록한다는 의미이므로,
    로그 레벨 랭크가 임계 랭크보다 낮으면(덜 심각하면) 저장하지 않는다.
    """
    lv = (level or "INFO").upper()
    if lv in UNRESOLVED_LEVELS:
        return True
    if not item_level:
        return True
    return _log_level_rank(lv) >= _log_level_rank(item_level)


def log_to_dict(session, row: GdlErrorLog) -> dict:
    source = session.get(Source, row.source_id) if row.source_id is not None else None
    return {
        "id": row.id,
        "source_id": row.source_id,
        "profile_id": row.profile_id,
        "folder_id": row.folder_id,
        "source_name": source.name if source else None,
        "scope": row.scope or "system",
        "level": (row.level or "ERROR").upper(),
        "category": row.category or "",
        "status": row.status or "open",
        "error_code": row.error_code,
        "message": row.message or "",
        "context": _load_json(row.context) if row.context else {},
        "resolution": row.resolution,
        "repeat_count": int(row.repeat_count or 1),
        "created_at": _iso(row.created_at),
        "last_at": _iso(row.last_at or row.created_at),
        "resolved_at": _iso(row.resolved_at),
    }


def _logs_query(
    level=None, category=None, status=None, scope=None, source_id=None,
    folder_id=None, profile_id=None, query=None,
):
    statement = select(GdlErrorLog)
    if level:
        statement = statement.where(GdlErrorLog.level == str(level).upper())
    if category:
        statement = statement.where(GdlErrorLog.category == category)
    if status:
        statement = statement.where(GdlErrorLog.status == status)
    if scope:
        statement = statement.where(GdlErrorLog.scope == scope)
    if source_id is not None:
        statement = statement.where(GdlErrorLog.source_id == int(source_id))
    if folder_id is not None:
        statement = statement.where(GdlErrorLog.folder_id == int(folder_id))
    if profile_id is not None:
        statement = statement.where(GdlErrorLog.profile_id == int(profile_id))
    if query:
        statement = statement.where(GdlErrorLog.message.like("%" + str(query) + "%"))
    return statement


def list_logs(session, limit=200, offset=0, **filters) -> dict:
    statement = _logs_query(**filters).order_by(
        GdlErrorLog.last_at.desc().nullslast(),
        GdlErrorLog.created_at.desc(),
        GdlErrorLog.id.desc(),
    )
    total = len(session.execute(statement).scalars().all())
    rows = (
        session.execute(statement.offset(max(0, int(offset))).limit(max(1, min(int(limit), 500))))
        .scalars()
        .all()
    )
    return {
        "items": [log_to_dict(session, row) for row in rows],
        "total": total,
        "stats": log_stats(session),
    }


def log_stats(session) -> dict:
    rows = session.execute(select(GdlErrorLog)).scalars().all()
    by_level: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for row in rows:
        level = (row.level or "ERROR").upper()
        by_level[level] = by_level.get(level, 0) + 1
        status = row.status or "open"
        by_status[status] = by_status.get(status, 0) + 1
        category = row.category or "기타"
        by_category[category] = by_category.get(category, 0) + 1
    unresolved = by_status.get("open", 0)
    return {
        "total": len(rows),
        "open": unresolved,
        "resolved": by_status.get("resolved", 0),
        "ignored": by_status.get("ignored", 0),
        "errors": by_level.get("ERROR", 0),
        "warnings": by_level.get("WARN", 0),
        "by_level": by_level,
        "by_category": by_category,
    }


def dashboard_stats(session, days: int = 30) -> dict:
    """홈 대시보드용 집계(수집 추이/활성 소스/미해결 오류/프로파일 상태)."""
    now = _now()
    span = max(1, int(days))
    start_date = (now - timedelta(days=span - 1)).date()
    trend_map: dict[str, int] = {}
    monthly_map: dict[str, int] = {}
    success_rows = (
        session.execute(
            select(GdlErrorLog)
            .where(GdlErrorLog.level == "SUCCESS")
            .where(GdlErrorLog.category == "run")
        )
        .scalars()
        .all()
    )
    for row in success_rows:
        moment = _naive(row.last_at) or _naive(row.created_at)
        if moment is None:
            continue
        if moment.date() >= start_date:
            key = moment.strftime("%Y-%m-%d")
            trend_map[key] = trend_map.get(key, 0) + 1
        mkey = moment.strftime("%Y-%m")
        monthly_map[mkey] = monthly_map.get(mkey, 0) + 1
    daily = []
    for offset in range(span):
        day = start_date + timedelta(days=offset)
        key = day.strftime("%Y-%m-%d")
        daily.append({"date": key, "count": trend_map.get(key, 0)})
    monthly = [
        {"month": key, "count": monthly_map[key]}
        for key in sorted(monthly_map.keys())[-12:]
    ]

    sources = session.execute(select(Source)).scalars().all()
    active = sum(1 for s in sources if (s.cyc_option or 0) in (1, 2))
    manual = sum(1 for s in sources if (s.cyc_option or 0) == 0)

    profiles = []
    for row in session.execute(select(Profile)).scalars().all():
        info = profile_to_dict(session, row)
        cooldown = int(info.get("cooldown_seconds") or 0)
        if cooldown > 0 or info.get("probe_pending"):
            profiles.append({
                "id": info["id"],
                "name": info["name"],
                "site": info.get("site"),
                "status": info.get("status"),
                "cooldown_seconds": cooldown,
                "in_cooldown": bool(info.get("in_cooldown")),
                "probe_pending": bool(info.get("probe_pending")),
                "learned_capacity": info.get("learned_capacity", 0),
                "recent_usage_count": info.get("recent_usage_count", 0),
                "remaining_capacity": info.get("remaining_capacity", 0),
                "usage_ratio": info.get("usage_ratio", 0.0),
                "last_error_kind": info.get("last_error_kind"),
            })

    open_rows = (
        session.execute(
            select(GdlErrorLog)
            .where(GdlErrorLog.status == "open")
            .where(GdlErrorLog.level.in_(("ERROR", "WARN")))
        )
        .scalars()
        .all()
    )
    watcher_rows = (
        session.execute(
            select(GdlErrorLog)
            .where(GdlErrorLog.scope == "watcher")
            .order_by(GdlErrorLog.last_at.desc().nullslast(), GdlErrorLog.id.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )
    recent_alerts = [
        {
            "id": row.id,
            "level": row.level,
            "status": row.status,
            "message": row.message,
            "category": row.category,
            "last_at": _iso(row.last_at),
            "context": _load_json(row.context),
        }
        for row in watcher_rows
    ]

    return {
        "active_sources": active,
        "manual_sources": manual,
        "total_sources": len(sources),
        "unresolved_errors": len(open_rows),
        "trend": {"daily": daily, "monthly": monthly},
        "profiles": profiles,
        "recent_alerts": recent_alerts,
    }


def dashboard_hot_picks(session, limit: int = 12) -> list[dict]:
    """최근 수집 이미지를 소스 최근성 기준으로 모아 핫 갤러리 후보를 만든다.

    파일별 좋아요 지표는 저장되어 있지 않으므로 소스의 최근 파일/업로드
    이력(``recent_file_mtime``/``recent_run_at``)을 반응도 프록시로 사용한다.
    썸네일 URL 은 소스 이미지 API(/api/sources/{id}/thumbnail)를 재사용한다.
    """
    rows = (
        session.execute(
            select(Source).order_by(
                Source.recent_file_mtime.desc().nullslast(),
                Source.recent_run_at.desc().nullslast(),
            )
        )
        .scalars()
        .all()
    )
    picks = []
    for source in rows:
        if len(picks) >= limit:
            break
        info = source_to_dict(session, source)
        directory = info.get("download_directory")
        images = utils.list_image_files(directory, 1) if directory else []
        if not images:
            continue
        moments = kde.parse_history(source.upload_time_history or "[]")
        picks.append({
            "source_id": source.id,
            "name": source.name,
            "site": source.site,
            "key": source.key,
            "thumbnail": "/api/sources/%d/thumbnail" % source.id,
            "image_count": info.get("file_count", 0),
            "recent_file_mtime": _iso(source.recent_file_mtime),
            "recent_run_at": _iso(source.recent_run_at),
            "history_count": len(moments),
            "score": len(moments) * 10 + (info.get("file_count", 0) or 0),
        })
    picks.sort(key=lambda item: item.get("score", 0), reverse=True)
    return picks[:limit]


def get_log(session, log_id) -> dict | None:
    row = session.get(GdlErrorLog, log_id)
    if row is None:
        return None
    return log_to_dict(session, row)


def resolve_log(session, log_id, resolution=None) -> dict | None:
    row = session.get(GdlErrorLog, log_id)
    if row is None:
        return None
    row.status = "resolved"
    row.resolved_at = _now()
    if resolution is not None:
        row.resolution = resolution
    session.commit()
    session.refresh(row)
    return log_to_dict(session, row)


def ignore_log(session, log_id, note=None) -> dict | None:
    row = session.get(GdlErrorLog, log_id)
    if row is None:
        return None
    row.status = "ignored"
    row.resolved_at = _now()
    if note is not None:
        row.resolution = note
    session.commit()
    session.refresh(row)
    return log_to_dict(session, row)


def delete_log(session, log_id) -> bool:
    row = session.get(GdlErrorLog, log_id)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def clear_logs(session, level=None, status=None, source_id=None) -> int:
    statement = _logs_query(level=level, status=status, source_id=source_id)
    rows = session.execute(statement).scalars().all()
    count = 0
    for row in rows:
        session.delete(row)
        count += 1
    session.commit()
    return count


def auto_resolve_source_logs(session, source_id) -> int:
    """소스 수집 성공 시 해당 소스의 미해결 로그를 자동 해결 처리."""
    rows = (
        session.execute(
            select(GdlErrorLog)
            .where(GdlErrorLog.source_id == int(source_id))
            .where(GdlErrorLog.status == "open")
        )
        .scalars()
        .all()
    )
    now = _now()
    for row in rows:
        row.status = "resolved"
        row.resolved_at = now
        if not row.resolution:
            row.resolution = "수집 성공으로 자동 해결"
    session.commit()
    return len(rows)


class PostIndex(Base):
    """추천 페이지용 게시물 인덱스.

    gallery-dl 메타데이터(``.metadata/*.json``)가 있으면 반응 지표까지,
    없으면 파일명(``{date} - {author} - {site} - {post_id}-{n}(...).ext``)에서
    날짜·작성자·게시물ID만 뽑아 적재한다.
    """

    __tablename__ = "post_index"
    __table_args__ = (
        UniqueConstraint("site", "post_id", name="uq_post_index_site_post"),
        Index("ix_post_index_posted_at", "posted_at"),
        Index("ix_post_index_source", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    site: Mapped[str] = mapped_column(String(200), default="", index=True)
    uploader_key: Mapped[str] = mapped_column(String(200), default="", index=True)
    uploader_name: Mapped[str] = mapped_column(String(255), default="")
    post_id: Mapped[str] = mapped_column(String(200), default="")
    post_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_count: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[str] = mapped_column(Text, default="[]")
    likes: Mapped[int] = mapped_column(Integer, default=0)
    retweets: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    quotes: Mapped[int] = mapped_column(Integer, default=0)
    bookmarks: Mapped[int] = mapped_column(Integer, default=0)
    views: Mapped[int] = mapped_column(Integer, default=0)
    has_engagement: Mapped[bool] = mapped_column(Boolean, default=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    media_rel: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(String(20), default="file")
    indexed_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
