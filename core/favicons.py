"""사이트 파비콘(브라우저 탭 아이콘) 수집/캐시.

소스 URL 의 호스트에서 파비콘을 받아 ``data/favicons/<host>.<ext>`` 로 저장하고,
이후에는 캐시만 사용한다. 실패하면 None 을 돌려주고 프론트는 글자 배지로 폴백한다.

수집 순서:
    1. ``https://<host>/favicon.ico`` (가장 흔함, 저렴)
    2. 홈페이지 HTML 의 ``<link rel="...icon" href="...">``
    3. ``http://`` 로 1·2 재시도
"""

import os
import re
import time
import urllib.parse
import urllib.request

from . import utils

FAVICON_DIRNAME = "favicons"
MISS_TTL_SECONDS = 3600
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 LAFhvst"
)
TIMEOUT = 6.0
MAX_BYTES = 512 * 1024
_HTML_BYTES = 256 * 1024

_EXT_BY_TYPE = {
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/ico": ".ico",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/avif": ".avif",
}
_EXT_BY_SUFFIX = {
    ".ico": ".ico",
    ".png": ".png",
    ".jpg": ".jpg",
    ".jpeg": ".jpg",
    ".gif": ".gif",
    ".svg": ".svg",
    ".webp": ".webp",
    ".avif": ".avif",
}
_LINK_RE = re.compile(rb"<link\b[^>]*>", re.I)
_REL_RE = re.compile(rb"rel\s*=\s*[\"']?([^\"'>]+)", re.I)
_HREF_RE = re.compile(rb"href\s*=\s*['\"]([^'\"]+)['\"]", re.I)


def cache_dir() -> str:
    return os.path.join(utils.project_root(), "data", FAVICON_DIRNAME)


def host_of(url: str) -> str | None:
    """URL 에서 호스트(포트 제외)를 뽑는다. http(s) 가 아니면 None."""
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").strip().lower()
    if not host or "." not in host:
        return None
    return host


def safe_name(host: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "_", str(host or "").lower())[:120] or "unknown"


def find_cached(host: str) -> str | None:
    """캐시된 파비콘 경로(확장자 무관)."""
    base = os.path.join(cache_dir(), safe_name(host))
    for ext in sorted(set(_EXT_BY_SUFFIX.values())):
        path = base + ext
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def _pick_ext(content_type: str, url: str) -> str:
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[kind]
    suffix = os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower()
    if suffix in _EXT_BY_SUFFIX:
        return _EXT_BY_SUFFIX[suffix]
    return ".ico"


def _get(url: str, limit: int = MAX_BYTES) -> tuple[bytes, str, str] | None:
    """(data, content_type, final_url) 또는 None."""
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "image/*,*/*;q=0.8",
                "Referer": url,
            },
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if getattr(response, "status", 200) >= 400:
                return None
            data = response.read(limit + 1)
            if not data or len(data) > limit:
                return None
            return data, response.headers.get("Content-Type", ""), response.geturl()
    except Exception:
        return None


def _looks_like_image(data: bytes, content_type: str, url: str) -> bool:
    if (content_type or "").lower().startswith("image/"):
        return True
    if data[:4] in (b"\x00\x00\x01\x00", b"\x89PNG", b"GIF8"):  # .ico / .png / .gif
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data.lstrip()[:5].lower() in (b"<svg ", b"<?xml"):
        return True
    return os.path.splitext(urllib.parse.urlsplit(url).path)[1].lower() in _EXT_BY_SUFFIX


def _icon_from_html(base_url: str) -> list[str]:
    """홈페이지 HTML 에서 link rel=icon 후보 URL 목록."""
    page = _get(base_url, limit=_HTML_BYTES)
    if not page:
        return []
    html, _content_type, final_url = page
    found = []
    for tag in _LINK_RE.findall(html):
        rel = _REL_RE.search(tag)
        if not rel or b"icon" not in rel.group(1).lower():
            continue
        href = _HREF_RE.search(tag)
        if not href:
            continue
        try:
            text = href.group(1).decode("utf-8", "replace").strip()
        except Exception:
            continue
        if not text or text.startswith("data:"):
            continue
        found.append(urllib.parse.urljoin(final_url, text))
    # apple-touch-icon(고해상도) 을 뒤로, 작은 아이콘을 앞으로
    found.sort(key=lambda item: 0 if item.lower().endswith(".ico") else 1)
    return found[:4]


def fetch(host: str) -> tuple[bytes, str] | None:
    """파비콘 바이트를 받아온다. (data, ext) 또는 None."""
    candidates: list[str] = []
    for scheme in ("https", "http"):
        base = "%s://%s/" % (scheme, host)
        candidates.append(base + "favicon.ico")
        candidates.extend(_icon_from_html(base))
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        result = _get(url)
        if not result:
            continue
        data, content_type, final_url = result
        if not _looks_like_image(data, content_type, final_url):
            continue
        return data, _pick_ext(content_type, final_url)
    return None


def ensure(url: str, refresh: bool = False) -> str | None:
    """URL 의 호스트 파비콘 캐시 경로를 반환(없으면 받아서 저장). 실패 시 None."""
    host = host_of(url)
    if not host:
        return None
    cache_root = cache_dir()
    miss_marker = os.path.join(cache_root, safe_name(host) + ".miss")
    if not refresh:
        cached = find_cached(host)
        if cached:
            return cached
        # 실패한 호스트는 1시간 동안 재시도하지 않는다(렌더마다 재요청 방지)
        try:
            if os.path.isfile(miss_marker) and (time.time() - os.path.getmtime(miss_marker)) < MISS_TTL_SECONDS:
                return None
        except OSError:
            pass
    fetched = fetch(host)
    if not fetched:
        try:
            utils.ensure_dir(cache_root)
            with open(miss_marker, "w", encoding="utf-8") as handle:
                handle.write(str(int(time.time())))
        except OSError:
            pass
        return None
    data, ext = fetched
    target = os.path.join(cache_root, safe_name(host) + ext)
    try:
        utils.ensure_dir(cache_root)
        with open(target, "wb") as handle:
            handle.write(data)
        if os.path.isfile(miss_marker):
            os.remove(miss_marker)
    except OSError:
        return None
    return target
