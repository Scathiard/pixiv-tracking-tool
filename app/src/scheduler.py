from apscheduler.schedulers.background import BackgroundScheduler
import threading

from .config import CHECK_INTERVAL_HOURS, CHECK_INTERVAL_MINUTES

_scheduler: BackgroundScheduler = None
_scheduler_lock = threading.RLock()


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

        interval_kwargs = {}
        if CHECK_INTERVAL_HOURS:
            interval_kwargs["hours"] = CHECK_INTERVAL_HOURS
        if CHECK_INTERVAL_MINUTES:
            interval_kwargs["minutes"] = CHECK_INTERVAL_MINUTES
        if not interval_kwargs:
            return

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
