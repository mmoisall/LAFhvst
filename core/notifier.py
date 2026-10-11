"""알림 웹훅 모듈.

수집 완료/실패, 감시 이벤트 같은 사건을 사용자가 지정한 웹훅 URL 로 POST 한다.
휴대폰의 ntfy/Discord/Slack/Telegram 등으로 알림을 받는 것을 목표로 한다.

외부 의존성 없이 표준 라이브러리(``urllib.request``/``json``/``threading``/``time``)만 사용한다.
전송 실패가 수집을 막지 않도록 이 모듈의 공개 함수는 예외를 던지지 않는다.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PRESETS: tuple[str, ...] = ("json", "text", "ntfy", "discord", "slack", "telegram")

#: 웹훅 요청 타임아웃(초)
TIMEOUT_SEC = 10

# ---------------------------------------------------------------------------
# 스로틀 상태 (프로세스 메모리)
# ---------------------------------------------------------------------------

_throttle_lock = threading.Lock()
#: (event, source_id) -> 마지막 "성공" 전송 시각(monotonic)
_last_sent: dict[tuple[str, int | None], float] = {}


def _throttled(key: tuple[str, int | None], interval: int) -> bool:
    """`interval` 초 이내에 같은 키로 성공 전송한 적이 있으면 True."""
    now = time.monotonic()
    with _throttle_lock:
        last = _last_sent.get(key)
        return last is not None and (now - last) < interval


def _mark_sent(key: tuple[str, int | None]) -> None:
    with _throttle_lock:
        _last_sent[key] = time.monotonic()


# ---------------------------------------------------------------------------
# 보조 함수
# ---------------------------------------------------------------------------


def _log_warn(message: str) -> None:
    """`error_logger` 로 경고 1줄 기록. 순환 import/실패를 방어한다."""
    try:
        from . import error_logger

        error_logger.log("WARN", "notify", message)
    except Exception:
        pass


def _app_version() -> str:
    """애플리케이션 버전(실패 시 "0")."""
    try:
        from . import version

        return str(version.__version__)
    except Exception:
        return "0"


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_settings(settings: dict | None) -> dict:
    """설정을 확정한다. 주 경로는 호출자가 넘긴 `server.SETTINGS`."""
    if settings is not None:
        try:
            return dict(settings)
        except Exception:
            return {}
    try:
        from . import models

        data = models.get_app_settings()
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def _compose_text(title: str, message: str) -> str:
    if title and message:
        return f"{title}\n{message}"
    return title or message


def _valid_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# ---------------------------------------------------------------------------
# 페이로드
# ---------------------------------------------------------------------------


def payload_for(
    preset: str,
    *,
    title: str,
    message: str,
    event: str,
    fields: dict | None = None,
) -> tuple[bytes, str]:
    """프리셋별 (body_bytes, content_type) 을 만든다. 알 수 없는 프리셋은 json 으로 폴백."""
    if preset not in PRESETS:
        preset = "json"
    title = str(title or "")
    message = str(message or "")
    extra = dict(fields or {})

    if preset in ("text", "ntfy"):
        return message.encode("utf-8"), "text/plain; charset=utf-8"

    if preset == "discord":
        body = {"content": _compose_text(title, message)}
    elif preset == "slack":
        body = {"text": _compose_text(title, message)}
    elif preset == "telegram":
        body = {"text": _compose_text(title, message)}
    else:  # json
        body = {
            "event": event,
            "title": title,
            "message": message,
            **extra,
            "app": "LAFhvst",
        }

    return json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json"


# ---------------------------------------------------------------------------
# HTTP 전송
# ---------------------------------------------------------------------------


def _post(url: str, body: bytes, content_type: str) -> tuple[int | None, str | None]:
    """POST 1회. (status, error) 반환. HTTP 오류는 status 로, 그 외는 error 문자열로.

    재시도하지 않는다: 응답을 받기 전 끊긴 경우에도 수신 측이 이미 처리했을 수 있어
    재전송하면 알림이 중복될 수 있다(외부 웹훅은 멱등 키를 지원하지 않는다).
    예외를 던지지 않는다.
    """
    try:
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", content_type)
        request.add_header("User-Agent", f"LAFhvst/{_app_version()}")
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            return int(getattr(response, "status", 0) or 0), None
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except Exception as exc:  # 연결 실패/URL 오류/타임아웃 등
        return None, f"{type(exc).__name__}: {exc}"


def _dispatch(url: str, preset: str, *, event: str, title: str, message: str,
              fields: dict, key: tuple[str, int | None], interval: int) -> dict:
    """실제 전송 + 결과 dict 생성 (스로틀 갱신 포함)."""
    result: dict = {"ok": False, "skipped": None, "status": None, "error": None}
    body, content_type = payload_for(
        preset, title=title, message=message, event=event, fields=fields
    )
    status, error = _post(url, body, content_type)
    if error is not None:
        result["error"] = error
        _log_warn(f"{event} 웹훅 전송 실패({preset}): {error}")
        return result
    result["status"] = status
    if status is not None and 200 <= status < 300:
        result["ok"] = True
        if interval > 0:
            _mark_sent(key)
        return result
    result["error"] = f"http-{status}"
    _log_warn(f"{event} 웹훅 HTTP {status}({preset})")
    return result


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def notify(
    event: str,
    *,
    title: str,
    message: str,
    source_id: int | None = None,
    profile_id: int | None = None,
    fields: dict | None = None,
    settings: dict | None = None,
) -> dict:
    """웹훅 1건 전송. 동기 함수(호출자가 asyncio.to_thread 로 실행).

    반환: ``{"ok", "skipped", "status", "error"}``. 예외를 던지지 않는다.
    """
    cfg = _resolve_settings(settings)
    url = str(cfg.get("notifyWebhookUrl") or "").strip()
    if not url:
        return {"ok": False, "skipped": "disabled", "status": None, "error": None}

    if event == "success" and not _as_bool(cfg.get("notifyOnSuccess"), False):
        return {"ok": False, "skipped": "event-disabled", "status": None, "error": None}
    if event == "error" and not _as_bool(cfg.get("notifyOnError"), True):
        return {"ok": False, "skipped": "event-disabled", "status": None, "error": None}

    interval = _as_int(cfg.get("notifyMinIntervalSec"), 60)
    key = (str(event), source_id)
    if interval > 0 and _throttled(key, interval):
        return {"ok": False, "skipped": "throttled", "status": None, "error": None}

    preset = str(cfg.get("notifyPreset") or "json")
    payload_fields = dict(fields or {})
    payload_fields.setdefault("source_id", source_id)
    payload_fields.setdefault("profile_id", profile_id)
    return _dispatch(
        url, preset, event=event, title=title, message=message,
        fields=payload_fields, key=key, interval=interval,
    )


def notify_success(
    source_name: str,
    *,
    source_id: int | None,
    message: str = "",
    settings: dict | None = None,
) -> dict:
    """수집 완료 알림."""
    return notify(
        "success",
        title=f"수집 완료: {source_name}",
        message=message,
        source_id=source_id,
        settings=settings,
    )


def notify_error(
    source_name: str,
    *,
    source_id: int | None,
    message: str = "",
    settings: dict | None = None,
) -> dict:
    """수집 실패 알림."""
    return notify(
        "error",
        title=f"수집 실패: {source_name}",
        message=message,
        source_id=source_id,
        settings=settings,
    )


def send_test(*, settings: dict | None = None) -> dict:
    """URL/프리셋을 검증하고 실제 1회 전송한다(스로틀 무시)."""
    cfg = _resolve_settings(settings)
    url = str(cfg.get("notifyWebhookUrl") or "").strip()
    if not url:
        return {"ok": False, "skipped": None, "status": None, "error": "no-url"}
    if not _valid_url(url):
        return {"ok": False, "skipped": None, "status": None, "error": "invalid-url"}

    preset = str(cfg.get("notifyPreset") or "json")
    result = _dispatch(
        url, preset,
        event="test",
        title="LAFhvst 테스트 알림",
        message="웹훅 설정이 정상입니다.",
        fields={"source_id": None, "profile_id": None},
        key=("test", None),
        interval=0,  # 스로틀 무시
    )
    return result


def status() -> dict:
    """현재 알림 설정 요약."""
    cfg = _resolve_settings(None)
    url = str(cfg.get("notifyWebhookUrl") or "").strip()
    host = ""
    if url:
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
        except Exception:
            host = ""
    preset = str(cfg.get("notifyPreset") or "json")
    with _throttle_lock:
        keys = len(_last_sent)
    return {
        "enabled": bool(url),
        "preset": preset,
        "url_host": host,
        "throttled_keys": keys,
    }
