import asyncio
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime

from . import browser_manager, models, utils
from .database import get_session

_ERROR_CODE_RE = re.compile(r"(?:code|error[_\s-]?code)[\s:=#-]*(\d{2,6})", re.IGNORECASE)
_ERROR_HINTS = (
    "error",
    "exception",
    "traceback",
    "failed",
    "forbidden",
    "unauthorized",
    "rate limit",
    "too many requests",
    "unavailable",
    "timed out",
    "timeout",
    "403",
    "404",
    "429",
    "500",
    "502",
    "503",
)


def gallery_dl_path() -> str | None:
    exe_name = "gallery-dl.exe" if os.name == "nt" else "gallery-dl"
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(sys.executable)), exe_name),
        os.path.join(utils.resource_root(), exe_name),
        os.path.join(utils.project_root(), exe_name),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("gallery-dl")


def app_config_path() -> str:
    return os.path.join(utils.project_root(), "data", "gallery-dl.conf")


_SECRET_CONFIG_KEYS = {
    "cookies", "auth_token", "refresh-token", "refresh_token",
    "api-key", "api_key", "api-secret", "api_secret",
    "client-id", "client_id", "client-secret", "client_secret",
    "password", "passwd", "username", "user", "token", "access_token",
}


def _strip_config_secrets(obj):
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if str(key).lower() in _SECRET_CONFIG_KEYS:
                continue
            out[key] = _strip_config_secrets(value)
        return out
    if isinstance(obj, list):
        return [_strip_config_secrets(item) for item in obj]
    return obj


def ensure_app_config() -> str:
    """앱 전용 gallery-dl config 를 준비한다.

    최초 1회 전역 config 를 seed 로 복사하되 **자격증명(쿠키/토큰/비밀번호)은 제거**한다.
    이후 앱이 관리하며, 인증은 프로파일 `--cookies` 로 공급한다.
    """
    target = app_config_path()
    if os.path.isfile(target):
        return target
    seed = None
    try:
        import json as _json
        appdata = os.environ.get("APPDATA")
        if appdata:
            global_cfg = os.path.join(appdata, "gallery-dl", "config.json")
            if os.path.isfile(global_cfg):
                with open(global_cfg, "r", encoding="utf-8") as handle:
                    seed = _json.load(handle)
    except Exception:
        seed = None
    if not isinstance(seed, dict):
        seed = {}
    seed = _strip_config_secrets(seed)
    seed.setdefault("downloader", {})
    utils.ensure_dir(os.path.dirname(target))
    try:
        import json as _json
        with open(target, "w", encoding="utf-8") as handle:
            _json.dump(seed, handle, ensure_ascii=False, indent=2)
    except Exception:
        return target
    return target


def build_command(
    url: str,
    directory: str | None = None,
    options: list[str] | None = None,
    date_after: str | None = None,
    date_filter: str | None = None,
    write_info_json: bool = True,
    quiet: bool = False,
    sleep: tuple | list | None = None,
    sleep_request: tuple | list | None = None,
    retries: int | None = None,
    timeout: float | None = None,
    limit_rate: str | None = None,
    use_app_config: bool = True,
) -> list[str]:
    command = [gallery_dl_path() or "gallery-dl"]
    if use_app_config:
        cfg = ensure_app_config()
        if os.path.isfile(cfg):
            command.extend(["--config", cfg])
    if write_info_json:
        command.append("--write-info-json")
        # 메타데이터(info.json 등)는 하위 '.metadata' 폴더로 격리
        command.extend([
            "-o", "metadata.base-directory=false",
            "-o", "metadata.directory=.metadata",
        ])
    if quiet:
        command.append("--quiet")
    if directory:
        command.extend(["-D", directory])
    if date_filter:
        command.extend(["--filter", "datetime(%s) <= date" % date_filter])
    elif date_after:
        command.extend(["--date-after", date_after])
    if sleep:
        command.extend(["--sleep", _range_value(sleep)])
    if sleep_request:
        command.extend(["--sleep-request", _range_value(sleep_request)])
    if retries is not None:
        command.extend(["--retries", str(int(retries))])
    if timeout is not None:
        command.extend(["--http-timeout", str(timeout)])
    if limit_rate:
        command.extend(["--limit-rate", str(limit_rate)])
    command.extend(list(options or []))
    command.append(url)
    return command


def _range_value(value) -> str:
    """(min,max) → gallery-dl 형식(단일값 또는 min-max)."""
    if isinstance(value, (list, tuple)):
        if len(value) >= 2:
            return "%s-%s" % (value[0], value[1])
        if len(value) == 1:
            return str(value[0])
        return ""
    return str(value)


