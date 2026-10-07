"""pixiv refresh token 발급(프로파일 브라우저 사용).

gallery-dl 의 ``oauth:pixiv`` 와 같은 OAuth(PKCE) 흐름을, 이미 로그인된 **프로파일 브라우저**로
자동 수행한다. gallery-dl 은 로그인 페이지의 콜백 URL 에서 ``code`` 를 사람이 복사해 붙여넣어야 하지만,
여기서는 Playwright 로 리다이렉트를 직접 가로채므로 복사가 필요 없다.

발급한 refresh-token 은 앱 config(``data/gallery-dl.conf``)에 저장한다.
gallery-dl 은 전역 config(%APPDATA%\\gallery-dl\\config.json) → 앱 config 순으로 로드하므로
전역 config 의 잘못된 값(플레이스홀더/만료)보다 항상 우선한다.
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

from . import browser_manager, gdl_executor, models, utils
from .database import get_session

CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
APP_UA = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
LOGIN_TIMEOUT = 600.0


def _code_verifier() -> str:
    return secrets.token_urlsafe(48)


def _code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def login_url(verifier: str) -> str:
    params = {
        "client": "pixiv-android",
        "code_challenge_method": "S256",
        "code_challenge": _code_challenge(verifier),
    }
    return LOGIN_URL + "?" + urllib.parse.urlencode(params)


def _extract_code(url: str) -> str | None:
    if "code=" not in (url or "") or "auth/pixiv/callback" not in url:
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    values = query.get("code") or []
    return values[0] if values else None


def exchange_code(code: str, verifier: str) -> dict:
    """authorization code → refresh token 교환."""
    form = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "include_policy": "true",
            "redirect_uri": REDIRECT_URI,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=form,
        headers={
            "User-Agent": APP_UA,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("pixiv 응답을 해석하지 못했습니다.")
    if payload.get("error"):
        raise RuntimeError("pixiv 오류: %s" % payload.get("error"))
    token = payload.get("refresh_token")
    if not token:
        raise RuntimeError("refresh_token 이 응답에 없습니다.")
    return {"refresh_token": token, "user": (payload.get("user") or {}).get("name")}


def save_refresh_token(token: str) -> str:
    """앱 config(data/gallery-dl.conf)의 extractor.pixiv.refresh-token 을 갱신."""
    path = gdl_executor.app_config_path()
    data: dict = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    extractor = data.setdefault("extractor", {})
    if not isinstance(extractor, dict):
        extractor = {}
        data["extractor"] = extractor
    pixiv = extractor.setdefault("pixiv", {})
    if not isinstance(pixiv, dict):
        pixiv = {}
        extractor["pixiv"] = pixiv
    pixiv["refresh-token"] = token
    utils.ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=4)
        handle.write("\n")
    return path


async def _wait_for_code(context, page, timeout: float) -> str | None:
    holder = {"code": None}

    def _capture(request):
        if holder["code"]:
            return
        found = _extract_code(getattr(request, "url", "") or "")
        if found:
            holder["code"] = found

    context.on("request", _capture)
    deadline = time.monotonic() + timeout
    while holder["code"] is None and time.monotonic() < deadline:
        if not holder["code"]:
            found = _extract_code(page.url or "")
            if found:
                holder["code"] = found
        if holder["code"]:
            break
        await asyncio.sleep(0.3)
    return holder["code"]


async def issue_refresh_token(profile_id: int, timeout: float = LOGIN_TIMEOUT) -> dict:
    """프로파일 브라우저로 pixiv refresh token 을 발급해 앱 config 에 저장한다."""
    if not browser_manager.is_available():
        return {
            "ok": False,
            "error": "playwright 가 설치되어 있지 않습니다. `pip install playwright` 후 `playwright install chromium` 을 실행하세요.",
        }
    session = get_session()
    try:
        profile = models.get_profile(session, int(profile_id))
    finally:
        session.close()
    if not profile:
        return {"ok": False, "error": "프로파일을 찾을 수 없습니다."}

    from playwright.async_api import async_playwright

    context_dir = profile.get("context_dir")
    verifier = _code_verifier()
    code = None
    async with async_playwright() as playwright:
        context = await browser_manager.launch_context(
            playwright, context_dir, headless=False, proxy_url=profile.get("proxy_url")
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(login_url(verifier), wait_until="domcontentloaded")
            code = await _wait_for_code(context, page, timeout)
            try:
                await browser_manager.export_context_cookies(
                    context, browser_manager.cookie_file_path(context_dir, profile_id)
                )
            except Exception:
                pass
        finally:
            try:
                await context.close()
            except Exception:
                pass

    if not code:
        return {
            "ok": False,
            "error": "pixiv 로그인 콜백을 받지 못했습니다(%.0f초 대기). 열린 창에서 pixiv 로그인을 끝낸 뒤 다시 시도하세요."
            % timeout,
        }
    try:
        result = await asyncio.to_thread(exchange_code, code, verifier)
    except Exception as exc:
        return {"ok": False, "error": "토큰 교환 실패: " + str(exc)}
    path = save_refresh_token(result["refresh_token"])
    return {
        "ok": True,
        "profile_id": int(profile_id),
        "user": result.get("user"),
        "saved_to": path,
        "token_length": len(result["refresh_token"]),
        "token_state": gdl_executor.pixiv_token_state(),
    }
