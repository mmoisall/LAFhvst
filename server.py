import asyncio
import hashlib
import io
import json
import mimetypes
import os
import platform
import secrets
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

import scheduler
from core import (
    alt_paths,
    browser_manager,
    browser_watcher,
    error_logger,
    favicons,
    gdl_executor,
    pixiv_auth,
    kde,
    metadata,
    models,
    notifier,
    post_index,
    remote_access,
    remote_crypto,
    remote_listener,
    site_url,
    sites,
    updater,
    utils,
)
from core.database import get_session, init_db


def proactor_loop_factory():
    import asyncio as _asyncio

    if hasattr(_asyncio, "ProactorEventLoop"):
        loop = _asyncio.ProactorEventLoop()
    else:
        loop = _asyncio.new_event_loop()

    def _handler(active_loop, context):
        exc = context.get("exception")
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10022:
            return
        active_loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    return loop


FRONTEND_DIR = os.path.join(utils.resource_root(), "frontend")
THUMBNAIL_DIR = os.path.join(utils.project_root(), "data", "thumbnails")
THUMBNAIL_MAX_PX = 400


def _thumbnail_cache_path(abs_path: str, mtime: float) -> str:
    key = ("%s|%.6f" % (abs_path, mtime)).encode("utf-8", "replace")
    return os.path.join(THUMBNAIL_DIR, hashlib.sha1(key).hexdigest() + ".jpg")


def ensure_thumbnail(abs_path: str, mtime: float) -> str | None:
    """원격 접속용 축소본을 디스크 캐시에 생성한다(Pillow 없으면 None)."""
    try:
        from PIL import Image  # 지연 import (선택적 의존성)
    except Exception:
        return None
    target = _thumbnail_cache_path(abs_path, mtime)
    if os.path.isfile(target):
        return target
    try:
        os.makedirs(THUMBNAIL_DIR, exist_ok=True)
        with Image.open(abs_path) as image:
            image = image.convert("RGB")
            image.thumbnail((THUMBNAIL_MAX_PX, THUMBNAIL_MAX_PX))
            image.save(target, "JPEG", quality=80)
        return target
    except Exception:
        return None

DEFAULT_SETTINGS = {
    "autoRefresh": False,
    "maxLogCount": 100,
    "systemLogLevel": "INFO",
    "offset_buffer": 0,
    "schedulerEnabled": True,
    "maxConcurrency": 3,
    "watcherAutoStart": False,
    "watcherHeadless": True,
    "watcherSites": [],
    "dbThumbnails": True,
    "browserChannel": "auto",
    "stealthEnabled": True,
    "browserCleanup": True,
    "closeToTray": True,
    "checkUpdateOnStart": True,
    "autoUpdate": False,
    "itemCycOption": 0,
    "itemCyc": 1,
    "itemLogLevel": "INFO",
    "itemConfig": {},
    "itemRetryDelay": 10,
    "kdeBackfillOnFirstRun": True,
    "kdeClusterMinutes": 5,
    "itemSleepMin": 1.3,
    "itemSleepMax": 8,
    "itemSleepRequestMin": 5,
    "itemSleepRequestMax": 8,
    "itemRetries": 4,
    "itemTimeout": 30,
    "itemLimitRate": "",
    "itemDateBasis": "filter",
    "itemMetadataYaml": False,
    "itemTitleAsName": True,
    "itemNameUpdateOnCollect": True,
    "recommendRangeDays": 30,
    "recommendIncludeSensitive": False,
    "recommendMinEngagement": 0,
    "recommendRareMaxPosts": 3,
    "recommendRareMinGapHours": 168,
    "recommendAutoIndex": True,
    "recommendWeightLike": 1.0,
    "recommendWeightRetweet": 2.0,
    "recommendWeightBookmark": 2.0,
    "recommendWeightReply": 1.0,
    "recommendWeightQuote": 1.0,
    "recommendWeightView": 0.001,
    "remoteEnabled": False,
    "remoteAllowDestructive": False,
    "remoteRelayUrl": "",
    "remoteHostId": "",
    "remoteAgentToken": "",
    "remoteAutoStart": False,
    "remoteE2EPrivateKey": "",
    "remoteE2EPublicKey": "",
    "notifyWebhookUrl": "",
    "notifyPreset": "json",
    "notifyOnSuccess": False,
    "notifyOnError": True,
    "notifyMinIntervalSec": 60,
}
SETTINGS = dict(DEFAULT_SETTINGS)

_running: set[int] = set()
_cancel_tokens: dict[int, "gdl_executor.CancelToken"] = {}
_launching: set[int] = set()
_pixiv_token_tasks: set[int] = set()

# SSE(`/api/events`)
SSE_INTERVAL_SECONDS = 3.0
SSE_HEARTBEAT_SECONDS = 15.0
SSE_MAX_CLIENTS = 4
_sse_clients = 0

# E2E v2(종단간 암호화) — 봉투는 릴레이에게 불투명하다. 계약: relay/protocol.md §9
E2E_MAX_ENVELOPE_BYTES = 8 * 1024 * 1024
E2E_MAX_INNER_BYTES = 6 * 1024 * 1024
_e2e_counters: dict[int, int] = {}
_e2e_counter_lock = threading.Lock()


def _sse_message(event: str, data) -> bytes:
    return (
        "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False, default=str) + "\n\n"
    ).encode("utf-8")


_tasks: set = set()

_collect_sem: asyncio.Semaphore | None = None
_collect_sem_limit: int | None = None


def _collect_semaphore() -> asyncio.Semaphore:
    global _collect_sem, _collect_sem_limit
    limit = max(1, int(SETTINGS.get("maxConcurrency") or 3))
    if _collect_sem is None or _collect_sem_limit != limit:
        _collect_sem = asyncio.Semaphore(limit)
        _collect_sem_limit = limit
    return _collect_sem


class FolderPayload(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    config: dict | None = None
    cyc_option: int | None = None
    cyc: int | None = None
    log_level: str | None = None
    tag_list: list[str] | None = None
    memo: str | None = None
    profile_group_id: int | None = None
    profile_id: int | None = None
    move_files: bool = False


class SourcePayload(BaseModel):
    name: str | None = None
    url: str | None = None
    folder_id: int | None = None
    site: str | None = None
    key: str | None = None
    config: dict | None = None
    cyc_option: int | None = None
    cyc: int | None = None
    log_level: str | None = None
    tag_list: list[str] | None = None
    memo: str | None = None
    status: str | None = None
    recent_file_date: str | None = None
    recent_file_mtime: str | None = None
    recent_run_at: str | None = None
    oldest_err_at: str | None = None
    profile_group_id: int | None = None
    profile_id: int | None = None
    move_files: bool = False


class ProfilePayload(BaseModel):
    name: str | None = None
    site: str | None = None
    context_dir: str | None = None
    proxy_url: str | None = None
    account_key: str | None = None
    account_url: str | None = None
    status: str | None = None
    learned_capacity: int | None = None
    learned_cooldown_minutes: int | None = None
    recent_usage_count: int | None = None
    consecutive_errors: int | None = None
    learned_rate_per_hour: float | None = None
    cooldown_until: str | None = None
    group_ids: list[int] | None = None
    is_site_default: bool | None = None


class ProfileGroupPayload(BaseModel):
    name: str | None = None
    site: str | None = None
    account_key: str | None = None
    account_url: str | None = None
    profile_ids: list[int] | None = None
    concurrent_limit: int | None = None
    pace_seconds: float | None = None
    shared_limit: bool | None = None
    cooldown_until: str | None = None


class ProfileLaunchPayload(BaseModel):
    url: str | None = None


class ProfileDefaultPayload(BaseModel):
    value: bool = True


class BatchItem(BaseModel):
    type: str
    id: int


class BatchMovePayload(BaseModel):
    items: list[BatchItem]
    target_folder_id: int | None = None


class BatchEditPayload(BaseModel):
    items: list[BatchItem]
    fields: dict = {}
    depth: int | None = None


class BatchDeletePayload(BaseModel):
    items: list[BatchItem]


class DownloadRequest(BaseModel):
    source_id: int | None = None
    source_ids: list[int] | None = None
    url: str | None = None
    full: bool = False
    date_after: str | None = None


class ReorderPayload(BaseModel):
    ordered_ids: list[int] = []


class ExplorerReorderPayload(BaseModel):
    type: str
    parent_id: int | None = None
    ordered_ids: list[int] = []


class QuickDownloadPayload(BaseModel):
    url: str
    directory: str | None = None
    folder_id: int | None = None
    cookies: bool = True


class SchedulerTogglePayload(BaseModel):
    enabled: bool | None = None


class ReactiveRulePayload(BaseModel):
    name: str | None = None
    site: str | None = None
    keyword_pattern: list[str] | None = None
    target_folder_id: int | None = None
    is_active: bool | None = None


class WatcherTogglePayload(BaseModel):
    enabled: bool | None = None
    headless: bool | None = None
    sites: list[str] | None = None


def _normalize_item_config(value) -> dict:
    """전역 아이템 기본 config: args(추가 실행 옵션)는 기본값에서 제외."""
    if not isinstance(value, dict):
        value = {}
    result = {k: v for k, v in value.items() if k != "args"}
    result.pop("args", None)
    return result


_update_state = {"last": None, "applying": False, "result": None}


def _restart_args() -> list:
    return list(sys.argv[1:])


def _delayed_shutdown(delay: float = 1.2) -> None:
    """응답 전송 후 앱을 종료(업데이터가 파일 교체 후 재시작)."""
    def run():
        import time

        time.sleep(delay)
        try:
            from core import app_control

            app_control.app_control.quit()
        except Exception:
            pass

    threading.Thread(target=run, name="lafhvst-restart", daemon=True).start()


def _perform_update_apply(info=None) -> dict:
    info = info or _update_state.get("last") or updater.check_for_update()
    _update_state["last"] = info
    if not info.get("available"):
        return {"ok": False, "error": "already-latest"}
    _update_state["applying"] = True
    result = updater.apply_update(info, restart_args=_restart_args())
    _update_state["result"] = result
    if result.get("ok"):
        _delayed_shutdown()
    else:
        _update_state["applying"] = False
    return result


async def _startup_update_check() -> None:
    if not SETTINGS.get("checkUpdateOnStart", True):
        return
    try:
        info = await asyncio.to_thread(updater.check_for_update)
        _update_state["last"] = info
        if info.get("available") and SETTINGS.get("autoUpdate"):
            await asyncio.to_thread(_perform_update_apply, info)
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    persisted = models.get_app_settings()
    for key in DEFAULT_SETTINGS:
        if key in persisted:
            SETTINGS[key] = persisted[key]
    # 전역 config.args 는 항상 비운다(예시값 제거). 아이템 개별 args 는 유지.
    SETTINGS["itemConfig"] = _normalize_item_config(SETTINGS.get("itemConfig"))
    models.update_app_settings({"itemConfig": SETTINGS["itemConfig"]})
    # 재시작으로 중단된 수집 상태 정리(안 하면 그대로 'running' 으로 남아 UI 가 수집 중으로 보인다)
    try:
        models.reset_running_status()
    except Exception:
        pass
    scheduler.start_scheduler(_start_collection, lambda: SETTINGS, _alt_scheduler_tick)
    if browser_manager.is_available():
        try:
            await asyncio.to_thread(browser_manager.cleanup_previous_browsers)
        except Exception:
            pass
    if SETTINGS.get("watcherAutoStart") and browser_watcher.is_available():
        try:
            await browser_watcher.start(
                headless=bool(SETTINGS.get("watcherHeadless", True)),
                sites=list(SETTINGS.get("watcherSites") or []) or None,
            )
        except Exception as exc:  # pragma: no cover - 환경 의존
            error_logger.log("ERROR", "watcher", "watcher start failed: " + str(exc), scope="watcher")
    asyncio.create_task(_startup_update_check())
    if SETTINGS.get("remoteEnabled") and SETTINGS.get("remoteAutoStart"):
        asyncio.create_task(_remote_autostart())
    try:
        yield
    finally:
        try:
            await _remote_stop_transport()
        except Exception:  # 종료 경로는 실패해도 계속 진행한다
            pass
        try:
            await browser_watcher.stop()
        except Exception:
            pass
        await scheduler.stop_scheduler()


app = FastAPI(title="LAFhvst", lifespan=lifespan)


def _session():
    return get_session()


@app.get("/api/settings")
def read_settings() -> dict:
    return dict(SETTINGS)


@app.post("/api/settings")
def write_settings(payload: dict) -> dict:
    SETTINGS.update(payload)
    SETTINGS["itemConfig"] = _normalize_item_config(SETTINGS.get("itemConfig"))
    models.update_app_settings({key: SETTINGS[key] for key in DEFAULT_SETTINGS})
    return dict(SETTINGS)


# --- 원격 접속 (페어링 / 기기 토큰 / 릴레이) -------------------------------
# 설계: docs/remote-access-plan.md §3.1, §4, §5.
# 접근 판정의 유일한 근거는 "요청이 원격 리스너로 들어왔는가" + "기기 토큰이 유효한가" 이다.
# 클라이언트가 보낸 IP/프록시 헤더로 로컬 여부를 판단하지 않는다.

SERVER_PORT = 17363  # main.py 가 실제 포트로 덮어쓴다
_remote_state: dict = {
    "transport": "none",
    "running": False,
    "url": "",
    "error": "",
    "since": None,
}
_remote_listener: "remote_listener.RemoteListener | None" = None

_REMOTE_PUBLIC_PATHS = {
    "/",
    "/index.html",
    "/pair.html",
    "/recommend",
    "/manifest.webmanifest",
    "/sw.js",
    "/favicon.ico",
    "/api/version",
    "/api/pair/claim",
    "/api/e2e",
}
_REMOTE_PUBLIC_PREFIXES = ("/css/", "/js/", "/assets/", "/vendor/", "/icons/")
# 앱 셸(정적 자산) — 캐시 재검증 대상
_SHELL_CACHE_PREFIXES = ("/js/", "/css/", "/vendor/", "/icons/", "/assets/")

_SCOPE_RANK = {"view": 0, "control": 1, "manage": 2}

# 본문 검증(422)보다 권한 검사가 먼저 오도록 미들웨어에서도 파괴적 경로를 막는다.
_REMOTE_MANAGE_EXACT = {
    ("POST", "/api/explorer/batch-move"),
    ("POST", "/api/explorer/batch-delete"),
    ("POST", "/api/logs/clear"),
}
_REMOTE_MANAGE_DELETE_PREFIXES = (
    "/api/logs/",
    "/api/folders/",
    "/api/sources/",
    "/api/profiles/",
    "/api/profile-groups/",
)
_REMOTE_MANAGE_POST_PREFIXES = (
    "/api/profiles/",
    "/api/profile-groups/",
    "/api/sources/",
    "/api/folders/",
)


def _remote_manage_required(method: str, path: str) -> bool:
    if (method, path) in _REMOTE_MANAGE_EXACT:
        return True
    if method == "DELETE" and path.startswith(_REMOTE_MANAGE_DELETE_PREFIXES):
        return True
    if method == "POST" and path.endswith(("/reset", "/alt-prune")):
        return path.startswith(_REMOTE_MANAGE_POST_PREFIXES)
    return False


def _remote_manage_allowed(device: dict) -> bool:
    if _SCOPE_RANK.get(device.get("role") or "view", 0) >= _SCOPE_RANK["manage"]:
        return True
    return bool(SETTINGS.get("remoteAllowDestructive"))


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if header[:7].lower() != "bearer ":
        return None
    token = header[7:].strip()
    return token or None


def _remote_request_is_remote(request: Request) -> bool:
    """원격 리스너 경유이거나 loopback 이 아니면 원격으로 취급한다."""
    state = request.scope.get("state") or {}
    if state.get("remote_listener"):
        return True
    client_host = request.client.host if request.client else None
    return not utils.is_loopback(client_host)


def _remote_path_is_public(path: str) -> bool:
    if path in _REMOTE_PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _REMOTE_PUBLIC_PREFIXES)


