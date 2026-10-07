"""Playwright 영구 컨텍스트 기반 프로파일별 브라우저 세션 및 쿠키 관리.

- 사용자는 Headful 브라우저에서 직접 로그인/캡차를 해결한다.
- 로그인 세션은 user_data_dir(=context_dir)에 영구 저장된다.
- Netscape HTTP Cookie File 포맷으로 cookies.txt를 추출하여 gallery-dl에 공급한다.
"""

import asyncio
import os
import subprocess

from . import models, site_url, utils
from .database import get_session

try:  # pragma: no cover - optional dependency (설치되어 있지 않아도 앱은 동작)
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None


COOKIE_FILENAME = "cookies.txt"
_SNAPSHOT_INTERVAL = 3.0

BROWSER_CHANNELS = ("chrome", "msedge", "chromium")
_LOCK_FILES = ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket")

# 자동화 탐지 회피용 최소 스텔스 스크립트 (webdriver/languages/window.chrome/plugins/permissions).
# --disable-blink-features=AutomationControlled 플래그 대신 이 스크립트로 대체한다
# (해당 플래그는 Chrome 이 "지원되지 않는 명령줄 플래그" 경고 배너를 띄운다).
_STEALTH_INIT_SCRIPT = r"""
(() => {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
  try {
    if (!window.chrome) { window.chrome = {}; }
    if (!window.chrome.runtime) { window.chrome.runtime = {}; }
  } catch (e) {}
  try {
    Object.defineProperty(navigator, 'languages', {
      get: () => ['ko-KR', 'ko', 'en-US', 'en']
    });
  } catch (e) {}
  try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
      var mk = function (name, filename, desc) {
        return { name: name, filename: filename, description: desc, length: 1 };
      };
      var plugins = [
        mk('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
        mk('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
        mk('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
      ];
      Object.defineProperty(navigator, 'plugins', { get: () => plugins });
      Object.defineProperty(navigator, 'mimeTypes', {
        get: () => [{ type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' }]
      });
    }
  } catch (e) {}
  try {
    var originalQuery = navigator.permissions && navigator.permissions.query;
    if (originalQuery) {
      navigator.permissions.query = function (params) {
        if (params && params.name === 'notifications') {
          return Promise.resolve({ state: Notification.permission, onchange: null });
        }
        return originalQuery.apply(navigator.permissions, arguments);
      };
    }
  } catch (e) {}
})();
"""


def is_available() -> bool:
    return async_playwright is not None


def _browser_settings() -> dict:
    try:
        data = models.get_app_settings()
    except Exception:
        data = {}
    channel = str(data.get("browserChannel") or "auto").strip().lower()
    if channel != "auto" and channel not in BROWSER_CHANNELS:
        channel = "auto"
    return {
        "channel": channel,
        "stealth": data.get("stealthEnabled", True) is not False,
        "cleanup": data.get("browserCleanup", True) is not False,
    }


def _channel_candidates(channel: str) -> list:
    if channel == "auto":
        return ["chrome", "msedge", None]
    if channel == "chromium":
        return [None]
    return [channel, None]


def _installed_chrome_major() -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"
        )
        value, _ = winreg.QueryValueEx(key, "version")
        return str(value).split(".")[0]
    except Exception:
        return None


def build_launch_options(
    context_dir: str, headless: bool, proxy_url: str | None = None, channel=None
) -> dict:
    options = {
        "user_data_dir": context_dir,
        "headless": bool(headless),
        "ignore_default_args": ["--enable-automation"],
        # Playwright 기본값은 샌드박스 비활성(=--no-sandbox)이라 Chrome 이 경고 배너를 띄운다.
        # 실제 Chrome 과 동일하게 샌드박스를 켜고, 실패하면 launch 시 비활성으로 재시도한다.
        "chromium_sandbox": True,
        "args": [
            # --disable-blink-features=AutomationControlled 는 Chrome 이
            # "지원되지 않는 명령줄 플래그" 배너를 띄우므로 쓰지 않는다(스텔스는 init script 로 처리).
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--start-maximized",
        ],
        "locale": "ko-KR",
        "timezone_id": "Asia/Seoul",
        "viewport": None,
    }
    if headless:
        # 헤드리스에서 UA에 HeadlessChrome이 노출되는 것을 막는다(감시 브라우저용)
        major = _installed_chrome_major() or "154"
        options["user_agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/%s.0.0.0 Safari/537.36" % major
        )
    if channel:
        options["channel"] = channel
    if proxy_url:
        options["proxy"] = {"server": proxy_url}
    return options


