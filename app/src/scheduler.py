from apscheduler.schedulers.background import BackgroundScheduler
import threading
from datetime import datetime, timedelta, timezone
from .core.models import ApplicationState

from .config import CHECK_INTERVAL_HOURS, CHECK_INTERVAL_MINUTES

_scheduler: BackgroundScheduler = None
_scheduler_lock = threading.RLock()
INTERVAL_KEY = "automatic_check_interval"


def interval_snapshot(database):
    with database.read() as session:
        record = session.get(ApplicationState, INTERVAL_KEY)
        value = record.value if record and isinstance(record.value, dict) else {}
    minutes = value.get("minutes")
    if type(minutes) is not int or not 1 <= minutes <= 43200:
        minutes = CHECK_INTERVAL_HOURS * 60 + (CHECK_INTERVAL_MINUTES or 0)
    next_check = value.get("next_check_at")
    with _scheduler_lock:
        if _scheduler is not None and callable(getattr(_scheduler, "get_job", None)):
            job = _scheduler.get_job("check_updates")
            if job and getattr(job, "next_run_time", None):
                next_check = job.next_run_time.isoformat()
    return {"minutes": minutes, "next_check_at": next_check}


def save_interval(tracker, minutes):
    if type(minutes) is not int or not 1 <= minutes <= 43200:
        raise ValueError("检查周期须为 1 到 43200 分钟")
    next_run = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    # Persist first; a failed save must leave the live schedule unchanged.
    with tracker.database.write() as session:
        record = session.get(ApplicationState, INTERVAL_KEY)
        if record is None:
            record = ApplicationState(key=INTERVAL_KEY)
            session.add(record)
        record.value = {"minutes": minutes, "next_check_at": next_run.isoformat()}
    with _scheduler_lock:
        if _scheduler is not None:
            # reschedule_job forwards extra arguments to IntervalTrigger;
            # start_date sets its first firing, while next_run_time is a Job option.
            _scheduler.reschedule_job("check_updates", trigger="interval", minutes=minutes, start_date=next_run)
        else:
            # A zero legacy interval may have left the scheduler unstarted.
            # Saving a valid interval activates it without starting a batch now.
            start_scheduler(tracker)
    return next_run


def check_and_download(tracker):
    """定时任务：检查更新并下载新作品。"""
    from . import progress
    if progress.maintenance_active():
        return
    if hasattr(tracker, "queue"):
        tracker.queue.submit("check", {}, "定时检查更新", source="automatic")
        return
    progress.submit_task(
        "check_updates",
        tracker.check_updates,
        label="定时检查更新",
        dedupe_key="check-updates",
    )


def start_scheduler(tracker):
    global _scheduler
    with _scheduler_lock:
        if _scheduler:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                pass
            _scheduler = None
        scheduler = BackgroundScheduler(daemon=True)

        saved = interval_snapshot(tracker.database) if hasattr(tracker, "database") else {"minutes": CHECK_INTERVAL_HOURS * 60 + (CHECK_INTERVAL_MINUTES or 0)}
        if not saved["minutes"]:
            return
        interval_kwargs = {"minutes": saved["minutes"]}
        try:
            due = datetime.fromisoformat(saved.get("next_check_at") or "")
            if due.tzinfo is not None:
                interval_kwargs["next_run_time"] = max(due, datetime.now(timezone.utc))
        except ValueError:
            pass

        scheduler.add_job(
            check_and_download,
            "interval",
            args=[tracker],
            id="check_updates",
            **interval_kwargs,
        )
        scheduler.start()
        _scheduler = scheduler


def stop_scheduler():
    global _scheduler
    with _scheduler_lock:
        if _scheduler:
            try:
                _scheduler.shutdown(wait=False)
            finally:
                _scheduler = None