@app.middleware("http")
async def static_cache_control(request: Request, call_next):
    """앱 셸(HTML/JS/CSS)은 항상 재검증하게 한다.

    정적 파일에 `Cache-Control` 이 없으면 브라우저가 휴리스틱 캐시로 **예전 JS** 를 재사용해
    "UI 는 새것인데 스크립트는 예전 것" 상태가 된다(설정 카드가 초기화되지 않는 증상).
    로컬 UI 이므로 캐시 이득이 없어 재검증(304)으로 충분하다.
    """
    response = await call_next(request)
    path = request.url.path
    if request.method == "GET" and (
        path.endswith(".html")
        or path in ("/", "/recommend")
        or path.startswith(_SHELL_CACHE_PREFIXES)
    ):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def remote_guard(request: Request, call_next):
    request.state.remote_device = None
    if not _remote_request_is_remote(request) or _remote_path_is_public(request.url.path):
        return await call_next(request)
    if not SETTINGS.get("remoteEnabled"):
        return JSONResponse({"detail": "원격 접속이 꺼져 있습니다."}, status_code=403)
    device = remote_access.verify(_bearer_token(request))
    if device is None:
        return JSONResponse(
            {"detail": "기기 인증이 필요합니다. 페어링 링크로 기기를 등록하세요."},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="lafhvst"'},
        )
    request.state.remote_device = device
    if _remote_manage_required(request.method, request.url.path) and not _remote_manage_allowed(
        device
    ):
        return JSONResponse(
            {"detail": "원격 권한이 부족합니다: manage"}, status_code=403
        )
    client_host = remote_access.valid_ip_literal(
        request.client.host if request.client else None
    )
    try:
        await asyncio.to_thread(
            remote_access.touch,
            device["id"],
            ip=client_host or "",
            user_agent=request.headers.get("user-agent") or "",
        )
    except Exception:  # last_seen 갱신 실패가 요청을 막지 않도록
        pass
    return await call_next(request)


def _require_local_only(request: Request) -> None:
    """데스크톱(loopback)에서만 허용. 원격 기기는 IP와 무관하게 거부한다."""
    if getattr(request.state, "remote_device", None) is not None:
        raise HTTPException(status_code=403, detail="원격 접속에서는 지원하지 않는 기능입니다.")
    _require_local_client(request)


def _require_scope(request: Request, scope: str) -> None:
    """원격 기기의 작업 범위 확인. 로컬(loopback)은 항상 통과."""
    device = getattr(request.state, "remote_device", None)
    if device is None:
        return
    if _SCOPE_RANK.get(device.get("role") or "view", 0) >= _SCOPE_RANK.get(scope, 99):
        return
    if scope == "manage" and _remote_manage_allowed(device):
        return
    raise HTTPException(status_code=403, detail="원격 권한이 부족합니다: " + scope)


def _remote_host_id() -> str:
    host_id = str(SETTINGS.get("remoteHostId") or "").strip()
    if len(host_id) >= 16:
        return host_id
    host_id = secrets.token_urlsafe(16)
    SETTINGS["remoteHostId"] = host_id
    models.update_app_settings({"remoteHostId": host_id})
    return host_id


def _remote_agent_token() -> str:
    token = str(SETTINGS.get("remoteAgentToken") or "").strip()
    if len(token) >= 16:
        return token
    token = secrets.token_urlsafe(32)
    SETTINGS["remoteAgentToken"] = token
    models.update_app_settings({"remoteAgentToken": token})
    return token


def _remote_public_base() -> str:
    """공개 주소. 릴레이 설정이 없으면 같은 네트워크(LAN) 주소로 폴백한다."""
    relay = str(SETTINGS.get("remoteRelayUrl") or "").strip().rstrip("/")
    if relay:
        return relay + "/h/" + _remote_host_id()
    addresses = utils.local_ipv4_addresses()
    host = addresses[0] if addresses else "127.0.0.1"
    return "http://%s:%d" % (host, SERVER_PORT)


def _remote_pair_payload() -> dict | None:
    """QR/링크에 담을 페어링 정보(활성 토큰이 없으면 None)."""
    state = remote_access.pair_state()
    token = state.get("token")
    if not state.get("active") or not token:
        return None
    base = _remote_public_base()
    name = "LAFhvst"
    expires_at = int(state["expires_at"])
    e2e = remote_crypto.AVAILABLE
    app_public = ""
    if e2e:
        try:
            _private, app_public = _e2e_keys()
        except Exception:  # 키 생성 실패 시 평문(v1) 페어링으로 폴백
            e2e = False
            app_public = ""
    suffix = "&v=2&pk=%s" % quote(app_public, safe="") if e2e else ""
    link = "%s/pair.html#t=%s&n=%s&e=%d%s" % (
        base,
        quote(token, safe=""),
        quote(name),
        expires_at,
        suffix,
    )
    app_link = "lafhvst://pair?v=%d&n=%s&u=%s&t=%s&e=%d%s" % (
        2 if e2e else 1,
        quote(name),
        quote(base, safe=""),
        quote(token, safe=""),
        expires_at,
        suffix,
    )
    return {
        "host_id": _remote_host_id(),
        "public_url": base,
        "link": link,
        "app_link": app_link,
        "app_public_key": app_public,
        "e2e": e2e,
        "expires_at": state["expires_at"],
        "expires_in": state["expires_in"],
    }


async def _remote_start_transport() -> dict:
    """릴레이 연결 기동. 릴레이 미설정이면 LAN 전용으로 표시한다."""
    from core import remote_tunnel

    relay = str(SETTINGS.get("remoteRelayUrl") or "").strip()
    desired = (relay + "/h/" + _remote_host_id()) if relay else _remote_public_base()
    current = str(remote_tunnel.tunnel.state().get("url") or "")
    if remote_tunnel.tunnel.running and current == desired:
        # 이미 같은 목적지로 연결돼 있다(중복 세션은 서로를 대체해 요청이 끊긴다).
        return _remote_transport_state()
    if remote_tunnel.tunnel.running:
        # 릴레이 주소가 바뀌었거나 LAN 전용으로 전환 → 기존 연결을 정리하고 새로 시작한다.
        await _remote_stop_transport()
    if not remote_access.pair_state().get("active"):
        # 원격 접속이 켜져 있는 동안에는 항상 페어링 링크(QR)가 준비되어 있어야 한다.
        remote_access.issue_pair_token()
    if not relay:
        _remote_state.update(
            {
                "transport": "lan",
                "running": True,
                "url": _remote_public_base(),
                "error": "",
                "since": time.time(),
            }
        )
        return _remote_transport_state()

    port = SERVER_PORT + 1
    global _remote_listener
    if _remote_listener is None:
        _remote_listener = remote_listener.RemoteListener(app)
    started = await asyncio.to_thread(_remote_listener.start, port)
    if not started:
        _remote_state.update(
            {
                "transport": "relay",
                "running": False,
                "url": _remote_public_base(),
                "error": "원격 리스너(127.0.0.1:%d)를 열지 못했습니다." % port,
                "since": time.time(),
            }
        )
        return _remote_transport_state()
    await remote_tunnel.tunnel.start(
        relay_url=relay,
        host_id=_remote_host_id(),
        agent_token=_remote_agent_token(),
        target_port=port,
        label=_remote_label(),
    )
    _remote_state.update(remote_tunnel.tunnel.state())
    _remote_state["listener_port"] = port
    return _remote_transport_state()


async def _remote_stop_transport() -> dict:
    relay = str(SETTINGS.get("remoteRelayUrl") or "").strip()
    if relay:
        from core import remote_tunnel

        await remote_tunnel.tunnel.stop()
    if _remote_listener is not None:
        await asyncio.to_thread(_remote_listener.stop)
    _remote_state.update(
        {
            "transport": "none",
            "running": False,
            "url": "",
            "error": "",
            "since": None,
            "listener_port": 0,
        }
    )
    return _remote_transport_state()


def _remote_transport_state() -> dict:
    """전송 상태. 릴레이 사용 중이면 에이전트의 실시간 상태를 합쳐 반환한다."""
    state = dict(_remote_state)
    if str(SETTINGS.get("remoteRelayUrl") or "").strip():
        from core import remote_tunnel

        live = remote_tunnel.tunnel.state()
        listener_port = state.get("listener_port", 0)
        state.update(live)
        state["listener_port"] = listener_port
    return state


def _remote_label() -> str:
    try:
        return "LAFhvst " + updater.current_version()
    except Exception:
        return "LAFhvst"


async def _remote_autostart() -> None:
    """앱 시작 시 원격 접속이 켜져 있고 자동 시작이면 릴레이 연결을 복구한다."""
    try:
        await _remote_start_transport()
        error_logger.log("INFO", "remote", "원격 접속을 자동으로 시작했습니다.", scope="remote")
    except Exception as exc:  # 시작 실패가 앱을 막지 않도록
        _remote_state.update({"running": False, "error": str(exc)[:200]})
        error_logger.log(
            "ERROR", "remote", "원격 접속 자동 시작 실패 · " + str(exc), scope="remote"
        )


async def _notify(event: str, *, source_name: str, source_id: int | None, message: str) -> None:
    """알림 웹훅 전송. 실패해도 수집 흐름을 막지 않는다."""
    title = ("[LAFhvst] 수집 완료 · " if event == "success" else "[LAFhvst] 수집 실패 · ") + source_name
    try:
        await asyncio.to_thread(
            notifier.notify,
            event,
            title=title,
            message=message,
            source_id=source_id,
            settings=SETTINGS,
        )
    except Exception:  # 알림 실패는 조용히 무시한다
        pass


def _e2e_keys():
    """앱 E2E 키쌍(최초 1회 생성해 설정에 보관). 반환: (개인키, 공개키 b64url)."""
    private_text = str(SETTINGS.get("remoteE2EPrivateKey") or "").strip()
    public_text = str(SETTINGS.get("remoteE2EPublicKey") or "").strip()
    if private_text and public_text:
        try:
            return remote_crypto.load_private_key(private_text), public_text
        except Exception:  # 손상된 키는 재발급한다(기존 기기는 재페어링 필요)
            pass
    private, public_text = remote_crypto.generate_keypair()
    SETTINGS["remoteE2EPrivateKey"] = remote_crypto.private_key_b64(private)
    SETTINGS["remoteE2EPublicKey"] = public_text
    models.update_app_settings(
        {
            "remoteE2EPrivateKey": SETTINGS["remoteE2EPrivateKey"],
            "remoteE2EPublicKey": public_text,
        }
    )
    return private, public_text


def _pair_claim_e2e(payload: "PairClaimPayload", request: Request) -> dict:
    """E2E v2 페어링: 요청/응답 모두 페어링 토큰에서 파생한 키로 암호화한다."""
    if not remote_crypto.AVAILABLE:
        raise HTTPException(status_code=503, detail=remote_crypto.MISSING_REASON)
    phone_public = (payload.pk or "").strip()
    if not phone_public:
        raise HTTPException(status_code=400, detail="폰 공개키가 없습니다.")
    token = remote_access.pair_state().get("token")
    if not token:
        raise HTTPException(status_code=401, detail="활성 페어링 링크가 없습니다. QR을 다시 발급하세요.")
    private, _public = _e2e_keys()
    client_host = request.client.host if request.client else ""
    try:
        key = remote_crypto.pair_key(private, phone_public, token)
        inner = remote_crypto.decode_pair_request(key, payload.model_dump())
    except Exception:
        remote_access.record_failure(client_host)
        raise HTTPException(status_code=401, detail="페어링 봉투를 복호화할 수 없습니다.")
    result = remote_access.claim(
        inner["token"],
        name=inner["name"],
        ip=client_host,
        user_agent=request.headers.get("user-agent") or "",
        public_key=phone_public,
    )
    if result is None:
        raise HTTPException(status_code=401, detail="페어링 토큰이 유효하지 않거나 만료되었습니다.")
    error_logger.log(
        "INFO",
        "remote",
        "원격 기기를 등록했습니다(E2E). · %s (%s)" % (result["device"]["name"], client_host or "-"),
        scope="remote",
    )
    return remote_crypto.encode_pair_response(
        key,
        {
            "device_id": result["device"]["id"],
            "device_token": result["token"],
            "host_id": _remote_host_id(),
            "public_url": _remote_public_base(),
            "name": result["device"]["name"],
        },
    )


