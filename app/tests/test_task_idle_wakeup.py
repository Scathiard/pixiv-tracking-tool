"""Exercise actual worker waits without database-size or network fixtures."""
import threading
import time
from types import SimpleNamespace

from src.core.tasks import TaskQueue, _TaskWakeSignal


def test_transactional_submission_wakes_only_after_successful_commit(tmp_path):
    from src.core.database import ArchiveDatabase
    import pytest
    database = ArchiveDatabase.create(tmp_path / 'wake.db')
    queue = TaskQueue(database, {'check': object()})
    try:
        before = queue._wake.snapshot()
        with database.write() as session:
            queue.enqueue_in_transaction(session, 'check', {}, 'synthetic')
            assert queue._wake.snapshot() == before
        assert queue._wake.snapshot() > before
        before = queue._wake.snapshot()
        with pytest.raises(RuntimeError, match='synthetic rollback'):
            with database.write() as session:
                queue.enqueue_in_transaction(session, 'check', {'different': True}, 'synthetic')
                raise RuntimeError('synthetic rollback')
        assert queue._wake.snapshot() == before
    finally:
        queue.stop()
        database.close()


def test_idle_worker_waits_for_work_and_stop_without_fast_polling():
    queue = TaskQueue.__new__(TaskQueue)
    queue.database = SimpleNamespace(background_can_advance=lambda: True,
                                     background_pressured=lambda: False)
    queue._wake, queue._stop = _TaskWakeSignal(), threading.Event()
    queue._scan_context = threading.local()
    queue._maintenance_lock = threading.Lock()
    queue.maintenance = None
    queue._clear_byte_attempts = lambda: None
    polls, entered, resumed = [], threading.Event(), threading.Event()

    def step(lane):
        polls.append(lane)
        queue._last_scan_more = False
        (entered if len(polls) == 1 else resumed).set()
        return False

    queue._step = step
    worker = threading.Thread(target=queue._worker, args=('local',))
    queue._threads = [worker]
    worker.start()
    try:
        assert entered.wait(1)
        time.sleep(.28)
        assert len(polls) == 1
        queue._wake.set()
        assert resumed.wait(.5)
    finally:
        assert queue.stop(timeout=1)


def test_work_arriving_before_wait_is_not_lost_for_any_worker():
    wake = _TaskWakeSignal()
    before = wake.snapshot()
    wake.set()
    completed = [threading.Event(), threading.Event()]
    threads = [threading.Thread(target=lambda event=event: (
        wake.wait_since(before, 5), event.set())) for event in completed]
    for thread in threads:
        thread.start()
    try:
        assert all(event.wait(.5) for event in completed)
    finally:
        wake.set()
        for thread in threads:
            thread.join(1)


def test_new_retry_deadline_wakes_all_waiters_without_database_poll():
    wake = _TaskWakeSignal()
    generation = wake.snapshot()
    completed = [threading.Event(), threading.Event()]
    threads = [threading.Thread(target=lambda event=event: (
        wake.wait_since(generation, 5), event.set())) for event in completed]
    for thread in threads:
        thread.start()
    try:
        wake.schedule(.03)
        assert all(event.wait(.5) for event in completed)
    finally:
        wake.set()
        for thread in threads:
            thread.join(1)