async def run_gdl(
    url: str,
    directory: str,
    options: list[str] | None = None,
    date_after: str | None = None,
    timeout: int | None = 900,
) -> dict:
    """단발성 gallery-dl 수집 (반응형 알림 트리거용).

    프로파일 로테이션/한도 학습 없이 주어진 URL 하나를 지정 디렉토리로
    즉시 내려받는다. 실패해도 예외를 던지지 않고 결과 dict를 반환한다.
    """
    target = (url or "").strip()
    if not target:
        return {"ok": False, "errors": [{"code": None, "message": "empty url"}], "directory": directory}
    if directory:
        await asyncio.to_thread(utils.ensure_dir, directory)
    command = build_command(
        target,
        directory=directory or None,
        options=list(options or []),
        date_after=date_after,
    )
    try:
        process = await start_process(command)
        result = await wait_process_streaming(process, timeout=timeout)
    except FileNotFoundError:
        return {
            "ok": False,
            "errors": [{"code": None, "message": "gallery-dl 실행 파일을 찾을 수 없습니다."}],
            "directory": directory,
        }
    except Exception as exc:  # pragma: no cover - 환경 의존
        return {
            "ok": False,
            "errors": [{"code": None, "message": str(exc)}],
            "directory": directory,
        }
    parsed = parse_output(result.get("stdout", ""), result.get("stderr", ""))
    errors = parsed["errors"]
    ok = (
        result.get("returncode") == 0
        and not result.get("timeout")
        and not result.get("rate_limited")
    )
    if not ok and not errors:
        errors = [{"code": None, "message": "gallery-dl exited with a non-zero status"}]
    return {
        "ok": ok,
        "returncode": result.get("returncode"),
        "timeout": bool(result.get("timeout")),
        "rate_limited": bool(result.get("rate_limited")),
        "errors": errors,
        "directory": directory,
    }


def extract_error_code(message: str) -> str | None:
    if not message:
        return None
    match = _ERROR_CODE_RE.search(str(message))
    return match.group(1) if match else None


def parse_output(stdout: str, stderr: str) -> dict:
    records: list[dict] = []
    errors: list[dict] = []
    seen: set[str] = set()

    def add_error(code, message):
        text = str(message or "").strip()
        if not text:
            return
        key = str(code) + "::" + text[:160]
        if key in seen:
            return
        seen.add(key)
        errors.append({"code": None if code is None else str(code), "message": text})

    combined = (stdout or "") + "\n" + (stderr or "")
    for raw_line in combined.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                records.append(obj)
                error_value = obj.get("error") or obj.get("exception")
                code_value = obj.get("code") or obj.get("error_code")
                if error_value or code_value:
                    add_error(
                        code_value if code_value is not None else extract_error_code(str(error_value)),
                        error_value or json.dumps(obj, ensure_ascii=False),
                    )
                continue
        lowered = line.lower()
        if any(hint in lowered for hint in _ERROR_HINTS):
            add_error(extract_error_code(line), line)

    return {"records": records, "errors": errors}


async def start_process(command: list[str], cwd: str | None = None, env: dict | None = None):
    return await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )


async def wait_process(process, timeout: int | None = None) -> dict:
    try:
        if timeout:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        else:
            stdout, stderr = await process.communicate()
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        return {
            "returncode": None,
            "timeout": True,
            "stdout": "",
            "stderr": "gallery-dl timed out",
        }
    return {
        "returncode": process.returncode,
        "timeout": False,
        "stdout": (stdout or b"").decode("utf-8", "replace"),
        "stderr": (stderr or b"").decode("utf-8", "replace"),
    }


async def execute(command: list[str], cwd: str | None = None, timeout: int | None = None) -> dict:
    process = await start_process(command, cwd=cwd)
    return await wait_process(process, timeout=timeout)


# ---------------------------------------------------------------------------
# Rate limit (Code 88) detection
# ---------------------------------------------------------------------------

RATE_LIMIT_CODES = {"88", "420", "429"}

_RATE_LIMIT_RE = re.compile(
    r"code[\s:=#-]*8{2,}\b|rate[\s_-]?limit|too many requests|\b429\b",
    re.IGNORECASE,
)


def is_rate_limit_text(text: str) -> bool:
    if not text:
        return False
    return bool(_RATE_LIMIT_RE.search(str(text)))


def is_rate_limit_error(errors) -> bool:
    for entry in errors or []:
        if not isinstance(entry, dict):
            if is_rate_limit_text(entry):
                return True
            continue
        code = str(entry.get("code") or "").strip()
        if code in RATE_LIMIT_CODES:
            return True
        if is_rate_limit_text(entry.get("message") or ""):
            return True
    return False