def cleanup_stale_lock(context_dir: str) -> list:
    removed = []
    for name in _LOCK_FILES:
        path = os.path.join(context_dir, name)
        try:
            if os.path.islink(path) or os.path.isfile(path):
                os.remove(path)
                removed.append(name)
        except OSError:
            pass
    return removed


def terminate_dir_browsers(context_dir: str) -> int:
    """해당 context_dir를 사용하는 Chrome/Edge 프로세스만 종료한다(전역 프로세스 보호)."""
    if os.name != "nt":
        return 0
    target = os.path.abspath(context_dir).lower()
    if not target or not os.path.isdir(context_dir):
        return 0
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains('%s') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        % target.replace("'", "''")
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=20,
        )
    except Exception:
        return 0
    return 1


def cleanup_previous_browsers() -> int:
    """앱 시작 시 이전에 남은 감시/프로파일 브라우저 프로세스와 잠금을 정리한다."""
    settings = _browser_settings()
    if not settings["cleanup"]:
        return 0
    roots = [utils.profiles_data_dir(), os.path.join(utils.project_root(), "data", "watcher")]
    count = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            terminate_dir_browsers(path)
            if cleanup_stale_lock(path):
                count += 1
    return count


async def _maximize_window(context) -> bool:
    """창을 최대화한다.

    프로파일(Preferences)에 저장된 창 크기 때문에 로그인 창이 이상한 크기로 뜨는 문제를 막기 위해
    실행 직후 CDP 로 명시적으로 최대화한다. 실패해도 조용히 넘어간다.
    """
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        session = await context.new_cdp_session(page)
        try:
            target = await session.send("Browser.getWindowForTarget")
            window_id = (target or {}).get("windowId")
            if window_id is None:
                return False
            await session.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "maximized"}},
            )
            return True
        finally:
            try:
                await session.detach()
            except Exception:
                pass
    except Exception:
        return False


async def _try_launch_candidates(
    playwright, candidates, context_dir, headless, proxy_url, stealth
):
    last_error = None
    for channel in candidates:
        options = build_launch_options(context_dir, headless, proxy_url, channel)
        try:
            context = await playwright.chromium.launch_persistent_context(**options)
        except Exception as exc:  # 샌드박스 실패 → 샌드박스만 끄고 같은 채널 재시도
            last_error = exc
            if options.get("chromium_sandbox"):
                fallback = dict(options)
                fallback["chromium_sandbox"] = False
                try:
                    context = await playwright.chromium.launch_persistent_context(**fallback)
                except Exception as exc2:
                    last_error = exc2
                    continue
            else:
                continue
        if stealth:
            try:
                await context.add_init_script(_STEALTH_INIT_SCRIPT)
            except Exception:
                pass
        if not headless:
            await _maximize_window(context)
        return context, None
    return None, last_error


async def launch_context(
    playwright, context_dir: str, headless: bool = False, proxy_url: str | None = None,
    stealth: bool | None = None,
):
    """스텔스 옵션 + 실제 Chrome 우선으로 Persistent Context를 연다.

    첫 시도가 모두 실패하면 잠금을 정리하고 1회 재시도한다.
    """
    settings = _browser_settings()
    if stealth is None:
        stealth = settings["stealth"]
    candidates = _channel_candidates(settings["channel"])
    context, error = await _try_launch_candidates(
        playwright, candidates, context_dir, headless, proxy_url, stealth
    )
    if context is not None:
        return context
    if settings["cleanup"]:
        terminate_dir_browsers(context_dir)
        await asyncio.to_thread(cleanup_stale_lock, context_dir)
        context, error = await _try_launch_candidates(
            playwright, candidates, context_dir, headless, proxy_url, stealth
        )
        if context is not None:
            return context
    raise error or RuntimeError("failed to launch browser")


def cookie_file_path(context_dir: str | None, profile_id=None) -> str:
    base = context_dir or utils.default_profile_dir(profile_id)
    return os.path.join(base, COOKIE_FILENAME)


# 사이트별 필수 쿠키(없으면 인증 실패). pixiv 는 refresh_token 이 있어야 토큰을 갱신한다.
REQUIRED_COOKIES = {
    "pixiv": ("refresh_token",),
}


