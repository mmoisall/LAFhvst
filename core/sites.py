"""사이트 자동완성용 메타데이터.

gallery-dl이 지원하는 사이트(category) 목록을 지연 로드/캐시한다.
gallery-dl이 없거나 로드 실패하면 빈 목록을 반환한다(앱은 정상 동작).
"""

import threading

try:  # pragma: no cover - 선택적 의존성
    import gallery_dl.extractor as _extractor
except Exception:  # pragma: no cover
    _extractor = None

_lock = threading.Lock()
_cache: list[str] | None = None


def supported_sites() -> list[str]:
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is not None:
            return _cache
        sites: set[str] = set()
        if _extractor is not None:
            try:
                for extractor in _extractor.extractors():
                    category = getattr(extractor, "category", None)
                    if category:
                        sites.add(str(category))
            except Exception:
                sites = set()
        _cache = sorted(sites)
        return _cache