_AUTH_RE = re.compile(
    r"\block(?:ed|out)\b|\bsuspend(?:ed)?\b|\bunauthorized\b|\bforbidden\b|"
    r"\blogin\b|\bsign[\s-]?in\b|\bauthenticat",
    re.IGNORECASE,
)
_SHORT_RATE_RE = re.compile(r"\b429\b")


def classify_error(errors) -> str:
    """오류 성격 분류: rate_limit(88) | short_rate(429) | auth | other."""
    kinds: set[str] = set()
    for entry in errors or []:
        if isinstance(entry, dict):
            code = str(entry.get("code") or "").strip()
            text = str(entry.get("message") or "")
        else:
            code = ""
            text = str(entry)
        if code == "429" or _SHORT_RATE_RE.search(text):
            kinds.add("short_rate")
        if code == "88" or is_rate_limit_text(text):
            kinds.add("rate_limit")
        if code in ("401", "403") or _AUTH_RE.search(text):
            kinds.add("auth")
    if "rate_limit" in kinds:
        return "rate_limit"
    if "short_rate" in kinds:
        return "short_rate"
    if "auth" in kinds:
        return "auth"
    return "other"


async def wait_process_streaming(process, timeout: int | None = None, rate_limit_probe=None) -> dict:
    """stdout/stderr를 한 줄씩 읽으며 진행 중 한도 초과(Code 88)를 조기 감지한다.

    감지 즉시 프로세스를 종료(kill)하여 남은 요청을 차단하고,
    수집된 출력으로 gallery-dl 결과를 파싱한다.
    """
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    state = {"rate_limited": False}

    async def _read(stream, sink):
        if stream is None:
            return
        while True:
            try:
                raw = await stream.readline()
            except (ValueError, RuntimeError):
                break
            if not raw:
                break
            text = raw.decode("utf-8", "replace")
            sink.append(text)
            if rate_limit_probe and not state["rate_limited"] and rate_limit_probe(text):
                state["rate_limited"] = True
                try:
                    process.kill()
                except (ProcessLookupError, RuntimeError):
                    pass

    readers = [asyncio.create_task(_read(process.stdout, stdout_lines))]
    if process.stderr is not None:
        readers.append(asyncio.create_task(_read(process.stderr, stderr_lines)))
    timed_out = False
    try:
        if timeout:
            await asyncio.wait_for(asyncio.gather(*readers), timeout)
        else:
            await asyncio.gather(*readers)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            process.kill()
        except (ProcessLookupError, RuntimeError):
            pass
    await process.wait()
    return {
        "returncode": process.returncode,
        "timeout": timed_out,
        "rate_limited": state["rate_limited"],
        "stdout": "".join(stdout_lines),
        "stderr": "".join(stderr_lines),
    }


# ---------------------------------------------------------------------------
# Failover + organic cooldown/capacity learning engine
# ---------------------------------------------------------------------------

PACING_MIN_SECONDS = 0.5
PACING_MAX_SECONDS = 30.0

# 그룹 공유 한도 직렬화용 세마포어 (프로세스 내 태스크 기준)
_group_semaphores: dict[int, asyncio.Semaphore] = {}


def _group_semaphore(group_id: int, limit: int) -> asyncio.Semaphore:
    limit = max(1, int(limit or 1))
    sem = _group_semaphores.get(group_id)
    if sem is None:
        sem = asyncio.Semaphore(limit)
        _group_semaphores[group_id] = sem
    return sem


def _now() -> datetime:
    return datetime.now()


def _db_acquire_profile(group_id):
    session = get_session()
    try:
        return models.acquire_profile_for_group(session, group_id)
    finally:
        session.close()


def _db_seconds_until(group_id):
    session = get_session()
    try:
        return models.seconds_until_next_available(session, group_id)
    finally:
        session.close()


def _db_acquire_single_profile(profile_id):
    session = get_session()
    try:
        return models.acquire_single_profile(session, profile_id)
    finally:
        session.close()


def _db_single_wait(profile_id):
    session = get_session()
    try:
        return models.single_profile_wait_seconds(session, profile_id)
    finally:
        session.close()


def _db_group(group_id):
    session = get_session()
    try:
        return models.get_group_runtime(session, group_id)
    finally:
        session.close()


def _db_add_usage(profile_id, amount):
    session = get_session()
    try:
        return models.profile_add_usage(session, profile_id, amount)
    finally:
        session.close()


