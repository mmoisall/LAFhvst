"""구형 HVST → LAFhvst 이관 전용 UI (pywebview + FastAPI).

메인 앱과 독립적으로 실행되며, 현재 lafhvst.db 에 자료를 '추가'만 한다.
    python tools/hvst_import/app.py
"""

import os
import socket
import sys
import threading

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(BASE_DIR)))

from core.database import init_db  # noqa: E402
from tools.hvst_import import importer  # noqa: E402

HOST = "127.0.0.1"
PORT = 17374


class PlanPayload(BaseModel):
    db_path: str | None = None
    settings_path: str | None = None
    include_secrets: bool = False
    exclude_columns: list[str] = []
    exclude_row_ids: list[str] = []


class MigratePayload(PlanPayload):
    dry_run: bool = False


app = FastAPI(title="HVST Import")


@app.get("/api/defaults")
def defaults() -> dict:
    return {
        "db_path": importer.DEFAULT_DB,
        "settings_path": importer.DEFAULT_SETTINGS,
    }


@app.post("/api/inspect")
def api_inspect(payload: PlanPayload) -> dict:
    try:
        return importer.inspect(payload.db_path or importer.DEFAULT_DB, payload.settings_path or importer.DEFAULT_SETTINGS)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/preview")
def api_preview(payload: PlanPayload) -> dict:
    try:
        return importer.build_plan(
            payload.db_path or importer.DEFAULT_DB,
            payload.settings_path or importer.DEFAULT_SETTINGS,
            payload.include_secrets,
            payload.exclude_columns,
            payload.exclude_row_ids,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/migrate")
def api_migrate(payload: MigratePayload) -> dict:
    try:
        init_db()
        return importer.migrate(
            payload.db_path or importer.DEFAULT_DB,
            payload.settings_path or importer.DEFAULT_SETTINGS,
            payload.include_secrets,
            dry_run=payload.dry_run,
            exclude_columns=payload.exclude_columns,
            exclude_row_ids=payload.exclude_row_ids,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="ui")


def _wait_for_server(port, host=HOST, timeout=15.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _run_server(serve_only=False):
    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    if serve_only:
        server.run()
        return
    thread = threading.Thread(target=server.run, name="hvst-import-server", daemon=True)
    thread.start()
    return thread


def main():
    serve_only = "--serve" in sys.argv or os.environ.get("LAF_NO_GUI") == "1"
    thread = _run_server(serve_only=serve_only)
    if not serve_only and not _wait_for_server(PORT):
        raise RuntimeError("이관 서버가 시작되지 않았습니다 (포트 %d)" % PORT)
    if serve_only:
        return
    import webview

    webview.create_window(
        "HVST 이관 도구", url="http://%s:%d" % (HOST, PORT), width=980, height=760, resizable=True
    )
    webview.start()


if __name__ == "__main__":
    main()
