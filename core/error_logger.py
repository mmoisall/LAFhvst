"""통합 로그 기록기.

기존 'gallery-dl 오류 로그'에서 확장되어 아이템(소스/폴더/프로파일) 런 로그와
시스템(감시/브라우저) 로그를 레벨/카테고리/상태와 함께 저장한다.

- 오류(ERROR/WARN): 무제한 저장 (동일 내용도 별도 행 허용)
- 정상(INFO/DEBUG/SUCCESS 등): fingerprint 동일 시 최초 1건 유지 + 반복 횟수/최근 시각 갱신
- 아이템 log_level 에 따라 저장 여부를 필터한다 (ERROR/WARN 은 항상 저장)
"""

import hashlib
import json
import re

from sqlalchemy import select

from . import models
from .database import get_session
from .models import GdlErrorLog

_WS_RE = re.compile(r"\s+")

# 오류 코드/성격 기반 추천 해결 가이드
RESOLUTION_GUIDES = {
    "rate_limit": "프로파일 한도 초과(Code 88). 해당 프로파일을 쿨다운/전환하거나 사용량을 줄이세요.",
    "short_rate": "요청 과다(429). 잠시 대기 후 재시도하거나 페이싱(--sleep-request)을 늘리세요.",
    "auth": "인증 실패입니다. 프로파일 브라우저로 다시 로그인한 뒤 쿠키를 갱신하세요.",
    "pixiv_token": (
        "pixiv 는 gallery-dl 의 `extractor.pixiv.refresh-token` 으로 로그인합니다(쿠키만으로는 불가). "
        "전역 config(%APPDATA%\\gallery-dl\\config.json)의 값이 오래됐거나 플레이스홀더면 이 오류가 납니다. "
        "`gallery-dl oauth:pixiv` 로 토큰을 재발급하거나, 그 값을 유효한 refresh token 으로 바꾸세요. "
        "(앱은 data/gallery-dl.conf 를 함께 넘기며, 전역 config 가 먼저 로드된 뒤 앱 config 가 덮어씁니다.)"
    ),
    "not_found": "대상이 없습니다(404). 소스 URL/키가 유효한지 확인하세요.",
    "browser": "브라우저 실행 실패입니다. Settings에서 브라우저 채널/스텔스를 점검하고 잠금을 정리하세요.",
    "network": "네트워크/타임아웃 오류입니다. 연결과 프록시 설정을 확인하세요.",
}


def classify_category(errors) -> str:
    """오류 목록에서 대표 카테고리를 추정한다."""
    blob = " ".join(
        str(entry.get("message") if isinstance(entry, dict) else entry)
        for entry in (errors or [])
    ).lower()
    codes = " ".join(
        str(entry.get("code")) for entry in (errors or []) if isinstance(entry, dict)
    )
    if "code 88" in blob or "rate limit" in blob or "88" in codes:
        return "rate_limit"
    if "429" in blob or "too many requests" in blob or "429" in codes:
        return "short_rate"
    if "auth" in blob or "login" in blob or "unauthorized" in blob or "403" in codes or "401" in codes:
        return "auth"
    if "404" in blob or "not found" in blob:
        return "not_found"
    if "timeout" in blob or "connection" in blob or "timed out" in blob:
        return "network"
    return "run"


def summarize_errors(errors, limit=3) -> str:
    parts = []
    for entry in errors or []:
        text = entry.get("message") if isinstance(entry, dict) else entry
        text = _normalize(text)
        if text:
            parts.append(text)
        if len(parts) >= limit:
            break
    return " / ".join(parts) if parts else "원인 미상"