def has_cookie(cookie_path: str, name: str) -> bool:
    """Netscape cookies.txt 에 해당 이름의 쿠키가 있는지 확인한다."""
    if not cookie_path or not os.path.isfile(cookie_path):
        return False
    try:
        with open(cookie_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                parts = text.split("\t")
                if len(parts) >= 7 and parts[5].strip() == name:
                    return True
    except OSError:
        return False
    return False


def missing_required_cookies(site, cookie_path) -> list[str]:
    """사이트에 필요한데 쿠키 파일에 없는 쿠키 이름 목록."""
    names = REQUIRED_COOKIES.get(str(site or "").strip().lower())
    if not names:
        return []
    return [name for name in names if not has_cookie(cookie_path, name)]


def to_netscape_cookies(cookies) -> str:
    """Playwright context.cookies() 결과를 Netscape HTTP Cookie File 텍스트로 변환."""
    lines = [
        "# Netscape HTTP Cookie File",
        "# This file was generated by LAFhvst. Do not edit manually.",
        "",
    ]
    for cookie in cookies or []:
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "").strip()
        name = cookie.get("name")
        if not domain or name in (None, ""):
            continue
        path = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        expires = cookie.get("expires")
        try:
            expiry = int(float(expires)) if expires and float(expires) > 0 else 0
        except (TypeError, ValueError):
            expiry = 0
        value = cookie.get("value")
        value = "" if value is None else str(value)
        lines.append(
            "\t".join(
                [domain, include_subdomains, path, secure, str(expiry), str(name), value]
            )
        )
    return "\n".join(lines) + "\n"


def write_cookies_file(cookies, path: str) -> str:
    utils.ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(to_netscape_cookies(cookies))
    return path


async def export_context_cookies(context, path: str) -> str | None:
    try:
        cookies = await context.cookies()
    except Exception:
        return None
    return await asyncio.to_thread(write_cookies_file, cookies, path)


def _db_profile(profile_id) -> dict | None:
    session = get_session()
    try:
        profile = session.get(models.Profile, profile_id)
        if profile is None:
            return None
        return {
            "id": profile.id,
            "name": profile.name,
            "site": profile.site,
            "account_key": profile.account_key,
            "account_url": profile.account_url,
            "context_dir": profile.context_dir or utils.default_profile_dir(profile.id),
            "proxy_url": profile.proxy_url,
        }
    finally:
        session.close()


def _is_http(value) -> bool:
    text = str(value or "").strip()
    return text.startswith("http://") or text.startswith("https://")


def _resolve_target_url(profile: dict, url: str | None) -> str | None:
    """우선순위: 명시 url → 프로파일 account_url → site(URL) → site+key 조합."""
    candidate = (url or "").strip()
    if _is_http(candidate):
        return candidate
    account_url = (profile.get("account_url") or "").strip()
    if _is_http(account_url):
        return account_url
    site = (profile.get("site") or "").strip()
    if _is_http(site):
        return site
    key = (profile.get("account_key") or "").strip()
    if site:
        built = site_url.make_url(site, key)
        if built:
            return built
    return None


async def _snapshot_loop(context, path: str) -> None:
    while True:
        await export_context_cookies(context, path)
        await asyncio.sleep(_SNAPSHOT_INTERVAL)


async def launch_profile_browser(profile_id, url: str | None = None, headless: bool = False) -> dict:
    """Headful 브라우저를 띄워 사용자의 로그인/캡차를 기다린 뒤 세션과 쿠키를 저장한다.

    이 코루틴은 창이 닫힐 때까지 대기하므로 반드시 Task로 분리하여
    FastAPI 이벤트 루프를 블로킹하지 않도록 사용한다.
    """
    if async_playwright is None:
        raise RuntimeError(
            "playwright가 설치되어 있지 않습니다. "
            "`pip install playwright` 후 `playwright install chromium`을 실행하세요."
        )
    profile = await asyncio.to_thread(_db_profile, profile_id)
    if profile is None:
        raise ValueError("profile not found: %s" % profile_id)

    context_dir = profile["context_dir"]
    await asyncio.to_thread(utils.ensure_dir, context_dir)
    cookie_path = cookie_file_path(context_dir, profile["id"])

    async with async_playwright() as playwright:
        context = await launch_context(
            playwright,
            context_dir,
            headless=headless,
            proxy_url=profile.get("proxy_url"),
        )
        closed = asyncio.Event()

        def _on_close(*_args):
            if not closed.is_set():
                closed.set()

        context.on("close", _on_close)
        snapshot = asyncio.create_task(_snapshot_loop(context, cookie_path))
        try:
            target = _resolve_target_url(profile, url)
            if target and context.pages:
                try:
                    await context.pages[0].goto(target)
                except Exception:
                    pass
            await closed.wait()
        finally:
            snapshot.cancel()
            try:
                await snapshot
            except asyncio.CancelledError:
                pass
            await export_context_cookies(context, cookie_path)
            try:
                await context.close()
            except Exception:
                pass

    return {
        "ok": True,
        "profile_id": profile["id"],
        "name": profile["name"],
        "context_dir": context_dir,
        "cookies": cookie_path,
    }
