"""Playwright 기반 실시간 브라우저 알림 감시 엔진.

OS 레벨 Push Notification은 헤드리스에서 가로채기 어려우므로,
Persistent Context에 **DOM MutationObserver** 를 주입(expose_binding)하여
새 알림 노드/토스트가 생성될 때마다 텍스트와 링크를 실시간으로 파싱한다.

- 사이트별 전용 세션 디렉토리(data/watcher/<site>)에 쿠키/로그인을 영구 저장.
- 감지된 알림을 ``ReactiveRule.keyword_pattern`` 과 대조.
- 조건 일치 시 ``gdl_executor.run_gdl()`` 로 단발 다운로드를 트리거.
- 감지/완료/실패 이벤트는 메모리 링버퍼에 적재되어 프론트 폴링으로 소비된다.
"""

import asyncio
import json
import os
import re
import time
from collections import deque
from datetime import datetime

from . import browser_manager, error_logger, gdl_executor, models, site_url, utils
from .database import get_session

try:  # pragma: no cover - optional dependency (미설치여도 앱은 동작)
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None


DEDUPE_TTL_SECONDS = 600
EVENT_BUFFER_SIZE = 300
COOKIE_SNAPSHOT_INTERVAL = 30.0
DEFAULT_SITES = ["twitter", "pixiv"]

SITE_LABELS = {
    "twitter": "트위터",
    "pixiv": "픽시브",
    "tumblr": "텀블러",
    "deviantart": "데비안트아트",
    "bluesky": "블루스카이",
    "instagram": "인스타그램",
    "fanbox": "팬박스",
}

SITE_WATCH_CONFIG = {
    "twitter": {
        "url": "https://x.com/notifications",
        "label": "트위터",
        "selectors": [
            '[data-testid="notification"]',
            '[data-testid="toast"]',
            '[data-testid="cellInnerDiv"]',
        ],
    },
    "pixiv": {
        "url": "https://www.pixiv.net/notifications",
        "label": "픽시브",
        "selectors": [
            'li[class*="notification"]',
            '[class*="notification-item"]',
            '[class*="NotificationItem"]',
        ],
    },
}


def is_available() -> bool:
    return async_playwright is not None


def site_label(site) -> str:
    norm = site_url.normalize_site(site)
    if norm in SITE_LABELS:
        return SITE_LABELS[norm]
    cfg = SITE_WATCH_CONFIG.get(norm)
    if cfg and cfg.get("label"):
        return cfg["label"]
    return (site or "알림") or "알림"


def site_config(site):
    norm = site_url.normalize_site(site)
    cfg = SITE_WATCH_CONFIG.get(norm)
    if cfg is not None:
        return norm, cfg
    return norm, {
        "url": site_url.site_home_url(norm) or "about:blank",
        "label": site_label(norm),
        "selectors": [],
    }


def watcher_dir(site) -> str:
    norm = site_url.normalize_site(site) or "default"
    safe = utils.sanitize_component(norm, 60)
    return os.path.join(utils.project_root(), "data", "watcher", safe)


def cookie_path(site) -> str | None:
    norm = site_url.normalize_site(site)
    if not norm:
        return None
    return os.path.join(watcher_dir(norm), browser_manager.COOKIE_FILENAME)


# ---------------------------------------------------------------------------
# 키워드 패턴 매칭
# ---------------------------------------------------------------------------

_REGEX_PATTERN = re.compile(r"^/(.*)/([a-zA-Z]*)$", re.DOTALL)


def _compile_patterns(patterns) -> list[tuple]:
    compiled: list[tuple] = []
    for raw in patterns or []:
        token = str(raw or "").strip()
        if not token:
            continue
        match = _REGEX_PATTERN.match(token)
        if match:
            body, flag_text = match.group(1), match.group(2)
            flags = 0
            for char in flag_text:
                if char == "i":
                    flags |= re.IGNORECASE
                elif char == "m":
                    flags |= re.MULTILINE
                elif char == "s":
                    flags |= re.DOTALL
            try:
                compiled.append(("regex", re.compile(body, flags), token))
                continue
            except re.error:
                pass
        compiled.append(("plain", token.lower(), token))
    return compiled


def match_patterns(compiled, text) -> str | None:
    if not text:
        return None
    lowered = str(text).lower()
    for kind, matcher, raw in compiled:
        if kind == "regex":
            if matcher.search(str(text)):
                return raw
        elif matcher in lowered:
            return raw
    return None


_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.IGNORECASE)


def extract_url(text: str) -> str:
    if not text:
        return ""
    match = _URL_RE.search(str(text))
    return match.group(0) if match else ""


