"""Real APScheduler dispatch and synthetic durable automatic intents only."""
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace

from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
import pytest
from sqlalchemy import select

from src import scheduler
from src.core.database import ArchiveDatabase
from src.core.models import ApplicationState, Operation
from src.core.tasks import TaskQueue


@pytest.fixture
def scheduled(tmp_path, monkeypatch):
    database = ArchiveDatabase.create(tmp_path / 'schedule.db')
    queue = TaskQueue(database, {'check': object()})
    live = BackgroundScheduler(daemon=True)
    original_start = live.start
    monkeypatch.setattr(live, 'start', lambda: original_start(paused=True))
    monkeypatch.setattr(scheduler, 'BackgroundScheduler', lambda **kwargs: live)
    monkeypatch.setattr(scheduler, '_scheduler', None)
    with database.write() as session:
        session.add(ApplicationState(key=scheduler.INTERVAL_KEY, value={'minutes': 1}))
        session.add(ApplicationState(key='automatic_paused', value=True))
    tracker = SimpleNamespace(database=database, queue=queue)
    yield tracker, live
    scheduler.stop_scheduler()
    queue.stop()
    database.close()


@pytest.mark.parametrize('late_seconds', [2, 182])
def test_late_dispatch_coalesces_and_restart_does_not_resubmit(scheduled, late_seconds):
    tracker, live = scheduled
    scheduler.start_scheduler(tracker)
    finished = Event()
    events = []
    def observed(event):
        events.append(event)
        finished.set()
    live.add_listener(observed, EVENT_JOB_EXECUTED | EVENT_JOB_MISSED)
    live.modify_job('check_updates', next_run_time=datetime.now(timezone.utc) - timedelta(seconds=late_seconds))
    live.resume()
    assert finished.wait(2)
    live.pause()
    with tracker.database.read() as session:
        operations = list(session.scalars(select(Operation)))
        assert len(operations) == 1, 'A late daily check must still register one durable automatic batch'
        operation_id = operations[0].id
        assert operations[0].source == 'automatic' and operations[0].parameters == {}
        assert operations[0].started_at is None
        assert session.get(ApplicationState, 'automatic_paused').value is True
        due = datetime.fromisoformat(session.get(ApplicationState, scheduler.INTERVAL_KEY).value['next_check_at'])
        assert due > datetime.now(timezone.utc)
    assert len(events) == 1 and events[0].code == EVENT_JOB_EXECUTED
    scheduler.check_and_download(tracker)
    scheduler.check_and_download(tracker)
    assert tracker.queue._step('check') is False  # Deferred while automatic checks are paused.
    scheduler.stop_scheduler()
    restarted = BackgroundScheduler(daemon=True)
    original_start = restarted.start
    restarted.start = lambda: original_start(paused=True)
    scheduler.BackgroundScheduler = lambda **kwargs: restarted
    scheduler.start_scheduler(tracker)
    assert restarted.get_job('check_updates').next_run_time == due
    with tracker.database.read() as session:
        assert [row.id for row in session.scalars(select(Operation))] == [operation_id]


def test_failed_registration_rolls_back_next_due_and_preserves_retry(scheduled, monkeypatch):
    tracker, live = scheduled
    due = datetime.now(timezone.utc) - timedelta(seconds=2)
    with tracker.database.write() as session:
        session.get(ApplicationState, scheduler.INTERVAL_KEY).value = {'minutes': 1, 'next_check_at': due.isoformat()}
    scheduler.start_scheduler(tracker)
    from src import progress
    with monkeypatch.context() as patch:
        patch.setattr(progress, 'maintenance_active', lambda: True)
        live.get_job('check_updates').func(*live.get_job('check_updates').args)
    with tracker.database.read() as session:
        assert session.scalar(select(Operation)) is None
        assert session.get(ApplicationState, scheduler.INTERVAL_KEY).value['next_check_at'] == due.isoformat()
    original = tracker.queue.enqueue_in_transaction
    def fail_after_enqueue(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic registration failure')
    with monkeypatch.context() as patch:
        patch.setattr(tracker.queue, 'enqueue_in_transaction', fail_after_enqueue)
        with pytest.raises(RuntimeError, match='synthetic registration failure'):
            live.get_job('check_updates').func(*live.get_job('check_updates').args)
    with tracker.database.read() as session:
        assert session.scalar(select(Operation)) is None
        assert session.get(ApplicationState, scheduler.INTERVAL_KEY).value['next_check_at'] == due.isoformat()
    live.get_job('check_updates').func(*live.get_job('check_updates').args)
    with tracker.database.read() as session:
        assert session.scalar(select(Operation)).source == 'automatic'
        assert datetime.fromisoformat(session.get(ApplicationState, scheduler.INTERVAL_KEY).value['next_check_at']) > datetime.now(timezone.utc)
