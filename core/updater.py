"""GitHub Releases 기반 업데이트 확인/적용.

표준 라이브러리만 사용한다.
- 최신 릴리스 확인: api.github.com/repos/mmoisall/LAFhvst/releases/latest
- 패키징(frozen) 실행 시: zip 다운로드 → 임시폴더 압축해제 → 별도 배치로 파일 교체 후 재시작
- 데이터(data/, lafhvst.db, log/)는 건드리지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile

from . import utils
from .version import __version__

REPO = "mmoisall/LAFhvst"
API_LATEST = "https://api.github.com/repos/" + REPO + "/releases/latest"
ASSET_PREFIX = "LAFhvst-"
ASSET_SUFFIX = "-win64.zip"
USER_AGENT = "LAFhvst-Updater"

_progress_lock = threading.Lock()
_progress = {"phase": "idle", "done": 0, "total": 0}


# --- 버전 ---------------------------------------------------------------
def current_version() -> str:
    return str(__version__)


def parse_version(text) -> tuple:
    if not text:
        return ()
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", str(text))
    if not match:
        return ()
    return tuple(int(part) for part in match.groups())


def is_newer(latest, current) -> bool:
    latest_v = parse_version(latest)
    current_v = parse_version(current)
    if not latest_v or not current_v:
        return False
    return latest_v > current_v


def is_supported() -> bool:
    """패키징 실행에서만 자동 업데이트(파일 교체)를 지원한다."""
    return utils.is_frozen()


def current_exe_name() -> str:
    name = os.path.basename(sys.executable)
    return name if name.lower().endswith(".exe") else "LAFhvst.exe"


# --- 진행 상황 ----------------------------------------------------------
def set_progress(phase, done=0, total=0) -> None:
    with _progress_lock:
        _progress["phase"] = phase
        _progress["done"] = done
        _progress["total"] = total


def get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def _report(done, total) -> None:
    set_progress("download", done, total)


# --- 확인 ---------------------------------------------------------------
def _http_json(url, timeout=8) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def check_for_update(timeout=8) -> dict:
    current = current_version()
    result = {
        "current": current,
        "latest": current,
        "available": False,
        "supported": is_supported(),
        "exe": current_exe_name(),
        "asset_url": None,
        "asset_name": None,
        "notes": "",
        "html_url": None,
        "error": None,
    }
    try:
        data = _http_json(API_LATEST, timeout=timeout)
    except Exception as exc:  # 오프라인/레이트리밋 등
        result["error"] = str(exc)
        return result

    tag = str(data.get("tag_name") or "").strip()
    result["latest"] = tag.lstrip("v") or current
    result["html_url"] = data.get("html_url")
    result["notes"] = (data.get("body") or "")[:2000]
    for candidate in data.get("assets") or []:
        name = str(candidate.get("name") or "")
        if name.startswith(ASSET_PREFIX) and name.endswith(ASSET_SUFFIX):
            result["asset_url"] = candidate.get("browser_download_url")
            result["asset_name"] = name
            break
    result["available"] = is_newer(tag, current) and bool(result["asset_url"])
    return result


# --- 다운로드/스테이징 --------------------------------------------------
def download_release(asset_url, dest_dir, progress=None) -> str:
    utils.ensure_dir(dest_dir)
    name = asset_url.rsplit("/", 1)[-1] or "LAFhvst-update.zip"
    target = os.path.join(dest_dir, name)
    request = urllib.request.Request(asset_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(target, "wb") as handle:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
    return target


def _looks_like_app_dir(path) -> bool:
    return os.path.isfile(os.path.join(path, "LAFhvst.exe")) or os.path.isfile(
        os.path.join(path, "LAFhvst-server.exe")
    )


def stage_zip(zip_path, dest_dir) -> str:
    if os.path.isdir(dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
    utils.ensure_dir(dest_dir)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(dest_dir)
    if _looks_like_app_dir(dest_dir):
        return dest_dir
    for entry in os.listdir(dest_dir):
        child = os.path.join(dest_dir, entry)
        if os.path.isdir(child) and _looks_like_app_dir(child):
            return child
    return dest_dir


# --- 적용 ---------------------------------------------------------------
def _ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _build_updater_script(staged_dir, target_dir, exe_name, pid, extra_args) -> str:
    items = ",".join(_ps_quote(arg) for arg in (extra_args or []))
    args_array = "@(%s)" % items if items else "@()"
    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        "$src = %s" % _ps_quote(staged_dir),
        "$dst = %s" % _ps_quote(target_dir),
        "$targetPid = %d" % int(pid),
        "$exe = %s" % _ps_quote(exe_name),
        "$restartArgs = %s" % args_array,
        "try { Wait-Process -Id $targetPid -Timeout 180 } catch {}",
        "Start-Sleep -Milliseconds 800",
        "robocopy $src $dst /E /R:2 /W:1 /NFL /NDL /NJH /NJS "
        "/XF lafhvst.db lafhvst.db-wal lafhvst.db-shm /XD data log | Out-Null",
        "$argLine = ($restartArgs | ForEach-Object { '\"' + $_ + '\"' }) -join ' '",
        "$psi = New-Object System.Diagnostics.ProcessStartInfo",
        "$psi.FileName = (Join-Path $dst $exe)",
        "$psi.WorkingDirectory = $dst",
        "$psi.UseShellExecute = $true",
        "if ($argLine) { $psi.Arguments = $argLine }",
        "[System.Diagnostics.Process]::Start($psi) | Out-Null",
        "Remove-Item -LiteralPath $PSCommandPath -Force",
        "",
    ]
    return "\r\n".join(lines)


def launch_updater(staged_dir, target_dir, exe_name, pid=None, extra_args=None) -> str:
    if pid is None:
        pid = os.getpid()
    script = _build_updater_script(staged_dir, target_dir, exe_name, pid, extra_args)
    script_path = os.path.join(tempfile.gettempdir(), "lafhvst_update_%s.ps1" % pid)
    with open(script_path, "w", encoding="utf-8-sig") as handle:
        handle.write(script)

    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        script_path,
    ]
    kwargs = {
        "close_fds": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    # 부모(앱)가 종료돼도 자식이 살아남도록 별도 프로세스 그룹/숨김 콘솔로 실행.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(command, creationflags=flags, **kwargs)
    return script_path


def apply_update(update_info, restart_args=None, progress=None) -> dict:
    """다운로드·스테이징·업데이터 실행. 성공 시 호출측이 앱을 종료해야 한다."""
    if not is_supported():
        return {"ok": False, "error": "패키징 실행에서만 자동 업데이트를 지원합니다."}
    asset_url = (update_info or {}).get("asset_url")
    if not asset_url:
        return {"ok": False, "error": "다운로드할 릴리스 자산을 찾을 수 없습니다."}

    target_dir = utils.project_root()
    work = os.path.join(tempfile.gettempdir(), "lafhvst_update")
    try:
        set_progress("download", 0, 0)
        zip_path = download_release(asset_url, work, progress=progress or _report)
        set_progress("extract")
        staged = stage_zip(zip_path, os.path.join(work, "staged"))
    except Exception as exc:
        set_progress("error")
        return {"ok": False, "error": str(exc)}

    if not _looks_like_app_dir(staged):
        set_progress("error")
        return {"ok": False, "error": "업데이트 압축 형식이 올바르지 않습니다."}

    try:
        set_progress("install")
        launch_updater(
            staged,
            target_dir,
            current_exe_name(),
            pid=os.getpid(),
            extra_args=restart_args,
        )
    except Exception as exc:
        set_progress("error")
        return {"ok": False, "error": str(exc)}

    set_progress("restarting")
    return {"ok": True, "restart": True, "exe": current_exe_name()}