# ---------------------------------------------------------------------------
# MutationObserver 주입 스크립트
# ---------------------------------------------------------------------------

_OBSERVER_TEMPLATE = r"""
(() => {
  if (window.__lafObserverInstalled) return;
  window.__lafObserverInstalled = true;
  const CONFIG = __LAF_CONFIG__;
  const pending = new Map();
  let timer = null;

  function textOf(node) {
    let text = (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
    if (text.length > 600) text = text.slice(0, 600);
    return text;
  }
  function linksOf(node) {
    const urls = [];
    if (node.closest) {
      const anchor = node.closest("a[href]");
      if (anchor && anchor.href) urls.push(anchor.href);
    }
    if (node.matches && node.matches("a[href]") && node.href) urls.push(node.href);
    if (node.querySelectorAll) {
      node.querySelectorAll("a[href]").forEach((a) => {
        if (a.href) urls.push(a.href);
      });
    }
    return urls;
  }
  function matches(node) {
    if (!node.matches) return false;
    const selectors = CONFIG.selectors || [];
    if (!selectors.length) return true;
    for (const selector of selectors) {
      try { if (node.matches(selector)) return true; } catch (e) {}
    }
    if (node.querySelector) {
      for (const selector of selectors) {
        try { if (node.querySelector(selector)) return true; } catch (e) {}
      }
    }
    return false;
  }
  function isNoise(node) {
    const tag = (node.nodeName || "").toLowerCase();
    return tag === "script" || tag === "style" || tag === "link" ||
      tag === "meta" || tag === "noscript" || tag === "svg";
  }
  function queue(node) {
    const text = textOf(node);
    if (!text || text.length < 2) return;
    const urls = linksOf(node);
    const key = text + "||" + (urls[0] || "");
    if (pending.has(key)) return;
    pending.set(key, { text: text, url: urls[0] || "", ts: Date.now() });
  }
  function flush() {
    timer = null;
    if (!pending.size) return;
    const items = Array.from(pending.values());
    pending.clear();
    for (const item of items) {
      try {
        if (typeof window.__lafWatcher === "function") {
          window.__lafWatcher(JSON.stringify(item));
        }
      } catch (e) {}
    }
  }
  function schedule() {
    if (timer) return;
    timer = setTimeout(flush, 350);
  }
  function start() {
    if (!document.body) { setTimeout(start, 120); return; }
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        if (mutation.type !== "childList") continue;
        mutation.addedNodes.forEach((node) => {
          if (node.nodeType !== 1) return;
          if (isNoise(node)) return;
          if (!matches(node)) return;
          queue(node);
        });
      }
      schedule();
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }
  start();
})();
"""


