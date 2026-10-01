import asyncio
import hashlib
import mimetypes
import os
import platform
import socket
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

import scheduler
from core import (
    alt_paths,
    browser_manager,
    browser_watcher,
    error_logger,
    gdl_executor,
    kde,
    models,
    site_url,
    sites,
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
}
SETTINGS = dict(DEFAULT_SETTINGS)

_running: set[int] = set()
_launching: set[int] = set()
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
    try:
        yield
    finally:
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
def delete_folder(folder_id: int) -> dict:
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
def delete_source(source_id: int) -> dict:
    session = _session()
    try:
        return {"deleted": models.delete_source(session, source_id)}
    finally:
        session.close()


class AltSyncPayload(BaseModel):
    kind: str = "both"


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
async def alt_prune_source(source_id: int, payload: AltSyncPayload | None = None) -> dict:
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
    _require_local_client(request)
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
    _require_local_client(request)
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


@app.get("/api/profiles")
def list_profiles() -> list[dict]:
    session = _session()
    try:
        return models.list_profiles(session)
    finally:
        session.close()


@app.get("/api/profiles/launching")
def launching_profiles() -> dict:
    return {"launching": sorted(_launching)}


@app.get("/api/profiles/{profile_id}")
def get_profile(profile_id: int) -> dict:
    session = _session()
    try:
        profile = models.get_profile(session, profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="profile not found")
        return profile
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
def delete_profile(profile_id: int) -> dict:
    session = _session()
    try:
        return {"deleted": models.delete_profile(session, profile_id)}
    finally:
        session.close()


@app.post("/api/profiles/{profile_id}/reset")
def reset_profile(profile_id: int) -> dict:
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
async def launch_profile(profile_id: int, payload: ProfileLaunchPayload | None = None) -> dict:
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
def delete_profile_group(group_id: int) -> dict:
    session = _session()
    try:
        return {"deleted": models.delete_profile_group(session, group_id)}
    finally:
        session.close()


@app.post("/api/profile-groups/{group_id}/reset")
def reset_profile_group(group_id: int) -> dict:
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
def batch_move(payload: BatchMovePayload) -> dict:
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
        for item in payload.items:
            models.apply_batch_fields(session, item.type, item.id, payload.fields)
            updated += 1
        return {"ok": True, "updated": updated}
    finally:
        session.close()


@app.post("/api/explorer/batch-delete")
def batch_delete(payload: BatchDeletePayload) -> dict:
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


async def _collect_source(source_id: int, overrides: dict | None = None) -> dict:
    if source_id in _running:
        return {"id": source_id, "status": "running"}
    _running.add(source_id)
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
            )
            if engine.get("ok"):
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

        if engine.get("ok"):
            mtime = await asyncio.to_thread(utils.newest_file_mtime, directory)
            found = bool(engine.get("usage_delta"))
            await asyncio.to_thread(_db_mark_success, source_id, mtime)
            learned =             await asyncio.to_thread(_record_learning, source_id, found, mtime)
            await asyncio.to_thread(_maybe_initial_backfill, source_id)
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
        return {"id": source_id, "status": "error", "message": str(exc)}
    finally:
        if acquired:
            sem.release()
        _running.discard(source_id)


@app.get("/api/explorer/running")
def running_sources() -> dict:
    return {"running": sorted(_running)}


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
def delete_log(log_id: int) -> dict:
    session = _session()
    try:
        return {"deleted": models.delete_log(session, log_id)}
    finally:
        session.close()


@app.post("/api/logs/clear")
def clear_logs(payload: LogClearPayload) -> dict:
    session = _session()
    try:
        count = models.clear_logs(
            session, level=payload.level, status=payload.status, source_id=payload.source_id
        )
        return {"ok": True, "deleted": count}
    finally:
        session.close()


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
