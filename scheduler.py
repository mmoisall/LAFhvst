"""KDE 기반 적응형 백그라운드 스케줄러.

FastAPI lifespan에서 asyncio Task로 구동되며 메인 스레드를 블로킹하지 않는다.

- cyc_option = 0 : 수동 전용(무시)
- cyc_option = 1 : 고정 간격 (cyc 시간)
- cyc_option = 2 : KDE 업로드 확률 기반 동적 간격
"""

import asyncio
from datetime import datetime

from sqlalchemy import select

from core import kde, models
from core.database import get_session

TICK_SECONDS = 30

_task: asyncio.Task | None = None


def source_interval_minutes(source, now=None) -> float | None:
    """소스의 다음 대기 시간(분). 스케줄 대상이 아니면 None."""
    option = source.cyc_option or 0
    if option == 1:
        return max(1.0, float(source.cyc or 1) * 60.0)
    if option == 2:
        prediction = source_prediction(source, now)
        return float(prediction["next_wait"])
    return None


def source_prediction(source, now=None) -> dict:
    moment = now or datetime.now()
    entries = kde.parse_history(source.upload_time_history)
    return kde.predict(entries, source.learned_upload_cycle, moment)


def gather_due_ids() -> list[int]:
    """현재 실행해야 할 소스 ID 목록을 계산한다(동기, to_thread로 호출)."""
    session = get_session()
    try:
        rows = (
            session.execute(
                select(models.Source).where(models.Source.cyc_option.in_((1, 2)))
            )
            .scalars()
            .all()
        )
        now = datetime.now()
        due: list[int] = []
        for source in rows:
            interval = source_interval_minutes(source, now)
            if interval is None:
                continue
            last = (
                models._naive(source.last_attempt_at)
                or models._naive(source.recent_run_at)
                or models._naive(source.created_at)
            )
            if last is None or (now - last).total_seconds() >= interval * 60.0:
                due.append(source.id)
        return due
    finally:
        session.close()


async def _loop(trigger, get_settings, alt_trigger=None) -> None:
    while True:
        try:
            settings = get_settings() or {}
            if settings.get("schedulerEnabled", True):
                due_ids = await asyncio.to_thread(gather_due_ids)
                if due_ids:
                    trigger(due_ids)
                if alt_trigger is not None:
                    await alt_trigger()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(TICK_SECONDS)


def start_scheduler(trigger, get_settings, alt_trigger=None) -> asyncio.Task:
    global _task
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(
        _loop(trigger, get_settings, alt_trigger), name="lafhvst-scheduler"
    )
    return _task


async def stop_scheduler() -> None:
    global _task
    task = _task
    _task = None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def is_running() -> bool:
    return _task is not None and not _task.done()
