import json
import os
import re
import socket
import sys
import threading
import time
from datetime import datetime, timezone

IH_SUFFIX = ".ih"
IIH_SUFFIX = ".iih"

DEFAULT_DATE_BASIS_ORDER = ["last_run", "last_file", "oldest_error", "normal"]

DATE_BASIS_ALIASES = {
    "normal": "normal",
    "all": "normal",
    "전체": "normal",
    "last run": "last_run",
    "last_run": "last_run",
    "recent_run_at": "last_run",
    "최근 실행": "last_run",
    "last file": "last_file",
    "last_file": "last_file",
    "recent_file_mtime": "last_file",
    "recent_file_date": "last_file",
    "최근 파일": "last_file",
    "oldest error": "oldest_error",
    "oldest_error": "oldest_error",
    "oldest_err_at": "oldest_error",
    "최초 에러": "oldest_error",
}

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def is_frozen() -> bool:
    """PyInstaller 등으로 패키징된 실행 파일인지 여부."""
    return bool(getattr(sys, "frozen", False))


def project_root() -> str:
    """쓰기 가능한 데이터 루트.

    - 소스 실행: 저장소 루트
    - 패키징 실행: 실행 파일이 있는 폴더(포터블)
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_root() -> str:
    """읽기 전용 동봉 리소스(frontend/assets 등) 루트."""
    if is_frozen():
        return getattr(sys, "_MEIPASS", project_root())
    return project_root()


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def profiles_data_dir() -> str:
    return os.path.join(project_root(), "data", "profiles")


def default_profile_dir(profile_id) -> str:
    return os.path.join(profiles_data_dir(), "prof_%s" % profile_id)


def effective_settings(chain_values, own_value):
    """부모(폴더 체인) → 자식 순으로 상속 해석.

    자식에 값이 설정되어 있으면 그대로 사용하고, 아니면 체인에서
    가장 가까운(먼저 등장하는) 비어있지 않은 값을 상속한다. (예: profile_group_id)
    """
    if own_value is not None:
        return own_value
    for value in chain_values or []:
        if value is not None:
            return value
    return None


IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jfif",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".avif",
    ".tif",
    ".tiff",
}

_IMAGE_CACHE: dict[str, tuple[float, list[dict]]] = {}
_IMAGE_CACHE_LOCK = threading.Lock()
_IMAGE_CACHE_TTL = 10.0


def is_image_file(name: str) -> bool:
    return os.path.splitext(str(name or ""))[1].lower() in IMAGE_EXTENSIONS


def is_loopback(host) -> bool:
    text = str(host or "").strip().lower()
    if not text:
        return False
    if text in ("localhost", "::1", "::ffff:127.0.0.1"):
        return True
    return text.startswith("127.")


METADATA_DIRNAME = ".metadata"


def list_image_files(directory: str, limit: int | None = None) -> list[dict]:
    """디렉터리(재귀)의 이미지 파일을 mtime 내림차순으로 반환.

    인덱스 기반 서빙이 인덱스마다 재스캔하지 않도록 짧은 TTL 캐시를 둔다.
    메타데이터 격리 폴더(``.metadata``)는 제외한다.
    """
    if not directory or not os.path.isdir(directory):
        return []
    now = time.time()
    with _IMAGE_CACHE_LOCK:
        cached = _IMAGE_CACHE.get(directory)
    if cached and (now - cached[0]) < _IMAGE_CACHE_TTL:
        items = cached[1]
        return items[:limit] if limit else list(items)
    entries: list[dict] = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d != METADATA_DIRNAME]
        for name in files:
            lowered = name.lower()
            if lowered.endswith(".part") or lowered.endswith(".json.tmp"):
                continue
            if not is_image_file(name):
                continue
            path = os.path.join(root, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            entries.append({"path": path, "mtime": mtime, "name": name})
    entries.sort(key=lambda item: item["mtime"], reverse=True)
    with _IMAGE_CACHE_LOCK:
        _IMAGE_CACHE[directory] = (now, entries)
    return entries[:limit] if limit else list(entries)


_DIR_COUNT_CACHE: dict[str, tuple[float, int]] = {}
_DIR_COUNT_TTL = 10.0


def count_directory_files(directory: str, use_cache: bool = True) -> int:
    """다운로드 폴더의 미디어 파일 수(메타데이터 격리 폴더·JSON 제외)."""
    if not directory or not os.path.isdir(directory):
        return 0
    now = time.time()
    if use_cache:
        cached = _DIR_COUNT_CACHE.get(directory)
        if cached and (now - cached[0]) < _DIR_COUNT_TTL:
            return cached[1]
    count = 0
    for _root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d != METADATA_DIRNAME]
        for name in files:
            lowered = name.lower()
            if lowered.endswith(".part") or lowered.endswith(".json.tmp"):
                continue
            if lowered.endswith(".json") or lowered.endswith(".yaml") or lowered.endswith(".yml"):
                continue
            count += 1
    _DIR_COUNT_CACHE[directory] = (now, count)
    return count


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def local_ipv4_addresses() -> list[str]:
    addresses: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in addresses and not ip.startswith("127."):
                addresses.append(ip)
    except OSError:
        pass
    return addresses


def is_absolute_path(value: str) -> bool:
    if not value:
        return False
    text = value.strip()
    if _DRIVE_RE.match(text):
        return True
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    return os.path.isabs(text)


def compute_base_path(root_name: str) -> str:
    name = (root_name or "").strip()
    if not name:
        return os.path.normpath(os.getcwd())
    if is_absolute_path(name):
        return os.path.normpath(name)
    return os.path.normpath(os.path.join(os.getcwd(), name))


def join_save_path(root_name: str, descendant_names) -> str:
    base = compute_base_path(root_name)
    for raw in descendant_names or []:
        name = (raw or "").strip()
        if name:
            base = os.path.join(base, name)
    return os.path.normpath(base)


def split_tags(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        tokens = [str(item) for item in raw]
    else:
        tokens = str(raw).split(",")
    return [token.strip() for token in tokens if token.strip()]


def parse_tag(token: str) -> dict:
    text = (token or "").strip()
    scope = "self"
    if text.endswith(IIH_SUFFIX):
        scope = "iih"
        text = text[: -len(IIH_SUFFIX)]
    elif text.endswith(IH_SUFFIX):
        scope = "ih"
        text = text[: -len(IH_SUFFIX)]
    return {"name": text.strip(), "scope": scope}


def parse_tags(raw) -> list[dict]:
    return [parse_tag(token) for token in split_tags(raw)]


def tag_names(raw) -> list[str]:
    return [tag["name"] for tag in parse_tags(raw) if tag["name"]]


def resolve_effective_tags(own_raw, ancestor_raws) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()

    for tag in parse_tags(own_raw):
        if not tag["name"] or tag["name"] in seen:
            continue
        result.append({"name": tag["name"], "inherited": False, "scope": tag["scope"]})
        seen.add(tag["name"])

    ancestors = list(ancestor_raws or [])
    total = len(ancestors)
    for index, raw in enumerate(ancestors):
        distance = total - index
        for tag in parse_tags(raw):
            if not tag["name"] or tag["scope"] == "self":
                continue
            if tag["scope"] == "ih" and distance != 1:
                continue
            if tag["name"] in seen:
                continue
            result.append({"name": tag["name"], "inherited": True, "scope": tag["scope"]})
            seen.add(tag["name"])

    return result


def sanitize_component(value, max_length: int | None = None) -> str:
    text = _INVALID_CHARS.sub("_", str(value if value is not None else "")).strip()
    text = text.strip().rstrip(".")
    if max_length is not None and max_length > 0:
        text = text[:max_length].strip().rstrip(".")
    return text or "_"


def download_directory_name(source: dict) -> str:
    site = sanitize_component(source.get("site") or "unknown", 40)
    key = sanitize_component(source.get("key") or "-", 40)
    name = sanitize_component(source.get("name") or source.get("url") or "item", 50)
    return site + " - " + key + " - " + name


def build_download_directory(base_path: str | None, source: dict) -> str:
    base = base_path or os.getcwd()
    return os.path.normpath(os.path.join(base, download_directory_name(source)))


def newest_file_mtime(directory: str, exclude_suffixes=(".info.json",)) -> datetime | None:
    if not directory or not os.path.isdir(directory):
        return None
    newest = None
    fallback = None
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d != METADATA_DIRNAME]
        for name in files:
            if name.endswith(".part") or name.endswith(".json.tmp"):
                continue
            path = os.path.join(root, name)
            try:
                modified = os.path.getmtime(path)
            except OSError:
                continue
            if fallback is None or modified > fallback:
                fallback = modified
            if any(name.endswith(suffix) for suffix in exclude_suffixes):
                continue
            if newest is None or modified > newest:
                newest = modified
    chosen = newest if newest is not None else fallback
    if chosen is None:
        return None
    return datetime.fromtimestamp(chosen, timezone.utc)


_INFO_DATE_KEYS = (
    "upload_date",
    "date",
    "created_at",
    "published",
    "published_at",
    "taken_at",
    "post_date",
    "timestamp",
)


def _parse_info_datetime(raw):
    """gallery-dl info.json 의 업로드 시각 필드를 datetime 으로 파싱."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value > 1e11:  # milliseconds
            value = value / 1000.0
        try:
            return datetime.fromtimestamp(value, timezone.utc).astimezone()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_info_datetime(int(text))
    normalized = text.replace("Z", "").replace("T", " ").split("+")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    parsed = parse_datetime(text)
    return parsed