def _db_apply_rate_limit(profile_id, kind, source_id):
    session = get_session()
    try:
        return models.profile_apply_rate_limit(session, profile_id, kind=kind, source_id=source_id)
    finally:
        session.close()


def _db_record_recovery(profile_id, usage_delta, run_seconds, source_id, started_at, ended_at):
    session = get_session()
    try:
        return models.profile_record_recovery(
            session,
            profile_id,
            usage_delta=usage_delta,
            run_seconds=run_seconds,
            source_id=source_id,
            started_at=started_at,
            ended_at=ended_at,
        )
    finally:
        session.close()


def _db_group_rate_limit(group_id, profile_id):
    session = get_session()
    try:
        return models.group_register_rate_limit(session, group_id, profile_id)
    finally:
        session.close()


def _db_set_status(profile_id, status):
    session = get_session()
    try:
        return models.update_profile(session, profile_id, {"status": status})
    finally:
        session.close()


def _pacing_seconds(profile, group) -> float:
    candidates = []
    if profile and profile.get("learned_rate_per_hour"):
        rate = float(profile["learned_rate_per_hour"] or 0.0)
        if rate > 0:
            candidates.append(3600.0 / rate)
    if group and group.get("pace_seconds"):
        candidates.append(float(group["pace_seconds"] or 0.0))
    if not candidates:
        return 0.0
    return max(0.0, min(PACING_MAX_SECONDS, max(PACING_MIN_SECONDS, max(candidates))))


def _sleep_request_value(pacing, sleep_request) -> tuple | None:
    """학습 pacing(초)과 전역/아이템 sleep-request min-max 를 합쳐 최종 범위를 만든다.

    우선순위: CLI(학습) > 아이템 > 전역 (여기서는 전달된 sleep_request 기준).
    - sleep_request 없고 pacing 없으면 None(옵션 미주입)
    - pacing만 있으면 (pacing, pacing)
    - sleep_request [min,max] 있고 pacing 있으면 (max(min,pacing), max(max,pacing))
    """
    sr = None
    if isinstance(sleep_request, (list, tuple)) and sleep_request:
        try:
            if len(sleep_request) >= 2:
                sr = (float(sleep_request[0]), float(sleep_request[1]))
            else:
                value = float(sleep_request[0])
                sr = (value, value)
        except (TypeError, ValueError):
            sr = None
    if pacing and pacing > 0:
        if sr is None:
            return (pacing, pacing)
        return (max(sr[0], pacing), max(sr[1], pacing))
    return sr


async def _wait_for_profile(group_id, wait_cap_seconds: int) -> dict | None:
    """그룹 내 가용 계정이 나올 때까지 (쿨다운/그룹 쿨다운 해제 시각까지) 대기 후 반환."""
    if not group_id:
        return None
    while True:
        profile = await asyncio.to_thread(_db_acquire_profile, group_id)
        if profile:
            return profile
        wait_seconds = await asyncio.to_thread(_db_seconds_until, group_id)
        if wait_seconds is None:
            return None
        sleep_for = min(max(wait_seconds, 1.0), max(1, wait_cap_seconds)) + 0.5
        await asyncio.sleep(sleep_for)


async def _wait_for_single_profile(profile_id, wait_cap_seconds: int) -> dict | None:
    """단일 프로파일이 가용해질 때까지 (쿨다운 해제 시각까지) 대기 후 반환."""
    if not profile_id:
        return None
    while True:
        profile = await asyncio.to_thread(_db_acquire_single_profile, profile_id)
        if profile:
            return profile
        wait_seconds = await asyncio.to_thread(_db_single_wait, profile_id)
        if wait_seconds is None:
            return None
        sleep_for = min(max(wait_seconds, 1.0), max(1, wait_cap_seconds)) + 0.5
        await asyncio.sleep(sleep_for)