def _observer_script(site) -> str:
    _, cfg = site_config(site)
    config = {"selectors": cfg.get("selectors") or []}
    return _OBSERVER_TEMPLATE.replace(
        "__LAF_CONFIG__", json.dumps(config, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# DB helpers (스레드에서 호출)
# ---------------------------------------------------------------------------


def _db_active_rules() -> list[dict]:
    session = get_session()
    try:
        return models.active_reactive_rules(session)
    finally:
        session.close()


def _db_rule_sites() -> list[str]:
    session = get_session()
    try:
        sites: list[str] = []
        for rule in models.active_reactive_rules(session):
            value = (rule.get("site") or "").strip()
            if value and value not in sites:
                sites.append(value)
        return sites
    finally:
        session.close()


def _db_folder_save_path(folder_id) -> str | None:
    if folder_id is None:
        return None
    session = get_session()
    try:
        context = models.folder_context(session, int(folder_id))
        return context.get("save_path")
    finally:
        session.close()


class BrowserWatcher:
    """Playwright 감시 브라우저의 수명주기와 이벤트 버퍼를 관리한다."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._contexts: dict[str, object] = {}
        self._snapshot_tasks: dict[str, asyncio.Task] = {}
        self._headless = True
        self._sites: list[str] = []
        self._running = False
        self._events: deque = deque(maxlen=EVENT_BUFFER_SIZE)
        self._seq = 0
        self._dedupe: dict[tuple, float] = {}
        self._downloads: set[asyncio.Task] = set()

    # -- 상태 --------------------------------------------------------------
    def is_running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {
            "available": is_available(),
            "running": self.is_running(),
            "headless": self._headless,
            "sites": list(self._sites),
            "events": self._seq,
            "downloads": len(self._downloads),
            "buffer": len(self._events),
        }

    def events_since(self, since=0) -> list[dict]:
        try:
            last = int(since or 0)
        except (TypeError, ValueError):
            last = 0
        return [event for event in list(self._events) if event["seq"] > last]

    # -- 시작/중지 ----------------------------------------------------------
    async def start(self, headless=True, sites=None) -> dict:
        if async_playwright is None:
            raise RuntimeError(
                "playwright가 설치되어 있지 않습니다. "
                "`pip install playwright` 후 `playwright install chromium`을 실행하세요."
            )
        target_sites = await self._resolve_sites(sites)
        if not target_sites:
            raise ValueError("감시할 사이트가 없습니다. 반응형 규칙을 먼저 등록하세요.")
        requested_headless = bool(headless)
        if self.is_running():
            if requested_headless == self._headless and set(target_sites) == set(self._sites):
                return self.status()
            await self.stop()
        self._headless = requested_headless
        self._sites = target_sites
        self._dedupe.clear()
        self._stop_event = asyncio.Event()
        started = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(started), name="lafhvst-watcher"
        )
        await started.wait()
        return self.status()

    async def stop(self) -> dict:
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=20)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except Exception:
                    pass
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        await self._close_all()
        return self.status()

    async def _resolve_sites(self, sites) -> list[str]:
        raw = [item for item in (sites or []) if str(item or "").strip()]
        if not raw:
            raw = list(self._sites)
        if not raw:
            raw = await asyncio.to_thread(_db_rule_sites)
        if not raw:
            raw = list(DEFAULT_SITES)
        result: list[str] = []
        for item in raw:
            norm = site_url.normalize_site(item)
            if norm and norm not in result:
                result.append(norm)
        return result

    # -- 감시 루프 ----------------------------------------------------------
    async def _run(self, started: asyncio.Event) -> None:
        self._running = True
        started_set = False
        try:
            async with async_playwright() as playwright:
                for site in self._sites:
                    try:
                        await self._launch_site(playwright, site)
                    except Exception as exc:
                        self._add_event(
                            site=site,
                            site_label=site_label(site),
                            status="error",
                            message="브라우저 시작 실패: " + str(exc)[:300],
                        )
                        await asyncio.to_thread(
                            error_logger.log_error,
                            None,
                            "watcher launch failed (%s): %s" % (site, exc),
                            "WATCHER",
                        )
                started.set()
                started_set = True
                if self._stop_event is not None:
                    await self._stop_event.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 환경 의존
            await asyncio.to_thread(
                error_logger.log_error, None, "watcher crashed: " + str(exc), "WATCHER"
            )
        finally:
            self._running = False
            if not started_set:
                started.set()
            await self._close_all()

    async def _launch_site(self, playwright, site) -> None:
        _, cfg = site_config(site)
        context_dir = watcher_dir(site)
        await asyncio.to_thread(utils.ensure_dir, context_dir)
        context = await browser_manager.launch_context(
            playwright, context_dir, headless=self._headless
        )
        self._contexts[site] = context
        context.on("close", self._make_close_handler(site))
        try:
            await context.expose_binding("__lafWatcher", self._make_binding(site))
        except Exception:
            pass
        try:
            await context.add_init_script(_observer_script(site))
        except Exception:
            pass
        snapshot = asyncio.create_task(
            self._snapshot_loop(context, cookie_path(site) or os.path.join(context_dir, "cookies.txt")),
            name="lafhvst-watcher-cookies-%s" % site,
        )
        self._snapshot_tasks[site] = snapshot
        page = context.pages[0] if context.pages else await context.new_page()
        target = cfg.get("url") or "about:blank"
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        self._add_event(
            site=site,
            site_label=site_label(site),
            status="monitoring",
            message="감시 시작 (%s)" % ("헤드리스" if self._headless else "UI 표시 모드"),
            url=target,
        )

    def _make_close_handler(self, site):
        def _handler(*_args):
            self._contexts.pop(site, None)
            if self._running and not self._contexts and self._stop_event is not None:
                self._stop_event.set()
        return _handler

    def _make_binding(self, site):
        async def _binding(source, payload):
            data: dict = {}
            if isinstance(payload, str):
                try:
                    data = json.loads(payload)
                except (TypeError, ValueError):
                    data = {"text": payload}
            elif isinstance(payload, dict):
                data = payload
            if not isinstance(data, dict):
                data = {"text": str(data or "")}
            await self._handle_notification(site, data)
        return _binding

    async def _snapshot_loop(self, context, path: str) -> None:
        while True:
            try:
                await browser_manager.export_context_cookies(context, path)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(COOKIE_SNAPSHOT_INTERVAL)

    async def _close_all(self) -> None:
        for site, snapshot in list(self._snapshot_tasks.items()):
            snapshot.cancel()
        for site, snapshot in list(self._snapshot_tasks.items()):
            try:
                await snapshot
            except (asyncio.CancelledError, Exception):
                pass
        self._snapshot_tasks.clear()
        for site, context in list(self._contexts.items()):
            try:
                await context.close()
            except Exception:
                pass
        self._contexts.clear()

    # -- 알림 처리 ----------------------------------------------------------
    async def _handle_notification(self, site, data: dict) -> None:
        text = str(data.get("text") or "").strip()
        if not text:
            return
        url = str(data.get("url") or "").strip()
        if not url:
            url = extract_url(text)
        norm = site_url.normalize_site(site)
        rules = await asyncio.to_thread(_db_active_rules)
        for rule in rules:
            rule_site = site_url.normalize_site(rule.get("site"))
            if rule_site and rule_site != norm:
                continue
            compiled = _compile_patterns(rule.get("keyword_pattern") or [])
            if not compiled:
                continue
            matched = match_patterns(compiled, text)
            if not matched:
                continue
            dedupe_key = (rule.get("id"), url or text)
            now = time.monotonic()
            last = self._dedupe.get(dedupe_key)
            if last is not None and (now - last) < DEDUPE_TTL_SECONDS:
                continue
            self._dedupe[dedupe_key] = now
            if len(self._dedupe) > 500:
                oldest = sorted(self._dedupe.items(), key=lambda item: item[1])[:100]
                for key, _value in oldest:
                    self._dedupe.pop(key, None)
            self._add_event(
                site=norm,
                site_label=site_label(norm),
                rule_id=rule.get("id"),
                rule_name=rule.get("name"),
                keyword=matched,
                text=text[:500],
                url=url,
                folder_id=rule.get("target_folder_id"),
                status="detected",
                message="알림 감지",
            )
            if not url:
                self._add_event(
                    site=norm,
                    site_label=site_label(norm),
                    rule_id=rule.get("id"),
                    rule_name=rule.get("name"),
                    keyword=matched,
                    status="error",
                    message="알림에서 URL을 찾지 못해 수집을 건너뜁니다.",
                )
                continue
            task = asyncio.create_task(
                self._trigger_download(rule, url, norm, matched, text),
                name="lafhvst-watcher-download",
            )
            self._downloads.add(task)
            task.add_done_callback(self._downloads.discard)

    async def _trigger_download(self, rule, url, site, keyword, text) -> None:
        directory = await asyncio.to_thread(_db_folder_save_path, rule.get("target_folder_id"))
        if not directory:
            directory = utils.compute_base_path("")
        options: list[str] = []
        cookies = cookie_path(rule.get("site") or site)
        if cookies and os.path.isfile(cookies):
            options.extend(["--cookies", cookies])
        self._add_event(
            site=site,
            site_label=site_label(site),
            rule_id=rule.get("id"),
            rule_name=rule.get("name"),
            keyword=keyword,
            url=url,
            folder_id=rule.get("target_folder_id"),
            status="started",
            message="즉시 수집 시작",
            directory=directory,
        )
        try:
            result = await gdl_executor.run_gdl(url, directory, options=options)
        except Exception as exc:  # pragma: no cover - 환경 의존
            result = {"ok": False, "errors": [{"code": None, "message": str(exc)}]}
        if result.get("ok"):
            self._add_event(
                site=site,
                site_label=site_label(site),
                rule_id=rule.get("id"),
                rule_name=rule.get("name"),
                keyword=keyword,
                url=url,
                folder_id=rule.get("target_folder_id"),
                status="done",
                message="수집 완료",
                directory=directory,
            )
            return
        errors = result.get("errors") or []
        message = "; ".join(
            str(entry.get("message") if isinstance(entry, dict) else entry)
            for entry in errors
        ) or "수집 실패"
        self._add_event(
            site=site,
            site_label=site_label(site),
            rule_id=rule.get("id"),
            rule_name=rule.get("name"),
            keyword=keyword,
            url=url,
            folder_id=rule.get("target_folder_id"),
            status="error",
            message=message[:300],
            directory=directory,
        )
        await asyncio.to_thread(
            error_logger.log_error, None, "watcher download failed: " + message, "WATCHER"
        )

    # -- 이벤트 버퍼 --------------------------------------------------------
    def _add_event(self, **fields) -> dict:
        self._seq += 1
        event = {"seq": self._seq, "ts": datetime.now().isoformat()}
        event.update(fields)
        self._events.append(event)
        return event


watcher = BrowserWatcher()


async def start(headless=True, sites=None) -> dict:
    return await watcher.start(headless=headless, sites=sites)


async def stop() -> dict:
    return await watcher.stop()


def status() -> dict:
    return watcher.status()


def events_since(since=0) -> list[dict]:
    return watcher.events_since(since)


def is_running() -> bool:
    return watcher.is_running()