def guide_for(category=None, error_code=None, message=None) -> str | None:
    text = (str(message or "") + " " + str(error_code or "")).lower()
    if "refresh token" in text or "refresh_token" in text or "authenticationerror" in text:
        return RESOLUTION_GUIDES["pixiv_token"]
    if category == "auth" or "auth" in text or "로그인" in text:
        return RESOLUTION_GUIDES["auth"]
    if category == "rate_limit" or "code 88" in text or "rate limit" in text:
        return RESOLUTION_GUIDES["rate_limit"]
    if category == "short_rate" or "429" in text or "too many requests" in text:
        return RESOLUTION_GUIDES["short_rate"]
    if "404" in text or "not found" in text:
        return RESOLUTION_GUIDES["not_found"]
    if category == "browser" or "browser" in text or "playwright" in text:
        return RESOLUTION_GUIDES["browser"]
    if "timeout" in text or "timed out" in text or "connection" in text:
        return RESOLUTION_GUIDES["network"]
    return None


def _normalize(message: str) -> str:
    return _WS_RE.sub(" ", str(message or "").strip())


def fingerprint_of(scope, category, error_code, message) -> str:
    raw = "|".join(
        [str(scope or ""), str(category or ""), str(error_code or ""), _normalize(message)]
    )
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def _is_error_level(level: str) -> bool:
    return (level or "").upper() in models.UNRESOLVED_LEVELS


def _item_level_for(session, source_id) -> str | None:
    if source_id is None:
        return None
    return models.source_log_level(session, source_id)


def log(
    level,
    category,
    message,
    *,
    source_id=None,
    profile_id=None,
    folder_id=None,
    scope="system",
    error_code=None,
    context=None,
    dedupe=True,
    created_at=None,
    apply_filter=True,
) -> int | None:
    """로그 1건 기록 (중복 규칙/레벨 필터 적용)."""
    session = get_session()
    try:
        level = (level or "INFO").upper()
        message = str(message or "")
        if apply_filter and source_id is not None:
            item_level = _item_level_for(session, source_id)
            if not models.should_record(level, item_level):
                return None
        now = created_at or models._now()
        fp = fingerprint_of(scope, category, error_code, message)
        encoded_context = json.dumps(context or {}, ensure_ascii=False)
        if dedupe and not _is_error_level(level):
            existing = (
                session.execute(
                    select(GdlErrorLog)
                    .where(GdlErrorLog.fingerprint == fp)
                    .order_by(GdlErrorLog.id)
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if existing is not None:
                existing.repeat_count = int(existing.repeat_count or 1) + 1
                existing.last_at = now
                if context:
                    existing.context = encoded_context
                session.commit()
                return existing.id
        entry = GdlErrorLog(
            source_id=source_id,
            profile_id=profile_id,
            folder_id=folder_id,
            error_code=error_code,
            level=level,
            category=category or "",
            scope=scope or "system",
            status="open" if _is_error_level(level) else "resolved",
            fingerprint=fp,
            repeat_count=1,
            context=encoded_context,
            message=message,
            last_at=now,
        )
        if created_at is not None:
            entry.created_at = created_at
        if not _is_error_level(level):
            entry.resolved_at = now
            entry.resolution = "정상 로그"
        session.add(entry)
        session.commit()
        session.refresh(entry)
        return entry.id
    finally:
        session.close()


def log_error(source_id, message, error_code=None, created_at=None) -> int | None:
    """하위 호환: 오류 로그 (ERROR)."""
    category = "auth" if (error_code in ("401", "403")) else "error"
    return log(
        "ERROR",
        category,
        message,
        source_id=source_id,
        scope="source" if source_id is not None else "system",
        error_code=error_code,
        created_at=created_at,
    )


def log_errors(source_id, errors, created_at=None) -> int:
    count = 0
    for entry in errors or []:
        if isinstance(entry, dict):
            log_error(source_id, entry.get("message", ""), entry.get("code"), created_at)
        else:
            log_error(source_id, str(entry), None, created_at)
        count += 1
    return count


def auto_resolve_related(source_id) -> int:
    session = get_session()
    try:
        return models.auto_resolve_source_logs(session, source_id)
    finally:
        session.close()


def list_errors(source_id, limit=50) -> list[dict]:
    session = get_session()
    try:
        result = models.list_logs(session, limit=limit, source_id=source_id)
        return result["items"]
    finally:
        session.close()