@app.post("/api/e2e")
async def api_e2e(request: Request) -> Response:
    """E2E v2 봉투 처리(릴레이는 본문을 해석할 수 없다).

    봉투 안의 요청은 **원격 리스너를 경유**해 실행한다 → 미들웨어·스코프 검증이 그대로 적용된다.
    """
    body = await request.body()
    if not remote_crypto.AVAILABLE:
        raise HTTPException(status_code=503, detail=remote_crypto.MISSING_REASON)
    if not body or body[0] != remote_crypto.ENVELOPE_MAGIC:
        raise HTTPException(status_code=400, detail="지원하지 않는 봉투입니다.")
    if len(body) > E2E_MAX_ENVELOPE_BYTES:
        raise HTTPException(status_code=413, detail="봉투가 너무 큽니다.")
    device_id = int.from_bytes(body[1:5], "big")
    device = remote_access.e2e_device(device_id)
    if device is None or not device.get("public_key"):
        raise HTTPException(status_code=401, detail="등록된 E2E 기기가 아닙니다.")
    private, _public = _e2e_keys()
    try:
        key = remote_crypto.session_key(private, device["public_key"], device_id)
        inner = remote_crypto.decode_request(device_id, key, body)
    except Exception:
        raise HTTPException(status_code=401, detail="봉투를 복호화할 수 없습니다.")
    if inner["path"].startswith("/api/e2e"):
        raise HTTPException(status_code=400, detail="중첩 봉투는 허용하지 않습니다.")
    if not remote_access.accept_counter(device_id, inner["counter"]):
        raise HTTPException(status_code=401, detail="재전송된 요청입니다.")
    status, headers, payload = await _e2e_inner_request(inner)
    counter = _e2e_next_counter(device_id)
    envelope = remote_crypto.encode_response(
        device_id,
        counter,
        key,
        status=status,
        headers=_e2e_response_headers(headers),
        body=payload,
    )
    return Response(
        envelope,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


def _e2e_response_headers(headers: dict) -> dict:
    """폰에 돌려줄 응답 헤더(홉바이홉·길이 관련 제거)."""
    drop = {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-length",
        "date",
        "server",
    }
    return {
        str(key): str(value)
        for key, value in (headers or {}).items()
        if str(key).lower() not in drop
    }


def _e2e_next_counter(device_id: int) -> int:
    """기기별 응답 카운터(폰이 단조 증가를 검증한다)."""
    with _e2e_counter_lock:
        value = _e2e_counters.get(device_id, -1) + 1
        _e2e_counters[device_id] = value
        return value


async def _e2e_inner_request(inner: dict) -> tuple[int, dict, bytes]:
    """봉투 안의 요청을 원격 리스너로 실행한다."""
    if _remote_listener is None or not _remote_listener.running:
        raise HTTPException(status_code=503, detail="원격 리스너가 실행 중이 아닙니다.")
    from core import remote_tunnel

    return await remote_tunnel.local_request(
        SERVER_PORT + 1,
        inner["method"],
        inner["path"],
        inner["query"],
        inner["headers"],
        inner["body"],
        max_bytes=E2E_MAX_INNER_BYTES,
    )


@app.get("/api/notify/status")
def notify_status() -> dict:
    return notifier.status()


@app.post("/api/notify/test")
async def notify_test(request: Request) -> dict:
    """설정된 웹훅으로 테스트 알림을 1회 보낸다(스로틀 무시)."""
    _require_scope(request, "control")
    return await asyncio.to_thread(notifier.send_test, settings=SETTINGS)


class PairClaimPayload(BaseModel):
    t: str = ""
    name: str = ""
    v: int = 1
    pk: str = ""
    enc: str = ""


@app.get("/api/remote/status")
def remote_status(request: Request) -> dict:
    device = getattr(request.state, "remote_device", None)
    if device is None:
        _require_local_client(request)
    state = _remote_transport_state()
    payload = {
        "enabled": bool(SETTINGS.get("remoteEnabled")),
        "transport": state.get("transport") or "none",
        "running": bool(state.get("running")),
        "url": state.get("url") or "",
        "error": state.get("error") or "",
        "relay_url": str(SETTINGS.get("remoteRelayUrl") or ""),
        "host_id": _remote_host_id(),
        "allow_destructive": bool(SETTINGS.get("remoteAllowDestructive")),
        "device": device,
        "devices": [],
        "pair": None,
    }
    if device is None:  # 기기 목록/페어링 정보는 데스크톱에서만 본다
        payload["devices"] = remote_access.list_devices()
        payload["pair"] = _remote_pair_payload()
    else:
        payload["devices"] = [device]  # 원격 기기는 자기 자신만 본다
    return payload


@app.post("/api/remote/enable")
async def remote_enable(request: Request) -> dict:
    _require_local_client(request)
    SETTINGS["remoteEnabled"] = True
    models.update_app_settings({"remoteEnabled": True})
    remote_access.issue_pair_token()
    await _remote_start_transport()
    error_logger.log("INFO", "remote", "원격 접속을 켰습니다.", scope="remote")
    return {
        "ok": True,
        "enabled": True,
        "pair": _remote_pair_payload(),
        "state": _remote_transport_state(),
    }


@app.post("/api/remote/disable")
async def remote_disable(request: Request) -> dict:
    _require_local_client(request)
    SETTINGS["remoteEnabled"] = False
    models.update_app_settings({"remoteEnabled": False})
    remote_access.clear_pair_tokens()
    await _remote_stop_transport()
    error_logger.log("INFO", "remote", "원격 접속을 껐습니다.", scope="remote")
    return {"ok": True, "enabled": False, "state": _remote_transport_state()}


@app.post("/api/remote/rotate")
def remote_rotate(request: Request) -> dict:
    _require_local_client(request)
    if not SETTINGS.get("remoteEnabled"):
        raise HTTPException(status_code=409, detail="원격 접속이 꺼져 있습니다.")
    remote_access.issue_pair_token()
    return {"ok": True, "pair": _remote_pair_payload()}


@app.delete("/api/remote/devices/{device_id}")
def remote_revoke_device(device_id: int, request: Request) -> dict:
    _require_local_client(request)
    revoked = remote_access.revoke(device_id)
    if revoked:
        error_logger.log(
            "INFO", "remote", "원격 기기를 회수했습니다. · id=%d" % device_id, scope="remote"
        )
    return {"ok": revoked}


@app.get("/api/remote/qr.png")
def remote_qr(request: Request) -> Response:
    _require_local_client(request)
    payload = _remote_pair_payload()
    if payload is None:
        raise HTTPException(status_code=404, detail="활성 페어링 링크가 없습니다.")
    try:
        import segno
    except Exception:
        raise HTTPException(status_code=503, detail="segno 패키지가 필요합니다.")
    buffer = io.BytesIO()
    segno.make(payload["link"], error="m").save(buffer, kind="png", scale=6, border=2)
    return Response(
        buffer.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/pair/claim")
def pair_claim(payload: PairClaimPayload, request: Request) -> dict:
    if not SETTINGS.get("remoteEnabled"):
        raise HTTPException(status_code=403, detail="원격 접속이 꺼져 있습니다.")
    if int(payload.v or 1) >= 2 and payload.enc:
        return _pair_claim_e2e(payload, request)
    client_host = request.client.host if request.client else ""
    result = remote_access.claim(
        payload.t,
        name=payload.name,
        ip=client_host,
        user_agent=request.headers.get("user-agent") or "",
    )
    if result is None:
        raise HTTPException(
            status_code=401, detail="페어링 토큰이 유효하지 않거나 만료되었습니다."
        )
    error_logger.log(
        "INFO",
        "remote",
        "원격 기기를 등록했습니다. · %s (%s)" % (result["device"]["name"], client_host or "-"),
        scope="remote",
    )
    return {
        "ok": True,
        "device": result["device"],
        "device_token": result["token"],
        "host_id": _remote_host_id(),
        "public_url": _remote_public_base(),
    }



@app.get("/api/version")
def api_version() -> dict:
    return {
        "version": updater.current_version(),
        "frozen": utils.is_frozen(),
        "exe": updater.current_exe_name(),
    }


@app.post("/api/update/check")
def api_update_check() -> dict:
    info = updater.check_for_update()
    _update_state["last"] = info
    return info


@app.get("/api/update/status")
def api_update_status() -> dict:
    return {
        "last": _update_state.get("last"),
        "applying": _update_state.get("applying"),
        "result": _update_state.get("result"),
        "progress": updater.get_progress(),
        "version": updater.current_version(),
        "supported": updater.is_supported(),
    }


@app.post("/api/update/apply")
def api_update_apply() -> dict:
    info = _update_state.get("last") or updater.check_for_update()
    if not info.get("available"):
        raise HTTPException(status_code=400, detail="이미 최신 버전입니다.")
    if _update_state.get("applying"):
        return {"ok": False, "error": "already-applying"}
    return _perform_update_apply(info)


@app.get("/api/sites")
def list_sites() -> dict:
    session = _session()
    try:
        used = models.distinct_sites(session)
    finally:
        session.close()
    return {"used": used, "supported": sites.supported_sites()}


@app.get("/api/url/resolve")
def resolve_url(url: str = Query(default="")) -> dict:
    return site_url.resolve(url)


@app.get("/api/url/build")
def build_url(site: str = Query(default=""), key: str = Query(default="")) -> dict:
    return site_url.build(site, key)


@app.get("/api/explorer/items")
def explorer_items(folder_id: str | None = Query(default=None), q: str | None = None) -> dict:
    fid = None
    if folder_id not in (None, "", "null", "root"):
        try:
            fid = int(folder_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid folder_id")
    session = _session()
    try:
        folders, sources = models.list_explorer(session, fid)
        return {
            "folder_id": fid,
            "folder": models.get_folder(session, fid) if fid is not None else None,
            "breadcrumb": models.breadcrumb(session, fid),
            "folders": folders,
            "sources": sources,
            "query": q or "",
        }
    finally:
        session.close()


@app.get("/api/explorer/path")
def explorer_path(folder_id: str | None = Query(default=None)) -> list[dict]:
    fid = None
    if folder_id not in (None, "", "null", "root"):
        try:
            fid = int(folder_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid folder_id")
    session = _session()
    try:
        return models.breadcrumb(session, fid)
    finally:
        session.close()


@app.get("/api/explorer/tree")
def explorer_tree() -> dict:
    session = _session()
    try:
        folders, sources = models.list_explorer(session, None)
        return {"type": "root", "name": "root", "children": folders, "sources": sources}
    finally:
        session.close()


@app.get("/api/folders")
def list_all_folders() -> list[dict]:
    session = _session()
    try:
        return models.all_folders_flat(session)
    finally:
        session.close()


@app.get("/api/folders/{folder_id}")
def get_folder(folder_id: int) -> dict:
    session = _session()
    try:
        folder = models.get_folder(session, folder_id)
        if folder is None:
            raise HTTPException(status_code=404, detail="folder not found")
        return folder
    finally:
        session.close()


@app.post("/api/folders", status_code=201)
def create_folder(payload: FolderPayload) -> dict:
    session = _session()
    try:
        return models.create_folder(session, payload.model_dump(exclude_unset=True))
    finally:
        session.close()


@app.put("/api/folders/{folder_id}")
def update_folder(folder_id: int, payload: FolderPayload) -> dict:
    session = _session()
    try:
        try:
            folder = models.update_folder(
                session, folder_id, payload.model_dump(exclude_unset=True)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if folder is None:
            raise HTTPException(status_code=404, detail="folder not found")
        return folder
    finally:
        session.close()


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: int, request: Request) -> dict:
    _require_scope(request, "manage")
    session = _session()
    try:
        return {"deleted": models.delete_folder(session, folder_id)}
    finally:
        session.close()


@app.get("/api/sources")
def list_sources() -> list[dict]:
    session = _session()
    try:
        return models.list_all_sources(session)
    finally:
        session.close()


@app.get("/api/events")
async def api_events(request: Request) -> Response:
    """SSE 스트림: 대시보드 변화와 감시 이벤트를 한 연결로 밀어준다(폴링 대체).

    - 하트비트를 `SSE_HEARTBEAT_SECONDS` 마다 보낸다(릴레이/프록시의 무진행 타임아웃 60초보다 짧게).
    - 동시 연결 수를 `SSE_MAX_CLIENTS` 로 제한한다(로컬 앱 보호).
    """
    _require_scope(request, "view")
    global _sse_clients
    if _sse_clients >= SSE_MAX_CLIENTS:
        raise HTTPException(status_code=503, detail="이벤트 스트림 연결이 너무 많습니다.")

    async def stream():
        global _sse_clients
        _sse_clients += 1
        last_dashboard = ""
        last_seq = 0
        last_sent = time.time()
        try:
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                sent = False
                try:
                    dashboard = await asyncio.to_thread(_build_dashboard)
                except Exception:
                    dashboard = None
                if dashboard is not None:
                    digest = hashlib.sha1(
                        json.dumps(
                            dashboard, sort_keys=True, ensure_ascii=False, default=str
                        ).encode("utf-8")
                    ).hexdigest()
                    if digest != last_dashboard:
                        last_dashboard = digest
                        yield _sse_message("dashboard", dashboard)
                        sent = True
                for event in browser_watcher.events_since(last_seq):
                    last_seq = max(last_seq, int(event.get("seq") or 0))
                    yield _sse_message("watcher", event)
                    sent = True
                if sent:
                    last_sent = time.time()
                elif time.time() - last_sent >= SSE_HEARTBEAT_SECONDS:
                    yield b": ping\n\n"
                    last_sent = time.time()
                await asyncio.sleep(SSE_INTERVAL_SECONDS)
        finally:
            _sse_clients -= 1

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/sources/{source_id}/kde")
def source_kde(source_id: int) -> dict:
    session = _session()
    try:
        source = session.get(models.Source, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        entries = kde.parse_history(source.upload_time_history)
        prediction = scheduler.source_prediction(source, datetime.now())
        return {
            "source_id": source.id,
            "name": source.name,
            "cyc_option": source.cyc_option or 0,
            "learned_upload_cycle": int(source.learned_upload_cycle or 0),
            "history_count": len(entries),
            "days": list(kde.GRID_DAY_NAMES),
            "grid": kde.density_grid(entries),
            "prediction": prediction,
        }
    finally:
        session.close()


@app.get("/api/scheduler/status")
def scheduler_status() -> dict:
    session = _session()
    try:
        rows = (
            session.execute(select(models.Source).where(models.Source.cyc_option.in_((1, 2))))
            .scalars()
            .all()
        )
        now = datetime.now()
        upcoming = []
        for source in rows:
            interval = scheduler.source_interval_minutes(source, now)
            if interval is None:
                continue
            last = (
                models._naive(source.last_attempt_at)
                or models._naive(source.recent_run_at)
                or models._naive(source.created_at)
            )
            elapsed = (now - last).total_seconds() if last else interval * 60.0
            remaining = max(0.0, interval * 60.0 - elapsed)
            prediction = (
                scheduler.source_prediction(source, now)
                if (source.cyc_option or 0) == 2
                else None
            )
            upcoming.append({
                "id": source.id,
                "name": source.name,
                "cyc_option": source.cyc_option or 0,
                "due_in": int(round(remaining)),
                "interval": int(round(interval)),
                "label": prediction["label"] if prediction else None,
                "running": source.id in _running,
            })
        upcoming.sort(key=lambda item: item["due_in"])
        return {
            "enabled": bool(SETTINGS.get("schedulerEnabled", True)),
            "maxConcurrency": int(SETTINGS.get("maxConcurrency") or 3),
            "running": len(_running),
            "running_ids": sorted(_running),
            "upcoming": upcoming[:20],
        }
    finally:
        session.close()


@app.post("/api/scheduler/toggle")
def scheduler_toggle(payload: SchedulerTogglePayload | None = None) -> dict:
    payload = payload or SchedulerTogglePayload()
    current = bool(SETTINGS.get("schedulerEnabled", True))
    enabled = current if payload.enabled is None else bool(payload.enabled)
    SETTINGS["schedulerEnabled"] = enabled
    models.update_app_settings({"schedulerEnabled": enabled})
    error_logger.log(
        "INFO",
        "scheduler",
        "스케줄러 " + ("활성화" if enabled else "비활성화"),
        scope="system",
    )
    return {"ok": True, "enabled": enabled}


@app.get("/api/dashboard/live")
async def dashboard_live() -> dict:
    """홈 대시보드 종합 라이브 데이터(폴링용)."""
    return await asyncio.to_thread(_build_dashboard)


def _build_dashboard() -> dict:
    session = _session()
    try:
        agg = models.dashboard_stats(session, days=30)
        hot = models.dashboard_hot_picks(session, limit=12)
        sources = models.list_all_sources(session)
    finally:
        session.close()

    running_ids = sorted(_running)
    running_set = set(running_ids)
    name_by_id = {row["id"]: row.get("name") for row in sources}

    # 작업 큐: 현재 실행 중(_running)
    queue = [
        {
            "id": sid,
            "name": name_by_id.get(sid) or ("소스 #%d" % sid),
            "state": "running",
            "stopping": bool(
                _cancel_tokens.get(sid) is not None and _cancel_tokens[sid].cancelled
            ),
        }
        for sid in running_ids
    ]

    # 다음 순번: 서버 스케줄러 upcoming 재사용
    sched = scheduler_status()
    upcoming = sched.get("upcoming") or []
    for item in upcoming:
        if item["id"] in running_set:
            item["state"] = "running"
        else:
            item["state"] = "queued"

    return {
        "scheduler": {
            "enabled": bool(sched.get("enabled")),
            "running": sched.get("running", 0),
            "maxConcurrency": sched.get("maxConcurrency", 0),
        },
        "watcher": browser_watcher.status(),
        "queue": queue,
        "next": upcoming[:10],
        "active_sources": agg.get("active_sources", 0),
        "manual_sources": agg.get("manual_sources", 0),
        "total_sources": agg.get("total_sources", 0),
        "running_sources": len(running_ids),
        "unresolved_errors": agg.get("unresolved_errors", 0),
        "trend": agg.get("trend", {"daily": [], "monthly": []}),
        "profiles": agg.get("profiles", []),
        "recent_alerts": agg.get("recent_alerts", []),
        "hot_picks": hot,
        "generated_at": datetime.now().isoformat(),
    }


@app.get("/api/sources/{source_id}")
def get_source(source_id: int) -> dict:
    session = _session()
    try:
        source = models.get_source(session, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        return source
    finally:
        session.close()


@app.post("/api/sources", status_code=201)
def create_source(payload: SourcePayload) -> dict:
    session = _session()
    try:
        return models.create_source(session, payload.model_dump(exclude_unset=True))
    finally:
        session.close()


@app.put("/api/sources/{source_id}")
def update_source(source_id: int, payload: SourcePayload) -> dict:
    session = _session()
    try:
        source = models.update_source(
            session, source_id, payload.model_dump(exclude_unset=True)
        )
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        return source
    finally:
        session.close()


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int, request: Request) -> dict:
    _require_scope(request, "manage")
    session = _session()
    try:
        return {"deleted": models.delete_source(session, source_id)}
    finally:
        session.close()


class AltSyncPayload(BaseModel):
    kind: str = "both"


class MetadataMigratePayload(BaseModel):
    item_type: str | None = None
    item_id: int | None = None
    all: bool = False
    yaml: bool | None = None
    dry_run: bool = False
    include_alt: bool = True


def _alt_base_dir(kind, item):
    """아이템(소스/폴더 dict)의 대체경로 기준(원본) 디렉터리."""
    if item.get("type") == "source":
        return item.get("download_directory") or item.get("save_path")
    return item.get("save_path")


def _alt_status_for(item_type, item_id) -> dict:
    session = _session()
    try:
        item = models.get_source(session, item_id) if item_type == "source" else models.get_folder(session, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="item not found")
        config = item.get("config") or {}
        base = _alt_base_dir(item_type, item)
        return {
            "item_id": item_id,
            "item_type": item_type,
            "base_dir": base,
            "alt_targets": alt_paths.resolve_targets(config, base or "", "alt"),
            "cmb_targets": alt_paths.resolve_targets(config, base or "", "cmb"),
            "mode": config.get("alt_mode", "hardlink"),
            "schedule": config.get("alt_schedule", "manual"),
            "interval_hours": config.get("alt_interval_hours"),
            "last_sync_at": config.get("alt_last_sync_at"),
        }
    finally:
        session.close()


@app.get("/api/sources/{source_id}/alt-status")
def alt_status_source(source_id: int) -> dict:
    return _alt_status_for("source", source_id)


@app.get("/api/folders/{folder_id}/alt-status")
def alt_status_folder(folder_id: int) -> dict:
    return _alt_status_for("folder", folder_id)


def _run_alt_sync(item_type, item_id, kind, force) -> dict:
    session = get_session()
    try:
        item = models.get_source(session, item_id) if item_type == "source" else models.get_folder(session, item_id)
        if item is None:
            return {"ok": False, "error": "item not found"}
        config = item.get("config") or {}
        base = _alt_base_dir(item_type, item)
        kinds = ["alt", "cmb"] if kind == "both" else [kind]
        results = []
        for k in kinds:
            report = alt_paths.sync(base, config, k, force=force)
            results.append(report)
            for err in report.get("errors", [])[:5]:
                error_logger.log(
                    "ERROR", "alt",
                    "대체경로 동기화 오류(%s): %s" % (k, err),
                    source_id=item_id if item_type == "source" else None,
                    scope=item_type,
                )
        # last sync timestamp 갱신
        new_config = dict(config)
        new_config["alt_last_sync_at"] = models._now().isoformat()
        updater = models.update_source if item_type == "source" else models.update_folder
        updater(session, item_id, {"config": new_config})
        return {"ok": True, "item_type": item_type, "reports": results}
    finally:
        session.close()


@app.post("/api/sources/{source_id}/alt-sync")
async def alt_sync_source(source_id: int, payload: AltSyncPayload | None = None) -> dict:
    kind = (payload.kind if payload else "both") or "both"
    result = await asyncio.to_thread(_run_alt_sync, "source", source_id, kind, True)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "failed"))
    return result


@app.post("/api/folders/{folder_id}/alt-sync")
async def alt_sync_folder(folder_id: int, payload: AltSyncPayload | None = None) -> dict:
    kind = (payload.kind if payload else "both") or "both"
    result = await asyncio.to_thread(_run_alt_sync, "folder", folder_id, kind, True)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "failed"))
    return result


@app.post("/api/sources/{source_id}/alt-prune")
async def alt_prune_source(
    source_id: int, request: Request, payload: AltSyncPayload | None = None
) -> dict:
    _require_scope(request, "manage")
    kind = (payload.kind if payload else "both") or "both"
    session = _session()
    try:
        item = models.get_source(session, source_id)
        if item is None:
            raise HTTPException(status_code=404, detail="source not found")
        config = item.get("config") or {}
        base = item.get("download_directory") or item.get("save_path")
    finally:
        session.close()
    kinds = ["alt", "cmb"] if kind == "both" else [kind]
    reports = [
        await asyncio.to_thread(alt_paths.prune, base, config, k) for k in kinds
    ]
    return {"ok": True, "reports": reports}


def _metadata_targets(item_type, item, include_alt=True) -> list[str]:
    """아이템의 메타데이터 정리 대상 디렉터리(원본 base + 옵션 alt/cmb)."""
    config = item.get("config") or {}
    base = _alt_base_dir(item_type, item)
    targets: list[str] = []
    if base:
        targets.append(base)
        if include_alt:
            targets.extend(alt_paths.resolve_targets(config, base, "alt"))
            targets.extend(alt_paths.resolve_targets(config, base, "cmb"))
    seen = set()
    out = []
    for path in targets:
        norm = os.path.normpath(path)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _metadata_status_for(item_type, item_id) -> dict:
    session = _session()
    try:
        item = (
            models.get_source(session, item_id)
            if item_type == "source"
            else models.get_folder(session, item_id)
        )
        if item is None:
            raise HTTPException(status_code=404, detail="item not found")
    finally:
        session.close()
    defaults = models.item_gdl_defaults()
    yaml_default = bool(
        (item.get("effective_config") or {}).get(
            "metadata_yaml", defaults.get("metadata_yaml")
        )
    )
    targets = _metadata_targets(item_type, item, True)
    reports = [metadata.scan_directory(path) for path in targets]
    return {
        "ok": True,
        "item_type": item_type,
        "item_id": item_id,
        "yaml_default": yaml_default,
        "targets": targets,
        "total": {"json": sum(rep["json"] for rep in reports)},
        "reports": reports,
    }


def _run_metadata_migrate(item_type, item_id, yaml, dry_run, include_alt) -> dict:
    session = get_session()
    try:
        item = (
            models.get_source(session, item_id)
            if item_type == "source"
            else models.get_folder(session, item_id)
        )
        if item is None:
            return {"ok": False, "error": "item not found"}
        targets = _metadata_targets(item_type, item, include_alt)
    finally:
        session.close()
    reports = []
    total = {"moved": 0, "converted": 0, "stray_before": 0}
    for directory in targets:
        if dry_run:
            scan = metadata.scan_directory(directory)
            reports.append({
                "directory": directory,
                "stray_before": scan["json"],
                "moved": 0,
                "converted": 0,
                "dry_run": True,
            })
            total["stray_before"] += scan["json"]
            continue
        report = metadata.migrate_directory(directory, yaml=bool(yaml))
        reports.append(report)
        total["moved"] += report.get("moved", 0)
        total["converted"] += report.get("converted", 0)
        total["stray_before"] += report.get("stray_before", 0)
    if not dry_run and (total["moved"] or total["converted"]):
        error_logger.log(
            "INFO",
            "metadata",
            "메타데이터 정리 · 이동 %s · YAML %s" % (total["moved"], total["converted"]),
            source_id=item_id if item_type == "source" else None,
            scope=item_type,
        )
    return {
        "ok": True,
        "item_type": item_type,
        "item_id": item_id,
        "dry_run": dry_run,
        "yaml": bool(yaml),
        "targets": targets,
        "total": total,
        "reports": reports,
    }


def _run_metadata_migrate_all(yaml, dry_run, include_alt) -> dict:
    session = get_session()
    try:
        sources = models.list_all_sources(session)
        folder_rows = models.all_folders_flat(session)
        folders = [
            folder
            for folder in (models.get_folder(session, row["id"]) for row in folder_rows)
            if folder
        ]
    finally:
        session.close()
    items = [("source", row) for row in sources] + [("folder", row) for row in folders]
    seen = set()
    reports = []
    total = {"moved": 0, "converted": 0, "stray_before": 0}
    for item_type, item in items:
        for directory in _metadata_targets(item_type, item, include_alt):
            if directory in seen:
                continue
            seen.add(directory)
            if dry_run:
                scan = metadata.scan_directory(directory)
                reports.append({
                    "directory": directory,
                    "item_type": item_type,
                    "item_id": item.get("id"),
                    "stray_before": scan["json"],
                    "moved": 0,
                    "converted": 0,
                    "dry_run": True,
                })
                total["stray_before"] += scan["json"]
                continue
            report = metadata.migrate_directory(directory, yaml=bool(yaml))
            report["item_type"] = item_type
            report["item_id"] = item.get("id")
            reports.append(report)
            total["moved"] += report.get("moved", 0)
            total["converted"] += report.get("converted", 0)
            total["stray_before"] += report.get("stray_before", 0)
    if not dry_run and (total["moved"] or total["converted"]):
        error_logger.log(
            "INFO",
            "metadata",
            "메타데이터 일괄 정리 · 이동 %s · YAML %s" % (total["moved"], total["converted"]),
            scope="system",
        )
    return {
        "ok": True,
        "all": True,
        "dry_run": dry_run,
        "yaml": bool(yaml),
        "target_count": len(seen),
        "total": total,
        "reports": reports,
    }


def _resolve_migrate_yaml(payload: MetadataMigratePayload) -> bool:
    if payload.yaml is not None:
        return bool(payload.yaml)
    return bool(models.item_gdl_defaults().get("metadata_yaml"))


@app.get("/api/sources/{source_id}/metadata-status")
def metadata_status_source(source_id: int) -> dict:
    return _metadata_status_for("source", source_id)


@app.get("/api/folders/{folder_id}/metadata-status")
def metadata_status_folder(folder_id: int) -> dict:
    return _metadata_status_for("folder", folder_id)


@app.post("/api/sources/{source_id}/metadata-migrate")
async def metadata_migrate_source(
    source_id: int, payload: MetadataMigratePayload | None = None
) -> dict:
    payload = payload or MetadataMigratePayload()
    yaml = _resolve_migrate_yaml(payload)
    result = await asyncio.to_thread(
        _run_metadata_migrate, "source", source_id, yaml, payload.dry_run, payload.include_alt
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "failed"))
    return result