async def collect_with_failover(
    source: dict,
    directory: str,
    options: list[str],
    date_after: str | None,
    group_id: int | None,
    on_started=None,
    log_errors=None,
    max_failovers: int = 5,
    wait_cap_seconds: int = 3600,
    profile_id: int | None = None,
    date_filter: str | None = None,
    sleep: tuple | list | None = None,
    sleep_request: tuple | list | None = None,
    retries: int | None = None,
    timeout: float | None = None,
    limit_rate: str | None = None,
) -> dict:
    """단일 소스 수집을 프로파일 로테이션과 유기적 한도 학습 루프로 감싼다.

    - 선제적 선택 + 그룹 공유 한도 직렬화.
    - 학습된 처리량 기반 선제적 페이싱(--sleep-request).
    - Code 88/429: 신뢰도 기반 EMA 한도 하향 + 관측 기반 쿨다운 + 즉시 우회.
    - 성공: 처리량/회복 시간/한도를 유기적으로 갱신.
    """
    await asyncio.to_thread(utils.ensure_dir, directory)
    before_count = await asyncio.to_thread(utils.count_directory_files, directory)
    failovers: list[dict] = []
    attempts = 0
    last_errors: list[dict] = []
    last_result: dict = {}
    group = await asyncio.to_thread(_db_group, group_id) if group_id else None

    while attempts <= max_failovers:
        attempts += 1
        held = None
        if group and group.get("shared_limit"):
            held = _group_semaphore(group_id, group.get("concurrent_limit") or 1)
            await held.acquire()
        try:
            if profile_id:
                profile = await _wait_for_single_profile(profile_id, wait_cap_seconds)
            else:
                profile = await _wait_for_profile(group_id, wait_cap_seconds)

            command_options = list(options)
            if profile:
                cookie_path = browser_manager.cookie_file_path(
                    profile.get("context_dir"), profile.get("id")
                )
                if os.path.isfile(cookie_path):
                    command_options.extend(["--cookies", cookie_path])
            pacing = _pacing_seconds(profile, group)
            has_sleep_request = any(
                str(opt).startswith("--sleep-request") for opt in command_options
            )
            sr_value = None if has_sleep_request else _sleep_request_value(pacing, sleep_request)

            command = build_command(
                source.get("url"),
                directory=directory,
                options=command_options,
                date_after=date_after,
                date_filter=date_filter,
                sleep=sleep,
                sleep_request=sr_value,
                retries=retries,
                timeout=timeout,
                limit_rate=limit_rate,
            )
            started_at = _now()
            started_monotonic = time.monotonic()
            process = await start_process(command)
            if on_started is not None:
                await on_started(source.get("id"), command)
            result = await wait_process_streaming(process, rate_limit_probe=is_rate_limit_text)
            run_seconds = max(0.0, time.monotonic() - started_monotonic)
            ended_at = _now()

            parsed = parse_output(result.get("stdout", ""), result.get("stderr", ""))
            errors = parsed["errors"]
            kind = classify_error(errors)
            rate_limited = bool(result.get("rate_limited")) or kind in ("rate_limit", "short_rate")

            after_count = await asyncio.to_thread(utils.count_directory_files, directory)
            usage_delta = max(0, after_count - before_count)
            before_count = after_count
            last_result = result
            last_errors = errors

            if result.get("returncode") == 0 and not result.get("timeout") and not rate_limited:
                if profile:
                    await asyncio.to_thread(
                        _db_record_recovery,
                        profile["id"],
                        usage_delta,
                        run_seconds,
                        source.get("id"),
                        started_at,
                        ended_at,
                    )
                return {
                    "ok": True,
                    "returncode": result.get("returncode"),
                    "errors": errors,
                    "rate_limited": False,
                    "profile": profile,
                    "is_probe": bool(profile and profile.get("is_probe")),
                    "attempts": attempts,
                    "failovers": failovers,
                    "usage_delta": usage_delta,
                }

            if rate_limited and profile and attempts <= max_failovers:
                rate_errors = errors or [
                    {"code": "88", "message": "rate limit detected (code 88)"}
                ]
                if usage_delta:
                    await asyncio.to_thread(_db_add_usage, profile["id"], usage_delta)
                await asyncio.to_thread(
                    _db_apply_rate_limit, profile["id"], kind, source.get("id")
                )
                await asyncio.to_thread(_db_group_rate_limit, group_id, profile["id"])
                if log_errors is not None:
                    await log_errors(source.get("id"), rate_errors)
                failovers.append(profile)
                continue

            if kind == "auth" and profile and attempts <= max_failovers:
                await asyncio.to_thread(_db_set_status, profile["id"], "잠금")
                if log_errors is not None:
                    await log_errors(
                        source.get("id"),
                        errors or [{"code": None, "message": "authentication failed"}],
                    )
                failovers.append(profile)
                continue

            break
        finally:
            if held is not None:
                held.release()

    if not last_errors:
        last_errors = [
            {"code": None, "message": "gallery-dl exited with a non-zero status"}
        ]
    return {
        "ok": False,
        "returncode": last_result.get("returncode"),
        "errors": last_errors,
        "rate_limited": is_rate_limit_error(last_errors),
        "profile": failovers[-1] if failovers else None,
        "attempts": attempts,
        "failovers": failovers,
        "usage_delta": 0,
    }