def upload_time_from_info(info_path: str):
    """info.json 에서 업로드 시각을 찾는다(우선순위: date 계열 키)."""
    if not info_path or not os.path.isfile(info_path):
        return None
    try:
        with open(info_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        try:
            with open(info_path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    for key in _INFO_DATE_KEYS:
        parsed = _parse_info_datetime(data.get(key))
        if parsed is not None:
            return parsed
    return None


def list_media_events(directory: str, cluster_minutes: float = 5.0) -> list[datetime]:
    """디렉터리(재귀)의 미디어 파일에서 업로드 시각을 수집한다.

    - 각 파일: 대응하는 ``*.info.json`` 의 업로드 시각 우선, 없으면 파일 mtime.
    - 동일/근접(``cluster_minutes``) 시각은 대표 1건으로 클러스터링하여 KDE 버스트 오염을 막는다.
    반환: 오래된 순으로 정렬된 고유 시각 목록.
    """
    if not directory or not os.path.isdir(directory):
        return []
    moments: list[datetime] = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d != METADATA_DIRNAME]
        for name in files:
            lowered = name.lower()
            if lowered.endswith(".part") or lowered.endswith(".json.tmp"):
                continue
            if lowered.endswith(".info.json") or lowered.endswith(".yaml") or lowered.endswith(".yml"):
                continue
            path = os.path.join(root, name)
            info_path = _find_info_path(directory, path, root)
            moment = upload_time_from_info(info_path) if info_path else None
            if moment is None:
                try:
                    moment = datetime.fromtimestamp(os.path.getmtime(path))
                except OSError:
                    continue
            if moment.tzinfo is not None:
                moment = moment.astimezone().replace(tzinfo=None)
            moments.append(moment)
    if not moments:
        return []
    moments.sort()
    cluster_seconds = max(0.0, float(cluster_minutes) * 60.0)
    clustered: list[datetime] = [moments[0]]
    for moment in moments[1:]:
        if (moment - clustered[-1]).total_seconds() >= cluster_seconds:
            clustered.append(moment)
    return clustered


def normalize_date_basis(value) -> str:
    if value is None:
        return "normal"
    return DATE_BASIS_ALIASES.get(str(value).strip().lower(), "normal")


def normalize_date_basis_order(order) -> list[str]:
    if isinstance(order, str):
        order = [order]
    if not isinstance(order, (list, tuple)):
        return list(DEFAULT_DATE_BASIS_ORDER)
    normalized = [normalize_date_basis(item) for item in order]
    if "normal" not in normalized:
        normalized.append("normal")
    return normalized or list(DEFAULT_DATE_BASIS_ORDER)


def date_basis_timestamp(source: dict, basis: str):
    if basis == "last_run":
        for key in ("recent_run_at", "last_run_at"):
            if source.get(key):
                return source[key]
    elif basis == "last_file":
        for key in ("recent_file_mtime", "recent_file_date"):
            if source.get(key):
                return source[key]
    elif basis == "oldest_error":
        for key in ("oldest_err_at", "error_date"):
            if source.get(key):
                return source[key]
    return None


def parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        text = str(value).strip().replace("Z", "").split("+")[0]
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def resolve_date_after(
    source: dict, order, offset_hours: float = 0.0, config: dict | None = None
) -> str | None:
    """수집 시작 시점(YYYY-MM-DD) 계산.

    config.date_mode:
      - "full"    : 범위 없이 전체 (None)
      - "fixed"   : config.date_fixed (초 단위까지) 사용
      - 그 외/없음: 증분 (order 기반, 기존 동작)
    overrides(full/fixed)는 server에서 config에 반영되어 전달된다.
    """
    from datetime import timedelta

    if config:
        mode = str(config.get("date_mode") or "").strip().lower()
        if mode == "full":
            return None
        if mode == "fixed":
            fixed = parse_datetime(config.get("date_fixed"))
            if fixed is not None:
                if offset_hours:
                    fixed = fixed - timedelta(hours=offset_hours)
                return fixed.strftime("%Y-%m-%d")

    for basis in normalize_date_basis_order(order):
        if basis == "normal":
            return None
        raw = date_basis_timestamp(source, basis)
        if not raw:
            continue
        parsed = parse_datetime(raw)
        if parsed is None:
            continue
        if offset_hours:
            parsed = parsed - timedelta(hours=offset_hours)
        return parsed.strftime("%Y-%m-%d")
    return None


def resolve_date_filter(
    source: dict, order, offset_hours: float = 0.0, config: dict | None = None
) -> str | None:
    """수집 시작 시점을 gallery-dl 필터식 인자(초 단위)로 계산.

    반환: ``"YYYY,M,D,H,M,S"`` (gallery-dl ``--filter "datetime(...) <= date"`` 용)
    config.date_mode: full/fixed/incremental (resolve_date_after 와 동일 규칙).
    """
    from datetime import timedelta

    target = None
    if config:
        mode = str(config.get("date_mode") or "").strip().lower()
        if mode == "full":
            return None
        if mode == "fixed":
            target = parse_datetime(config.get("date_fixed"))
    if target is None:
        for basis in normalize_date_basis_order(order):
            if basis == "normal":
                return None
            raw = date_basis_timestamp(source, basis)
            if not raw:
                continue
            target = parse_datetime(raw)
            if target is not None:
                break
    if target is None:
        return None
    if offset_hours:
        target = target - timedelta(hours=offset_hours)
    return "%d,%d,%d,%d,%d,%d" % (
        target.year,
        target.month,
        target.day,
        target.hour,
        target.minute,
        target.second,
    )