@app.post("/api/folders/{folder_id}/metadata-migrate")
async def metadata_migrate_folder(
    folder_id: int, payload: MetadataMigratePayload | None = None
) -> dict:
    payload = payload or MetadataMigratePayload()
    yaml = _resolve_migrate_yaml(payload)
    result = await asyncio.to_thread(
        _run_metadata_migrate, "folder", folder_id, yaml, payload.dry_run, payload.include_alt
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "failed"))
    return result


@app.post("/api/metadata/migrate")
async def metadata_migrate_all(payload: MetadataMigratePayload | None = None) -> dict:
    payload = payload or MetadataMigratePayload()
    yaml = _resolve_migrate_yaml(payload)
    return await asyncio.to_thread(
        _run_metadata_migrate_all, yaml, payload.dry_run, payload.include_alt
    )


@app.get("/api/sources/{source_id}/logs")
def source_logs(source_id: int, status: str | None = None, limit: int = 20) -> dict:
    session = _session()
    try:
        source = models.get_source(session, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        return models.list_logs(session, limit=limit, source_id=source_id, status=status)
    finally:
        session.close()


@app.post("/api/sources/{source_id}/relearn")
def relearn_source(source_id: int, replace: bool = Query(default=False)) -> dict:
    session = _session()
    try:
        source = models.get_source(session, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        result = models.backfill_source_learning(
            session, source_id, float(SETTINGS.get("kdeClusterMinutes") or 5), replace=replace
        )
        if result is None:
            raise HTTPException(status_code=404, detail="source not found")
        return {"ok": True, **result}
    finally:
        session.close()


@app.post("/api/folders/{folder_id}/relearn")
def relearn_folder(folder_id: int, replace: bool = Query(default=False)) -> dict:
    session = _session()
    try:
        folder = models.get_folder(session, folder_id)
        if folder is None:
            raise HTTPException(status_code=404, detail="folder not found")
        # gather descendant source ids
        flat = models.all_folders_flat(session)
        subtree = {folder_id}
        changed = True
        while changed:
            changed = False
            for item in flat:
                if item["parent_id"] in subtree and item["id"] not in subtree:
                    subtree.add(item["id"])
                    changed = True
        source_ids = [
            src["id"]
            for src in models.list_all_sources(session)
            if src.get("folder_id") in subtree
        ]
        summaries = []
        for sid in source_ids:
            result = models.backfill_source_learning(
                session, sid, float(SETTINGS.get("kdeClusterMinutes") or 5), replace=replace
            )
            if result:
                summaries.append(result)
        return {
            "ok": True,
            "sources": len(source_ids),
            "scanned": sum(item["scanned"] for item in summaries),
            "added": sum(item["added"] for item in summaries),
        }
    finally:
        session.close()


@app.post("/api/sources/download")
async def download_source(payload: DownloadRequest) -> dict:
    ids = payload.source_ids or ([payload.source_id] if payload.source_id else [])
    overrides = None
    if payload.full:
        overrides = {"date_mode": "full"}
    elif payload.date_after:
        overrides = {"date_mode": "fixed", "date_fixed": payload.date_after}
    return _start_collection(ids, overrides)


def _quick_download_directory(payload: QuickDownloadPayload) -> str:
    if payload.directory:
        return utils.compute_base_path(payload.directory) if not os.path.isabs(payload.directory) else os.path.normpath(payload.directory)
    if payload.folder_id is not None:
        session = _session()
        try:
            folder = models.get_folder(session, payload.folder_id)
        finally:
            session.close()
        if folder and folder.get("save_path"):
            return folder["save_path"]
    return utils.compute_base_path("")


def _quick_download_options(payload: QuickDownloadPayload) -> list[str]:
    options: list[str] = []
    if payload.cookies is False:
        return options
    try:
        site_name, _key = site_url.url_to_site_key(payload.url or "")
        norm = site_url.normalize_site(site_name) or site_name
    except Exception:
        norm = ""
    if not norm:
        return options
    session = get_session()
    try:
        profile = models.default_profile_for_site(session, norm)
    finally:
        session.close()
    if profile and profile.get("context_dir") and profile.get("id") is not None:
        cookie_path = browser_manager.cookie_file_path(
            profile.get("context_dir"), profile.get("id")
        )
        if os.path.isfile(cookie_path):
            options.extend(["--cookies", cookie_path])
    return options


@app.post("/api/quick-download")
async def quick_download(payload: QuickDownloadPayload) -> dict:
    """DB에 소스를 등록하지 않고 URL 하나를 즉시 단발 수집한다."""
    url = (payload.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL을 입력하세요.")
    directory = _quick_download_directory(payload)
    options = await asyncio.to_thread(_quick_download_options, payload)
    await asyncio.to_thread(utils.ensure_dir, directory)
    try:
        result = await gdl_executor.run_gdl(
            url,
            directory,
            options=options,
            metadata_yaml=bool(models.item_gdl_defaults().get("metadata_yaml")),
        )
    except Exception as exc:  # pragma: no cover - 환경 의존
        error_logger.log("ERROR", "quick", "단발성 수집 실패: " + str(exc), scope="system")
        raise HTTPException(status_code=500, detail=str(exc))
    ok = bool(result.get("ok"))
    error_logger.log(
        "SUCCESS" if ok else "ERROR",
        "quick",
        ("단발성 수집 완료 · %s" if ok else "단발성 수집 실패 · %s") % url,
        scope="system",
        context={
            "url": url,
            "directory": directory,
            "errors": result.get("errors"),
            "yaml_count": result.get("yaml_count"),
        },
    )
    return {
        "ok": ok,
        "url": url,
        "directory": directory,
        "errors": result.get("errors") or [],
        "yaml_count": result.get("yaml_count", 0),
    }


def _open_in_explorer(path: str) -> None:
    """OS 파일 탐색기로 경로를 연다 (파일이면 선택 상태로)."""
    if platform.system() == "Windows":
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(os.path.normpath(path))  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", "-R", path] if os.path.isfile(path) else ["open", path])
    else:
        target = path if os.path.isdir(path) else os.path.dirname(path)
        subprocess.Popen(["xdg-open", target])


def _resolve_reveal_target(raw_path: str | None) -> str | None:
    """탐색기로 열 대상 경로를 결정 (없으면 상위 폴더로 폴백)."""
    if not raw_path:
        return None
    path = os.path.normpath(raw_path)
    if os.path.exists(path):
        return path
    parent = os.path.dirname(path)
    while parent and parent != os.path.dirname(parent):
        if os.path.isdir(parent):
            return parent
        parent = os.path.dirname(parent)
    return None


def _require_local_client(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if not utils.is_loopback(client_host):
        raise HTTPException(
            status_code=403,
            detail="원격 접속에서는 파일 탐색기를 열 수 있는 권한이 없습니다.",
        )


@app.post("/api/sources/{source_id}/reveal")
def reveal_source(source_id: int, request: Request) -> dict:
    _require_local_only(request)
    directory = _source_directory(source_id)
    if directory is None:
        raise HTTPException(status_code=404, detail="source not found")
    target = _resolve_reveal_target(directory)
    if target is None:
        raise HTTPException(status_code=404, detail="저장 폴더를 찾을 수 없습니다: " + directory)
    try:
        _open_in_explorer(target)
    except Exception as exc:  # pragma: no cover - 환경 의존
        raise HTTPException(status_code=500, detail="탐색기를 열지 못했습니다: " + str(exc))
    return {"ok": True, "path": target}


@app.post("/api/folders/{folder_id}/reveal")
def reveal_folder(folder_id: int, request: Request) -> dict:
    _require_local_only(request)
    session = _session()
    try:
        folder = models.get_folder(session, folder_id)
    finally:
        session.close()
    if folder is None:
        raise HTTPException(status_code=404, detail="folder not found")
    target = _resolve_reveal_target(folder.get("save_path"))
    if target is None:
        raise HTTPException(
            status_code=404,
            detail="저장 폴더를 찾을 수 없습니다: " + str(folder.get("save_path")),
        )
    try:
        _open_in_explorer(target)
    except Exception as exc:  # pragma: no cover - 환경 의존
        raise HTTPException(status_code=500, detail="탐색기를 열지 못했습니다: " + str(exc))
    return {"ok": True, "path": target}


def _source_directory(source_id: int) -> str | None:
    session = _session()
    try:
        source = models.get_source(session, source_id)
        if source is None:
            return None
        return source.get("download_directory")
    finally:
        session.close()


def _index_source_posts(source_id: int) -> dict | None:
    """소스 하나의 게시물 인덱스를 (재)구축한다."""
    session = _session()
    try:
        source = session.get(models.Source, source_id)
        if source is None:
            return None
        return post_index.refresh_source(session, models.source_to_dict(session, source))
    finally:
        session.close()


def _relocate_alt_paths(config: dict, old_dir: str, new_dir: str) -> list[dict]:
    """이름 변경으로 원본 경로가 바뀌면 alt/cmb 대상 경로와 폴더도 함께 갱신한다.

    - 파생 기본값(`<원본>_alt`/`_cmb`)·원본 하위·원본 접두 경로는 새 이름 기준으로 치환
    - 사용자가 지정한 외부 경로는 건드리지 않는다
    - 실제 폴더가 있으면 함께 이동(rename 우선). 실패하면 로그만 남기고 경로는 새 기준으로 둔다.
    """
    if not old_dir or not new_dir:
        return []
    old_norm = os.path.normpath(old_dir)
    new_norm = os.path.normpath(new_dir)
    if old_norm == new_norm:
        return []
    moves = []
    for kind, key, suffix in (("alt", "alt_paths", "_alt"), ("cmb", "cmb_paths", "_cmb")):
        before = alt_paths.resolve_targets(config, old_norm, kind)
        entries = config.get(key)
        if isinstance(entries, (list, tuple)):
            updated = []
            for entry in entries:
                text = str(entry).strip()
                if not text or text.lower() == "default":
                    updated.append(entry)
                    continue
                resolved = os.path.normpath(
                    text if os.path.isabs(text) else os.path.join(old_norm, text)
                )
                if (
                    resolved == old_norm + suffix
                    or resolved.startswith(old_norm + os.sep)
                    or resolved.startswith(old_norm + suffix)
                ):
                    updated.append(resolved.replace(old_norm, new_norm, 1))
                else:
                    updated.append(entry)
            config[key] = updated
        after = alt_paths.resolve_targets(config, new_norm, kind)
        for old_target, new_target in zip(before, after):
            if old_target == new_target:
                continue
            if not os.path.isdir(old_target) or os.path.exists(new_target):
                continue
            result = models.move_real_directory(old_target, new_target)
            moves.append(
                {
                    "kind": kind,
                    "from": old_target,
                    "to": new_target,
                    "ok": bool(result.get("ok")),
                    "error": result.get("error"),
                }
            )
    return moves


def _apply_collected_title(source_id: int) -> dict | None:
    """수집된 메타데이터의 title 을 아이템 이름으로 반영한다.

    - `title_as_name`: 이름이 아직 title 로 설정된 적 없으면(최초 수집) 적용
    - `name_update_on_collect`: 이후 수집에서도 title 이 바뀌면 이름 갱신
    - 이름이 바뀌면 실제 폴더도 함께 이동하고, 변경 이력을 `config.__name_history` 에 남긴다.
    """
    session = _session()
    try:
        source = session.get(models.Source, source_id)
        if source is None:
            return None
        info = models.source_to_dict(session, source)
        effective = info.get("effective_config") or {}
        if effective.get("title_as_name") is False:
            return None
        directory = info.get("download_directory")
        title = utils.item_title_from_directory(directory)
        if not title:
            return None

        config = dict(info.get("config") or {})
        current = (info.get("name") or "").strip()
        first_time = not config.get("__title")

        def _remember(value):
            if config.get("__title") != value:
                config["__title"] = value
                models.update_source(session, source_id, {"config": config})

        if not (first_time or effective.get("name_update_on_collect") is not False):
            _remember(title)
            return None
        if title == current:
            _remember(title)
            return None

        new_name = title[:80]
        context = models.source_context(session, source)
        new_dir = utils.build_download_directory(
            context["save_path"],
            {"site": source.site, "key": source.key, "name": new_name, "url": source.url},
        )
        move = {"ok": True, "moved": False}
        target_existed = bool(new_dir) and os.path.exists(new_dir)
        if target_existed:
            # 같은 이름의 폴더가 이미 있음(예: 레거시 데이터가 작가명으로 저장된 경우)
            # → 파일은 옮기지 않고 이름만 갱신해, 이후 수집이 기존 폴더로 들어가게 한다.
            move = {"ok": True, "moved": False, "target_existed": True}
        elif directory and new_dir and os.path.normpath(directory) != os.path.normpath(new_dir):
            move = models.move_real_directory(directory, new_dir)
        if not move.get("ok"):
            error_logger.log(
                "WARN",
                "title",
                "이름 갱신 취소(폴더 이동 실패): %s → %s · %s"
                % (current or "(없음)", new_name, move.get("error")),
                source_id=source_id,
                scope="source",
            )
            return None
        if target_existed and directory and os.path.normpath(directory) != os.path.normpath(new_dir):
            merge = models.merge_directory(directory, new_dir)
            detail = "이동 %s · 중복정리 %s · 충돌보존 %s" % (
                merge.get("moved"),
                merge.get("skipped"),
                merge.get("renamed"),
            )
            if merge.get("removed"):
                detail += " · 남은 폴더 정리됨"
            else:
                detail += " · 남은 폴더: %s" % directory
            error_logger.log(
                "INFO",
                "title",
                "같은 이름의 폴더가 이미 있어 이름만 갱신했습니다(%s).\n기존 폴더: %s"
                % (detail, new_dir),
                source_id=source_id,
                scope="source",
            )
            move["merge"] = merge

        history = list(config.get("__name_history") or [])
        alt_moves = _relocate_alt_paths(config, directory, new_dir)
        history.append(
            {
                "at": datetime.now().isoformat(timespec="seconds"),
                "from": current,
                "to": new_name,
                "moved": bool(move.get("moved")),
                "alt": alt_moves or None,
            }
        )
        config["__name_history"] = history[-20:]
        config["__title"] = title
        models.update_source(session, source_id, {"name": new_name, "config": config})
        failed_alt = [entry for entry in alt_moves if not entry.get("ok")]
        error_logger.log(
            "INFO",
            "title",
            "이름 갱신: %s → %s%s%s"
            % (
                current or "(없음)",
                new_name,
                " (폴더 이동)" if move.get("moved") else (" (이름만 · 폴더 유지)" if target_existed else ""),
                (" (대체경로 %d개 이동)" % len(alt_moves)) if alt_moves else "",
            ),
            source_id=source_id,
            scope="source",
        )
        for entry in failed_alt:
            error_logger.log(
                "WARN",
                "title",
                "대체경로 이동 실패(%s): %s → %s · %s"
                % (entry.get("kind"), entry.get("from"), entry.get("to"), entry.get("error")),
                source_id=source_id,
                scope="source",
            )
        return {
            "source_id": source_id,
            "from": current,
            "to": new_name,
            "moved": bool(move.get("moved")),
            "alt_moves": alt_moves,
        }
    finally:
        session.close()


@app.get("/api/sources/{source_id}/images")
def source_images(source_id: int, limit: int = 4) -> dict:
    """소스 다운로드 디렉터리의 최근 이미지 목록 (썸네일 모자이크용)."""
    directory = _source_directory(source_id)
    if directory is None:
        raise HTTPException(status_code=404, detail="source not found")
    limit = max(1, min(int(limit or 4), 24))
    items = utils.list_image_files(directory, limit)
    return {
        "count": len(items),
        "images": [
            {
                "index": index,
                "name": item["name"],
                "mtime": item["mtime"],
                "url": "/api/sources/%d/images/%d" % (source_id, index),
            }
            for index, item in enumerate(items)
        ],
    }


@app.get("/api/sources/{source_id}/images/{index}")
def source_image(
    source_id: int, index: int, request: Request, full: int = 0
) -> FileResponse:
    """이미지 서빙. 로컬(loopback)이면 원본, 외부면 축소본(가능 시).

    full=1 이면 클라이언트와 무관하게 항상 원본을 반환한다.
    """
    directory = _source_directory(source_id)
    if not directory:
        raise HTTPException(status_code=404, detail="source not found")
    items = utils.list_image_files(directory)
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail="image not found")
    item = items[index]
    abs_path = os.path.realpath(item["path"])
    base = os.path.realpath(directory)
    if abs_path != base and not abs_path.startswith(base + os.sep):
        raise HTTPException(status_code=403, detail="forbidden")
    media_type = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
    client_host = request.client.host if request.client else None
    local = utils.is_loopback(client_host)
    serve_path = abs_path
    if not full and not local:
        thumb = ensure_thumbnail(abs_path, item["mtime"])
        if thumb:
            serve_path = thumb
            media_type = "image/jpeg"
    return FileResponse(serve_path, media_type=media_type)


@app.get("/api/sources/{source_id}/thumbnail")
def source_thumbnail(source_id: int, request: Request, full: int = 0) -> FileResponse:
    return source_image(source_id, 0, request, full)


# ---------------------------------------------------------------------------
# Profiles (단일 계정) & ProfileGroups (합동 프로파일)
# ---------------------------------------------------------------------------


def _profile_cookie_info(profile: dict) -> dict:
    """프로파일 인증 상태(쿠키 파일 + 사이트 토큰 설정)."""
    site = str(profile.get("site") or "").strip().lower()
    path = browser_manager.cookie_file_path(profile.get("context_dir"), profile.get("id"))
    exists = bool(path) and os.path.isfile(path)
    required = list(browser_manager.REQUIRED_COOKIES.get(site, ()))
    missing = (
        browser_manager.missing_required_cookies(site, path) if exists else list(required)
    )
    updated = None
    if exists:
        try:
            updated = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
        except OSError:
            updated = None

    problem = None
    problem_kind = None
    token_state = None
    if missing:
        problem = "쿠키 누락: " + ", ".join(missing)
        problem_kind = "cookie"
    elif site == "pixiv":
        # pixiv 는 쿠키가 아니라 gallery-dl config 의 refresh-token 으로 로그인한다.
        token_state = gdl_executor.pixiv_token_state()
        state = token_state.get("state")
        if state == "missing":
            problem = "gallery-dl config 에 pixiv refresh-token 이 없습니다."
            problem_kind = "token"
        elif state == "placeholder":
            problem = (
                "gallery-dl config(%s)의 pixiv refresh-token 이 무효해 보입니다"
                "(%d자 플레이스홀더)." % (token_state.get("source"), token_state.get("length"))
            )
            problem_kind = "token"
    return {
        "path": path,
        "exists": exists,
        "required": required,
        "missing": missing,
        "ok": problem is None,
        "updated_at": updated,
        "problem": problem,
        "problem_kind": problem_kind,
        "pixiv_token": token_state,
    }


def _with_cookie_info(payload):
    """프로파일 dict(또는 목록)에 cookie_status 를 붙인다."""
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                item["cookie_status"] = _profile_cookie_info(item)
        return payload
    if isinstance(payload, dict):
        payload["cookie_status"] = _profile_cookie_info(payload)
    return payload


@app.get("/api/profiles")
def list_profiles() -> list[dict]:
    session = _session()
    try:
        return _with_cookie_info(models.list_profiles(session))
    finally:
        session.close()


@app.get("/api/profiles/launching")
def launching_profiles() -> dict:
    return {"launching": sorted(_launching)}


@app.post("/api/profiles/{profile_id}/pixiv-token")
async def issue_pixiv_token(profile_id: int, request: Request) -> dict:
    """프로파일 브라우저로 pixiv refresh token 을 발급해 앱 config 에 저장한다."""
    _require_local_only(request)
    if profile_id in _pixiv_token_tasks:
        return {"ok": True, "started": False, "already_running": True, "profile_id": profile_id}
    _pixiv_token_tasks.add(profile_id)
    try:
        result = await pixiv_auth.issue_refresh_token(profile_id)
    except Exception as exc:  # pragma: no cover - 사용자 환경 의존
        error_logger.log(
            "ERROR", "auth", "pixiv 토큰 발급 실패 · " + str(exc),
            profile_id=profile_id, scope="profile",
        )
        return {"ok": False, "error": str(exc)}
    finally:
        _pixiv_token_tasks.discard(profile_id)
    if result.get("ok"):
        error_logger.log(
            "INFO",
            "auth",
            "pixiv refresh token 발급 완료%s" % (
                " (%s)" % result.get("user") if result.get("user") else ""
            ),
            profile_id=profile_id,
            scope="profile",
        )
    else:
        error_logger.log(
            "WARN", "auth", "pixiv 토큰 발급 실패 · " + str(result.get("error")),
            profile_id=profile_id, scope="profile",
        )
    return result


class SiteCredentialPayload(BaseModel):
    values: dict = {}


@app.get("/api/site-credentials/{site}")
def get_site_credentials(site: str) -> dict:
    """사이트 자격증명의 적용 상태(내 앱/전역 config/내장 기본값). 값 자체는 노출하지 않는다."""
    name = (site or "").strip().lower()
    if name not in gdl_executor.SITE_CREDENTIAL_KEYS:
        raise HTTPException(status_code=404, detail="지원하지 않는 사이트입니다.")
    return gdl_executor.credential_state(name)


@app.post("/api/site-credentials/{site}")
def set_site_credentials(site: str, payload: SiteCredentialPayload) -> dict:
    """사이트 자격증명을 앱 config(`data/gallery-dl.conf`)에 저장한다(빈 값 → 삭제)."""
    name = (site or "").strip().lower()
    keys = gdl_executor.SITE_CREDENTIAL_KEYS.get(name)
    if not keys:
        raise HTTPException(status_code=404, detail="지원하지 않는 사이트입니다.")
    values: dict = {}
    for key in keys:
        incoming = (payload.values or {}).get(key)
        if incoming is None:
            continue
        raw = str(incoming).strip()
        label = gdl_executor.CREDENTIAL_LABELS.get(key, key)
        if raw and len(raw) < gdl_executor.CREDENTIAL_MIN_LENGTH:
            raise HTTPException(
                status_code=400,
                detail="%s 값이 너무 짧습니다(최소 %d자)." % (label, gdl_executor.CREDENTIAL_MIN_LENGTH),
            )
        if raw and gdl_executor.looks_like_placeholder(raw):
            raise HTTPException(status_code=400, detail="%s 값이 더미(예: '...')처럼 보입니다." % label)
        values[key] = raw
    if not values:
        raise HTTPException(status_code=400, detail="저장할 값이 없습니다.")
    if not gdl_executor.save_app_config_credentials(name, values):
        raise HTTPException(status_code=500, detail="config 저장에 실패했습니다.")
    saved = [gdl_executor.CREDENTIAL_LABELS.get(k, k) for k, v in values.items() if v]
    cleared = [gdl_executor.CREDENTIAL_LABELS.get(k, k) for k, v in values.items() if not v]
    detail = []
    if saved:
        detail.append("저장 " + ", ".join(saved))
    if cleared:
        detail.append("삭제 " + ", ".join(cleared))
    error_logger.log(
        "INFO",
        "auth",
        "%s 자격증명을 앱 config 에 반영했습니다(%s). 다음 수집부터 적용됩니다." % (name, " · ".join(detail)),
        scope="system",
    )
    state = gdl_executor.credential_state(name)
    state.update({"ok": True, "saved": saved, "cleared": cleared})
    return state


@app.get("/api/profiles/{profile_id}")
def get_profile(profile_id: int) -> dict:
    session = _session()
    try:
        profile = models.get_profile(session, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        return _with_cookie_info(profile)
    finally:
        session.close()


@app.post("/api/profiles", status_code=201)
def create_profile(payload: ProfilePayload) -> dict:
    session = _session()
    try:
        return models.create_profile(session, payload.model_dump(exclude_unset=True))
    finally:
        session.close()


@app.put("/api/profiles/{profile_id}")
def update_profile(profile_id: int, payload: ProfilePayload) -> dict:
    session = _session()
    try:
        profile = models.update_profile(
            session, profile_id, payload.model_dump(exclude_unset=True)
        )
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        return profile
    finally:
        session.close()


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: int, request: Request) -> dict:
    _require_scope(request, "manage")
    session = _session()
    try:
        return {"deleted": models.delete_profile(session, profile_id)}
    finally:
        session.close()


@app.post("/api/profiles/{profile_id}/reset")
def reset_profile(profile_id: int, request: Request) -> dict:
    _require_scope(request, "manage")
    session = _session()
    try:
        profile = models.reset_profile_learning(session, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        return profile
    finally:
        session.close()


@app.post("/api/profiles/{profile_id}/default")
def set_profile_default(profile_id: int, payload: ProfileDefaultPayload) -> dict:
    session = _session()
    try:
        profile = models.set_site_default(session, profile_id, payload.value)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        return profile
    finally:
        session.close()


@app.get("/api/profiles/{profile_id}/learning")
def profile_learning(profile_id: int, limit: int = 50) -> list[dict]:
    session = _session()
    try:
        return models.list_profile_learning_events(session, profile_id, limit)
    finally:
        session.close()


@app.post("/api/profiles/{profile_id}/launch")
async def launch_profile(
    profile_id: int, request: Request, payload: ProfileLaunchPayload | None = None
) -> dict:
    _require_local_only(request)
    if not browser_manager.is_available():
        raise HTTPException(
            status_code=400,
            detail="playwright가 설치되어 있지 않습니다. `pip install playwright` 후 `playwright install chromium`을 실행하세요.",
        )
    session = _session()
    try:
        profile = models.get_profile(session, profile_id)
    finally:
        session.close()
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    if profile_id in _launching:
        return {"ok": True, "started": False, "already_running": True, "profile_id": profile_id}

    url = payload.url if payload else None

    async def _run_launch():
        try:
            await browser_manager.launch_profile_browser(profile_id, url=url)
        except Exception as exc:  # pragma: no cover - 사용자 환경 의존
            error_logger.log(
                "ERROR", "browser", "프로파일 브라우저 실행 실패 · " + str(exc),
                profile_id=profile_id, scope="profile",
            )
        finally:
            _launching.discard(profile_id)

    _launching.add(profile_id)
    task = asyncio.create_task(_run_launch())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"ok": True, "started": True, "already_running": False, "profile_id": profile_id}


@app.get("/api/profile-groups")
def list_profile_groups() -> list[dict]:
    session = _session()
    try:
        return models.list_profile_groups(session)
    finally:
        session.close()


@app.get("/api/profile-groups/{group_id}")
def get_profile_group(group_id: int) -> dict:
    session = _session()
    try:
        group = models.get_profile_group(session, group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="profile group not found")
        return group
    finally:
        session.close()


@app.post("/api/profile-groups", status_code=201)
def create_profile_group(payload: ProfileGroupPayload) -> dict:
    session = _session()
    try:
        return models.create_profile_group(session, payload.model_dump(exclude_unset=True))
    finally:
        session.close()


@app.put("/api/profile-groups/{group_id}")
def update_profile_group(group_id: int, payload: ProfileGroupPayload) -> dict:
    session = _session()
    try:
        group = models.update_profile_group(
            session, group_id, payload.model_dump(exclude_unset=True)
        )
        if group is None:
            raise HTTPException(status_code=404, detail="profile group not found")
        return group
    finally:
        session.close()


@app.delete("/api/profile-groups/{group_id}")
def delete_profile_group(group_id: int, request: Request) -> dict:
    _require_scope(request, "manage")
    session = _session()
    try:
        return {"deleted": models.delete_profile_group(session, group_id)}
    finally:
        session.close()


@app.post("/api/profile-groups/{group_id}/reset")
def reset_profile_group(group_id: int, request: Request) -> dict:
    _require_scope(request, "manage")
    session = _session()
    try:
        group = models.reset_profile_group(session, group_id)
        if group is None:
            raise HTTPException(status_code=404, detail="profile group not found")
        return group
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Reactive rules & Playwright 감시 브라우저
# ---------------------------------------------------------------------------


@app.get("/api/reactive-rules")
def list_reactive_rules() -> list[dict]:
    session = _session()
    try:
        return models.list_reactive_rules(session)
    finally:
        session.close()


@app.get("/api/reactive-rules/{rule_id}")
def get_reactive_rule(rule_id: int) -> dict:
    session = _session()
    try:
        rule = models.get_reactive_rule(session, rule_id)
        if rule is None:
            raise HTTPException(status_code=404, detail="reactive rule not found")
        return rule
    finally:
        session.close()


@app.post("/api/reactive-rules", status_code=201)
def create_reactive_rule(payload: ReactiveRulePayload) -> dict:
    session = _session()
    try:
        return models.create_reactive_rule(session, payload.model_dump(exclude_unset=True))
    finally:
        session.close()


@app.put("/api/reactive-rules/{rule_id}")
def update_reactive_rule(rule_id: int, payload: ReactiveRulePayload) -> dict:
    session = _session()
    try:
        rule = models.update_reactive_rule(
            session, rule_id, payload.model_dump(exclude_unset=True)
        )
        if rule is None:
            raise HTTPException(status_code=404, detail="reactive rule not found")
        return rule
    finally:
        session.close()


@app.delete("/api/reactive-rules/{rule_id}")
def delete_reactive_rule(rule_id: int) -> dict:
    session = _session()
    try:
        return {"deleted": models.delete_reactive_rule(session, rule_id)}
    finally:
        session.close()


@app.get("/api/watcher/status")
def watcher_status() -> dict:
    return browser_watcher.status()


@app.get("/api/watcher/events")
def watcher_events(since: int = 0) -> dict:
    events = browser_watcher.events_since(since)
    return {"events": events, "last": events[-1]["seq"] if events else int(since or 0)}


@app.post("/api/watcher/toggle")
async def watcher_toggle(payload: WatcherTogglePayload) -> dict:
    if not browser_watcher.is_available() and payload.enabled is not False:
        raise HTTPException(
            status_code=400,
            detail="playwright가 설치되어 있지 않습니다. `pip install playwright` 후 `playwright install chromium`을 실행하세요.",
        )
    if payload.headless is not None:
        SETTINGS["watcherHeadless"] = bool(payload.headless)
    if payload.sites is not None:
        SETTINGS["watcherSites"] = [str(item) for item in payload.sites]
    models.update_app_settings(
        {
            "watcherHeadless": bool(SETTINGS.get("watcherHeadless", True)),
            "watcherSites": list(SETTINGS.get("watcherSites") or []),
        }
    )

    headless = bool(SETTINGS.get("watcherHeadless", True))
    sites = list(SETTINGS.get("watcherSites") or []) or None

    running = browser_watcher.is_running()
    enabled = payload.enabled
    if enabled is None:
        enabled = True
    if not enabled:
        status = await browser_watcher.stop()
        SETTINGS["watcherAutoStart"] = False
        models.update_app_settings({"watcherAutoStart": False})
        return {"ok": True, "action": "stopped", "status": status}
    try:
        status = await browser_watcher.start(headless=headless, sites=sites)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        error_logger.log("ERROR", "watcher", "watcher start failed: " + str(exc), scope="watcher")
        raise HTTPException(status_code=500, detail=str(exc))
    SETTINGS["watcherAutoStart"] = True
    models.update_app_settings({"watcherAutoStart": True})
    return {
        "ok": True,
        "action": "restarted" if running else "started",
        "status": status,
    }


@app.post("/api/explorer/batch-move")
def batch_move(payload: BatchMovePayload, request: Request) -> dict:
    _require_scope(request, "manage")
    session = _session()
    moved = 0
    errors: list[str] = []
    try:
        for item in payload.items:
            try:
                result = models.move_item(
                    session, item.type, item.id, payload.target_folder_id
                )
                if result is not None:
                    moved += 1
            except ValueError as exc:
                errors.append(str(exc))
        return {"ok": True, "moved": moved, "errors": errors}
    finally:
        session.close()


@app.post("/api/explorer/batch-edit")
def batch_edit(payload: BatchEditPayload) -> dict:
    session = _session()
    updated = 0
    try:
        targets = models.expand_batch_items(
            session, [item.model_dump() for item in payload.items], payload.depth
        )
        for item in targets:
            models.apply_batch_fields(session, item["type"], item["id"], payload.fields)
            updated += 1
        return {"ok": True, "updated": updated, "selected": len(payload.items), "targets": len(targets)}
    finally:
        session.close()


@app.post("/api/explorer/batch-delete")
def batch_delete(payload: BatchDeletePayload, request: Request) -> dict:
    _require_scope(request, "manage")
    session = _session()
    deleted = 0
    try:
        for item in payload.items:
            if item.type == "source":
                deleted += 1 if models.delete_source(session, item.id) else 0
            elif item.type == "folder":
                deleted += 1 if models.delete_folder(session, item.id) else 0
        return {"ok": True, "deleted": deleted}
    finally:
        session.close()


@app.post("/api/explorer/batch-run")
async def batch_run(payload: BatchDeletePayload) -> dict:
    ids = [item.id for item in payload.items if item.type == "source"]
    return _start_collection(ids)


@app.post("/api/explorer/reorder")
def explorer_reorder(payload: ExplorerReorderPayload) -> dict:
    session = _session()
    try:
        try:
            count = models.reorder_children(
                session, payload.type, payload.parent_id, payload.ordered_ids
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "count": count}
    finally:
        session.close()


@app.post("/api/profiles/reorder")
def profiles_reorder(payload: ReorderPayload) -> dict:
    session = _session()
    try:
        return {"ok": True, "count": models.reorder_profiles(session, payload.ordered_ids)}
    finally:
        session.close()


@app.post("/api/profile-groups/reorder")
def profile_groups_reorder(payload: ReorderPayload) -> dict:
    session = _session()
    try:
        return {
            "ok": True,
            "count": models.reorder_profile_groups(session, payload.ordered_ids),
        }
    finally:
        session.close()


def _gather_due_alt_items() -> list[dict]:
    """N시간마다 갱신 대상(소스)을 계산한다(동기)."""
    from datetime import datetime as _dt

    session = get_session()
    try:
        now = _dt.now()
        due = []
        for src in models.list_all_sources(session):
            config = src.get("config") or {}
            if str(config.get("alt_schedule") or "manual") != "n_hours":
                continue
            try:
                interval_h = float(config.get("alt_interval_hours") or 0)
            except (TypeError, ValueError):
                interval_h = 0
            if interval_h <= 0:
                continue
            last = models.parse_dt(config.get("alt_last_sync_at"))
            if last is not None and (now - models._naive(last)).total_seconds() < interval_h * 3600:
                continue
            due.append({"type": "source", "id": src["id"]})
        return due
    finally:
        session.close()


async def _alt_scheduler_tick() -> None:
    due = await asyncio.to_thread(_gather_due_alt_items)
    for item in due:
        await asyncio.to_thread(_run_alt_sync, item["type"], item["id"], "both", False)


def _db_mark_started(source_id):
    session = get_session()
    try:
        return models.mark_run_started(session, source_id)
    finally:
        session.close()


def _db_mark_success(source_id, mtime):
    session = get_session()
    try:
        return models.mark_source_success(session, source_id, mtime)
    finally:
        session.close()


def _db_mark_failure(source_id, errors):
    session = get_session()
    try:
        return models.mark_source_failure(session, source_id, errors)
    finally:
        session.close()


def _maybe_initial_backfill(source_id):
    """이력이 비어 있으면 최초 1회 과거 파일 스캔으로 KDE 학습."""
    if not SETTINGS.get("kdeBackfillOnFirstRun", True):
        return None
    session = get_session()
    try:
        source = session.get(models.Source, source_id)
        if source is None:
            return None
        already = len(models.kde.parse_history(source.upload_time_history))
        if already > 0:
            return None
        return models.backfill_source_learning(
            session, source_id, float(SETTINGS.get("kdeClusterMinutes") or 5), replace=False
        )
    finally:
        session.close()


def _record_learning(source_id, found, mtime):
    session = get_session()
    try:
        source = session.get(models.Source, source_id)
        if source is None:
            return None
        if found:
            moment = models._naive(mtime) or datetime.now()
            models.record_upload_event(session, source_id, moment)
        new_cycle = kde.update_cycle_ema(source.learned_upload_cycle, found)
        models.set_source_cycle(session, source_id, new_cycle)
        return models.get_source(session, source_id)
    finally:
        session.close()


def _start_collection(source_ids, overrides=None) -> dict:
    unique: list[int] = []
    for source_id in source_ids:
        if source_id is None or source_id in unique:
            continue
        unique.append(source_id)
    session = get_session()
    try:
        sources = [models.get_source(session, sid) for sid in unique]
        sources = [source for source in sources if source]
    finally:
        session.close()
    started = []
    for source in sources:
        if source["id"] in _running:
            started.append({"id": source["id"], "name": source["name"], "already_running": True})
            continue
        task = asyncio.create_task(_collect_source(source["id"], overrides))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        started.append({"id": source["id"], "name": source["name"], "already_running": False})
    return {
        "ok": True,
        "queued": len([item for item in started if not item["already_running"]]),
        "started": started,
    }


_auth_warned_at: dict[str, float] = {}
_AUTH_WARN_INTERVAL = 1800.0


def _warn_auth_setup(source, profile_id, source_id) -> None:
    """수집 시작 전에 인증 준비 상태(프로파일 쿠키 / pixiv 토큰)를 점검해 안내한다.

    같은 대상에는 30분에 한 번만 기록한다(소스가 많을 때 로그 폭주 방지).
    """
    site = str(source.get("site") or "").strip().lower()
    now = time.monotonic()

    if site == "pixiv":
        state = gdl_executor.pixiv_token_state()
        if state.get("state") not in ("missing", "placeholder"):
            return
        if now - _auth_warned_at.get("pixiv", 0.0) < _AUTH_WARN_INTERVAL:
            return
        _auth_warned_at["pixiv"] = now
        error_logger.log(
            "WARN",
            "auth",
            "pixiv gallery-dl refresh-token 이 %s 상태입니다(전역 config %s). "
            "`gallery-dl oauth:pixiv` 로 재발급하거나 값를 유효한 토큰으로 바꾸세요."
            % (
                "없음" if state.get("state") == "missing" else "무효(플레이스홀더)",
                state.get("source") or "-",
            ),
            source_id=source_id,
            profile_id=profile_id,
            scope="source",
        )
        return

    if not profile_id or site not in browser_manager.REQUIRED_COOKIES:
        return
    key = "cookie:%s" % profile_id
    if now - _auth_warned_at.get(key, 0.0) < _AUTH_WARN_INTERVAL:
        return
    session = _session()
    try:
        profile = models.get_profile(session, int(profile_id))
    finally:
        session.close()
    if not profile:
        return
    path = browser_manager.cookie_file_path(profile.get("context_dir"), profile_id)
    missing = browser_manager.missing_required_cookies(site, path)
    if not missing:
        return
    _auth_warned_at[key] = now
    error_logger.log(
        "WARN",
        "auth",
        "%s 프로파일(%s) 쿠키에 %s 이(가) 없습니다. 프로파일을 '실행'해 해당 사이트 계정으로 다시 로그인하세요."
        % (site, profile.get("name") or profile_id, ", ".join(missing)),
        source_id=source_id,
        profile_id=int(profile_id),
        scope="source",
    )


async def _collect_source(source_id: int, overrides: dict | None = None) -> dict:
    if source_id in _running:
        return {"id": source_id, "status": "running"}
    _running.add(source_id)
    cancel_token = gdl_executor.CancelToken()
    _cancel_tokens[source_id] = cancel_token
    sem = _collect_semaphore()
    acquired = False
    try:
        await sem.acquire()
        acquired = True
        session = get_session()
        try:
            source = models.get_source(session, source_id)
        finally:
            session.close()
        if not source:
            return {"id": source_id, "status": "missing"}

        offset = float(SETTINGS.get("offset_buffer") or 0)
        config = dict(source.get("effective_config") or {})
        if overrides:
            config.update(overrides)
        raw_options = config.get("args")
        options = [str(item) for item in raw_options] if isinstance(raw_options, list) else []
        gdl_defaults = models.item_gdl_defaults()
        date_basis = str(config.get("date_basis") or gdl_defaults.get("date_basis") or "filter")
        date_after = None
        date_filter = None
        if date_basis == "date-after":
            date_after = utils.resolve_date_after(
                source, source.get("date_basis_order"), offset, config
            )
        else:
            date_filter = utils.resolve_date_filter(
                source, source.get("date_basis_order"), offset, config
            )
        sleep_val = config.get("sleep", gdl_defaults.get("sleep"))
        sleep_request_val = config.get("sleep_request", gdl_defaults.get("sleep_request"))
        retries_val = config.get("retries", gdl_defaults.get("retries"))
        timeout_val = config.get("timeout", gdl_defaults.get("timeout"))
        limit_rate_val = config.get("limit_rate", gdl_defaults.get("limit_rate"))
        directory = utils.build_download_directory(source.get("save_path"), source)

        await asyncio.to_thread(utils.ensure_dir, directory)
        group_id = source.get("effective_profile_group_id")
        profile_id = source.get("effective_profile_id")
        try:
            await asyncio.to_thread(_warn_auth_setup, source, profile_id, source_id)
        except Exception:  # 진단 실패가 수집을 막지 않도록
            pass

        start_logged = {"done": False}

        async def _on_started(sid, command=None):
            await asyncio.to_thread(_db_mark_started, sid)
            if start_logged["done"]:
                return
            start_logged["done"] = True
            command = command or []
            command_str = " ".join(str(part) for part in command)
            await asyncio.to_thread(
                error_logger.log,
                "INFO",
                "run",
                "수집 시작 · %s\n$ %s" % (
                    source.get("url") or source.get("name"),
                    command_str,
                ),
                source_id=source_id,
                profile_id=profile_id,
                folder_id=source.get("folder_id"),
                scope="source",
                context={
                    "directory": directory,
                    "date_after": date_after,
                    "date_filter": date_filter,
                    "options": options,
                    "command": command,
                    "command_str": command_str,
                },
            )

        async def _log_errors(sid, errors):
            await asyncio.to_thread(error_logger.log_errors, sid, errors)

        retry_delay = float(config.get("retry_delay") if config.get("retry_delay") is not None
                            else models.item_retry_delay())
        max_retries = int(config.get("max_retries") if config.get("max_retries") is not None else 2)
        retries = 0
        engine = {}
        while True:
            engine = await gdl_executor.collect_with_failover(
                source,
                directory,
                options,
                date_after,
                group_id,
                on_started=_on_started,
                log_errors=_log_errors,
                profile_id=profile_id,
                date_filter=date_filter,
                sleep=sleep_val,
                sleep_request=sleep_request_val,
                retries=int(retries_val) if retries_val is not None else None,
                timeout=float(timeout_val) if timeout_val is not None else None,
                limit_rate=limit_rate_val,
                metadata_yaml=bool(config.get("metadata_yaml", gdl_defaults.get("metadata_yaml"))),
                cancel_token=cancel_token,
            )
            if engine.get("ok") or engine.get("cancelled"):
                break
            # 프로파일 할당량(rate_limit) 이슈는 재시도 규칙 예외 → 즉시 종료(쿨다운 학습)
            kind = error_logger.classify_category(engine.get("errors"))
            if kind in ("rate_limit", "short_rate") or retries >= max_retries:
                break
            retries += 1
            await asyncio.to_thread(
                error_logger.log,
                "WARN",
                "run",
                "동일 오류 재시도 %d/%d (%ss 후)" % (retries, max_retries, retry_delay),
                source_id=source_id,
                profile_id=profile_id,
                folder_id=source.get("folder_id"),
                scope="source",
            )
            await asyncio.sleep(max(0.0, retry_delay))
        engine["retries"] = retries

        if engine.get("cancelled"):
            await asyncio.to_thread(
                error_logger.log,
                "INFO",
                "run",
                "수집을 취소했습니다.",
                source_id=source_id,
                profile_id=profile_id,
                scope="source",
            )
            return {
                "id": source_id,
                "name": source.get("name"),
                "status": "cancelled",
                "directory": directory,
            }

        if engine.get("ok"):
            mtime = await asyncio.to_thread(utils.newest_file_mtime, directory)
            found = bool(engine.get("usage_delta"))
            await asyncio.to_thread(_db_mark_success, source_id, mtime)
            learned =             await asyncio.to_thread(_record_learning, source_id, found, mtime)
            await asyncio.to_thread(_maybe_initial_backfill, source_id)
            try:
                await asyncio.to_thread(_apply_collected_title, source_id)
            except Exception as exc:  # 이름 갱신 실패가 수집을 막지 않도록
                error_logger.log(
                    "WARN",
                    "title",
                    "이름 갱신 실패: " + str(exc),
                    source_id=source_id,
                    scope="source",
                )
            if SETTINGS.get("recommendAutoIndex"):
                try:
                    await asyncio.to_thread(_index_source_posts, source_id)
                except Exception as exc:  # 인덱싱 실패가 수집을 막지 않도록
                    error_logger.log(
                        "WARN",
                        "recommend",
                        "게시물 인덱스 갱신 실패: " + str(exc),
                        source_id=source_id,
                        scope="source",
                    )
            if str((config or {}).get("alt_schedule") or "manual") == "on_run":
                await asyncio.to_thread(_run_alt_sync, "source", source_id, "both", False)
            await asyncio.to_thread(error_logger.auto_resolve_related, source_id)
            await asyncio.to_thread(
                error_logger.log,
                "SUCCESS",
                "run",
                "수집 완료 · 신규 %s개 · 시도 %s · 우회 %s" % (
                    engine.get("usage_delta", 0),
                    engine.get("attempts", 1),
                    len(engine.get("failovers") or []),
                ),
                source_id=source_id,
                profile_id=profile_id,
                folder_id=source.get("folder_id"),
                scope="source",
                context={
                    "usage_delta": engine.get("usage_delta", 0),
                    "attempts": engine.get("attempts"),
                    "profile": (engine.get("profile") or {}).get("name"),
                    "directory": directory,
                },
            )
            await _notify(
                "success",
                source_name=source.get("name") or ("소스 #%d" % source_id),
                source_id=source_id,
                message="%s · 신규 %d개%s"
                % (
                    source.get("url") or "",
                    int(engine.get("usage_delta") or 0),
                    " · " + directory if directory else "",
                ),
            )
            return {
                "id": source_id,
                "name": source.get("name"),
                "status": "done",
                "directory": directory,
                "date_after": date_after,
                "mtime": mtime.isoformat() if mtime else None,
                "profile": engine.get("profile"),
                "is_probe": engine.get("is_probe"),
                "attempts": engine.get("attempts"),
                "found_new": found,
                "ai_state": (learned or {}).get("ai_state"),
            }

        await asyncio.to_thread(_db_mark_failure, source_id, engine.get("errors"))
        await asyncio.to_thread(
            error_logger.log,
            "ERROR",
            error_logger.classify_category(engine.get("errors")),
            "수집 실패 · " + error_logger.summarize_errors(engine.get("errors")),
            source_id=source_id,
            profile_id=profile_id,
            folder_id=source.get("folder_id"),
            scope="source",
            context={
                "returncode": engine.get("returncode"),
                "attempts": engine.get("attempts"),
                "directory": directory,
            },
        )
        await _notify(
            "error",
            source_name=source.get("name") or ("소스 #%d" % source_id),
            source_id=source_id,
            message=error_logger.summarize_errors(engine.get("errors")),
        )
        return {
            "id": source_id,
            "name": source.get("name"),
            "status": "error",
            "directory": directory,
            "date_after": date_after,
            "returncode": engine.get("returncode"),
            "errors": engine.get("errors"),
            "failovers": engine.get("failovers"),
        }
    except Exception as exc:
        await asyncio.to_thread(_db_mark_failure, source_id, [{"code": None, "message": str(exc)}])
        await asyncio.to_thread(
            error_logger.log,
            "ERROR",
            "run",
            "수집 예외 · " + str(exc),
            source_id=source_id,
            scope="source",
        )
        await _notify(
            "error",
            source_name=source.get("name") or ("소스 #%d" % source_id),
            source_id=source_id,
            message=str(exc),
        )
        return {"id": source_id, "status": "error", "message": str(exc)}
    finally:
        if acquired:
            sem.release()
        _cancel_tokens.pop(source_id, None)
        _running.discard(source_id)


@app.get("/api/explorer/running")
def running_sources() -> dict:
    return {
        "running": sorted(_running),
        "stopping": sorted(
            source_id for source_id, token in _cancel_tokens.items() if token.cancelled
        ),
    }


@app.post("/api/sources/{source_id}/stop")
def stop_source(source_id: int, request: Request) -> dict:
    """실행 중인 수집을 취소한다(원격 제어의 핵심 동작)."""
    _require_scope(request, "control")
    if source_id not in _running:
        return {"ok": False, "reason": "not_running", "source_id": source_id}
    token = _cancel_tokens.get(source_id)
    if token is None:
        return {"ok": False, "reason": "not_cancellable", "source_id": source_id}
    token.cancel()
    error_logger.log(
        "INFO", "run", "수집 정지를 요청했습니다.", source_id=source_id, scope="source"
    )
    return {"ok": True, "source_id": source_id, "stopping": True}


# ---------------------------------------------------------------------------
# Logs (아이템/시스템 통합 로그 + 해결 시스템)
# ---------------------------------------------------------------------------


class LogResolvePayload(BaseModel):
    resolution: str | None = None


class LogClearPayload(BaseModel):
    level: str | None = None
    status: str | None = None
    source_id: int | None = None


@app.get("/api/logs")
def list_logs(
    level: str | None = None,
    category: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    source_id: int | None = None,
    folder_id: int | None = None,
    profile_id: int | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    session = _session()
    try:
        return models.list_logs(
            session,
            limit=limit,
            offset=offset,
            level=level,
            category=category,
            status=status,
            scope=scope,
            source_id=source_id,
            folder_id=folder_id,
            profile_id=profile_id,
            query=q,
        )
    finally:
        session.close()


@app.get("/api/logs/stats")
def log_stats() -> dict:
    session = _session()
    try:
        return models.log_stats(session)
    finally:
        session.close()


@app.get("/api/logs/{log_id}")
def get_log(log_id: int) -> dict:
    session = _session()
    try:
        row = models.get_log(session, log_id)
        if row is None:
            raise HTTPException(status_code=404, detail="log not found")
        row["guide"] = error_logger.guide_for(row.get("category"), row.get("error_code"), row.get("message"))
        return row
    finally:
        session.close()


@app.post("/api/logs/{log_id}/resolve")
def resolve_log(log_id: int, payload: LogResolvePayload | None = None) -> dict:
    session = _session()
    try:
        row = models.resolve_log(
            session, log_id, payload.resolution if payload else None
        )
        if row is None:
            raise HTTPException(status_code=404, detail="log not found")
        return row
    finally:
        session.close()


@app.post("/api/logs/{log_id}/ignore")
def ignore_log(log_id: int, payload: LogResolvePayload | None = None) -> dict:
    session = _session()
    try:
        row = models.ignore_log(session, log_id, payload.resolution if payload else None)
        if row is None:
            raise HTTPException(status_code=404, detail="log not found")
        return row
    finally:
        session.close()


@app.post("/api/logs/{log_id}/retry")
async def retry_log(log_id: int) -> dict:
    session = _session()
    try:
        row = models.get_log(session, log_id)
    finally:
        session.close()
    if row is None:
        raise HTTPException(status_code=404, detail="log not found")
    if not row.get("source_id"):
        raise HTTPException(status_code=400, detail="연결된 소스가 없어 재시도할 수 없습니다.")
    return _start_collection([row["source_id"]])


@app.delete("/api/logs/{log_id}")
def delete_log(log_id: int, request: Request) -> dict:
    _require_scope(request, "manage")
    session = _session()
    try:
        return {"deleted": models.delete_log(session, log_id)}
    finally:
        session.close()


@app.post("/api/logs/clear")
def clear_logs(payload: LogClearPayload, request: Request) -> dict:
    _require_scope(request, "manage")
    session = _session()
    try:
        count = models.clear_logs(
            session, level=payload.level, status=payload.status, source_id=payload.source_id
        )
        return {"ok": True, "deleted": count}
    finally:
        session.close()


class RecommendRefreshPayload(BaseModel):
    source_id: int | None = None


@app.get("/api/recommendations")
def recommendations(
    days: int | None = None,
    site: str = "",
    q: str = "",
    limit: int = 24,
    include_sensitive: int | None = None,
) -> dict:
    """최근글 / 인기글 / 드문 업로더 종합."""
    days_value = int(days if days is not None else (SETTINGS.get("recommendRangeDays") or 0) or 0)
    include = (
        bool(SETTINGS.get("recommendIncludeSensitive"))
        if include_sensitive is None
        else bool(include_sensitive)
    )
    limit = max(1, min(int(limit or 24), 100))
    weights = post_index.weights_from_settings(SETTINGS)
    site_value = (site or "").strip() or None
    query_value = (q or "").strip() or None
    session = _session()
    try:
        return {
            "days": days_value,
            "site": site_value,
            "query": query_value,
            "limit": limit,
            "recent": post_index.section_recent(
                session, days_value, limit, site_value, include, query_value, weights
            ),
            "popular": post_index.section_popular(
                session,
                days_value,
                limit,
                SETTINGS.get("recommendMinEngagement") or 0,
                site_value,
                include,
                query_value,
                weights,
            ),
            "rare_uploaders": post_index.section_rare_uploaders(
                session,
                days_value,
                12,
                SETTINGS.get("recommendRareMaxPosts") or 0,
                SETTINGS.get("recommendRareMinGapHours") or 0,
                site_value,
                include,
                query_value,
                weights,
            ),
            "sites": post_index.site_options(session),
            "stats": post_index.stats(session, days_value),
            "generated_at": datetime.now().isoformat(),
        }
    finally:
        session.close()


@app.get("/api/recommendations/status")
def recommendations_status() -> dict:
    session = _session()
    try:
        return post_index.stats(session, int(SETTINGS.get("recommendRangeDays") or 0))
    finally:
        session.close()


@app.post("/api/recommendations/refresh")
def recommendations_refresh(payload: RecommendRefreshPayload | None = None) -> dict:
    """게시물 인덱스를 (재)구축한다. source_id 지정 시 해당 소스만."""
    payload = payload or RecommendRefreshPayload()
    session = _session()
    try:
        if payload.source_id:
            source = models.get_source(session, int(payload.source_id))
            if source is None:
                raise HTTPException(status_code=404, detail="source not found")
            # get_source 는 이미 source_to_dict 결과(dict)를 준다
            result = post_index.refresh_source(session, source)
        else:
            result = post_index.refresh_all(session)
    finally:
        session.close()
    error_logger.log(
        "INFO",
        "recommend",
        "게시물 인덱스 갱신",
        scope="system",
        context={"created": result.get("created"), "updated": result.get("updated")},
    )
    return {"ok": True, "result": result, "stats": recommendations_status()}


@app.get("/api/favicon")
def site_favicon(url: str, refresh: int = 0) -> FileResponse:
    """소스 URL 의 사이트 파비콘(브라우저 탭 아이콘)을 캐시해 서빙한다.

    없으면 받아서 `data/favicons/<host>.<ext>` 에 저장하고, 실패하면 404(프론트는 글자 배지로 폴백).
    """
    path = favicons.ensure(url, refresh=bool(refresh))
    if not path:
        raise HTTPException(status_code=404, detail="favicon not found")
    media_type = mimetypes.guess_type(path)[0] or "image/x-icon"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/posts/{post_row_id}/thumbnail")
def post_thumbnail(post_row_id: int, request: Request, full: int = 0) -> FileResponse:
    """인덱스된 게시물의 대표 미디어를 서빙(외부 접속은 축소본)."""
    session = _session()
    try:
        row = session.get(models.PostIndex, post_row_id)
        if row is None:
            raise HTTPException(status_code=404, detail="post not found")
        source_id = row.source_id
        media_rel = row.media_rel
    finally:
        session.close()
    directory = _source_directory(source_id) if source_id else None
    if not directory or not media_rel:
        raise HTTPException(status_code=404, detail="media not found")
    abs_path = os.path.realpath(os.path.join(directory, media_rel))
    base = os.path.realpath(directory)
    if abs_path != base and not abs_path.startswith(base + os.sep):
        raise HTTPException(status_code=403, detail="forbidden")
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail="media not found")
    media_type = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
    client_host = request.client.host if request.client else None
    serve_path = abs_path
    if not full and not utils.is_loopback(client_host):
        thumb = ensure_thumbnail(abs_path, os.path.getmtime(abs_path))
        if thumb:
            serve_path = thumb
            media_type = "image/jpeg"
    return FileResponse(serve_path, media_type=media_type)


@app.get("/recommend")
def recommend_page() -> FileResponse:
    """추천 페이지(새 탭용 독립 문서)."""
    return FileResponse(os.path.join(FRONTEND_DIR, "recommend.html"), media_type="text/html")


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


def wait_for_server(port: int, host: str = "127.0.0.1", timeout: float = 15.0) -> bool:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False
