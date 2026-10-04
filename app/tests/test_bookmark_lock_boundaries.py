"""Memory freshness never retains a scarce database lease or blocks browsing."""
from contextlib import contextmanager
from threading import Event, Thread, current_thread
from time import monotonic, sleep

import pytest
from uuid import uuid4

from src.core.bookmarks import BookmarkService
from src.core.database import ArchiveDatabase
from src.core.library import Library
from src.core.models import Artist, Work, Operation, OperationItem
from src.core.read_control import LocalReadBusy
from src.core.activity_log import processing_log


class Remote:
    def __init__(self):
        self.value = False

    def get_account_id(self):
        return "42"

    def write_bookmark(self, _id, desired, account):
        assert account == "42"
        self.value = desired

    def read_bookmark_bound(self, _id, account):
        assert account == "42"
        return self.value


def create(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "library.db")
    with database.write() as session:
        session.add(Work(id=1, pixiv_illust_id="10", title="work", page_count=1,
                         artist=Artist(pixiv_user_id="7", name="artist")))
    service = BookmarkService(database, Remote(), current_account=lambda: "42")
    return database, service


def test_write_wait_does_not_hold_bookmark_memory_lock(tmp_path):
    database, service = create(tmp_path)
    service.start()
    entered, returned = Event(), Event()
    errors = []
    original = service._persist_submission

    def persist(*args):
        entered.set()
        return original(*args)

    service._persist_submission = persist
    try:
        database._write_lock.acquire()
        def submit():
            try:
                service.submit(1, True, str(uuid4()))
            except Exception as exc:
                errors.append(exc)
            finally:
                returned.set()
        worker = Thread(target=submit)
        worker.start()
        assert entered.wait(2)
        snap_done = Event()
        reader = Thread(target=lambda: (service.snapshot(None, work_id=1), snap_done.set()))
        reader.start()
        assert snap_done.wait(1), "blocked writer retained bookmark memory lock"
        assert service.busy()
        assert service.stop(timeout=.02) is False, "accepted submission is still in flight"
        database._write_lock.release()
        worker.join(3)
        reader.join(1)
        assert returned.is_set() and not errors
        assert service.stop(timeout=None)
        assert not service.busy()
    finally:
        # The lock is an RLock owned only by this test thread.
        if database._write_lock._is_owned():
            database._write_lock.release()
        service.request_stop()
        service.stop()
        database.close()


def test_gallery_detail_feed_release_read_connection_before_freshness(tmp_path):
    database, service = create(tmp_path)
    library = Library(database)
    library.bookmarks = service
    original = service.snapshot
    count = 0

    def snapshot(*args, **kwargs):
        nonlocal count
        assert database.engine.pool.checkedout() == 0
        count += 1
        return original(*args, **kwargs)

    service.snapshot = snapshot
    try:
        assert library.query(compact=True)["items"][0]["id"] == 1
        assert library.query()["items"][0]["id"] == 1
        assert library.detail(1)["id"] == 1
        assert library.feed()["items"][0]["work_id"] == 1
        assert service.state(1)["work_id"] == 1
        assert count == 5
    finally:
        database.close()


def test_late_freshness_cannot_replace_newer_version_or_other_account(tmp_path):
    database, service = create(tmp_path)
    try:
        service._mark_fresh(1, 3, "42")
        service._mark_fresh(1, 2, "42")
        service._mark_fresh(1, 4, "99")
        assert service._fresh[1][:2] == (3, "42")
    finally:
        database.close()


class ReviewRemote:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.release_writes = None
    def get_account_id(self):
        return '42'
    def write_bookmark(self, identity, desired, account):
        assert account == '42'
        self.writes.append(identity)
        self.values[identity] = desired
        if self.release_writes is not None:
            assert self.release_writes.wait(3)
    def read_bookmark_bound(self, identity, account):
        assert account == '42'
        return self.values.get(identity, False)


@pytest.fixture
def review_context(tmp_path):
    database = ArchiveDatabase.create(tmp_path/'synthetic.db')
    with database.write() as session:
        artist = Artist(pixiv_user_id='7', name='synthetic')
        session.add_all([Work(id=index, pixiv_illust_id=str(index), title='synthetic', artist=artist)
                         for index in (1, 2)])
    remote = ReviewRemote()
    service = BookmarkService(database, remote, max_pending=1, max_workers=1, current_account=lambda: '42')
    service.start()
    try:
        yield database, service, remote
    finally:
        service.request_stop()
        assert service.stop(timeout=3)
        database.close()


def review_done(service, identity):
    until = monotonic()+3
    while monotonic()<until:
        state = service.operation(identity)
        if state['status'] != 'pending':
            return state
        sleep(.005)
    raise AssertionError('operation remained pending')


def test_local_read_pressure_after_a_write_retries_only_readback(review_context):
    database, service, remote = review_context
    original = service._is_suppressed
    checks = 0
    def suppressed(identity):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise LocalReadBusy('synthetic lease pressure after successful remote write')
        return original(identity)
    service._is_suppressed = suppressed
    receipt = service.submit(1, True, str(uuid4()), expected_account_id='42')
    assert review_done(service, receipt['operation_id'])['status'] == 'confirmed'
    assert remote.writes == ['1']


def test_a_completed_future_pending_its_callback_cannot_swallow_a_new_write(review_context):
    database, service, remote = review_context
    entered, release = Event(), Event()
    original = service._done
    remote.release_writes = Event()
    calls = 0
    def delayed_callback(identity, future):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(3)
        return original(identity, future)
    service._done = delayed_callback
    try:
        first = service.submit(1, True, str(uuid4()), expected_account_id='42')
        remote.release_writes.set()
        assert review_done(service, first['operation_id'])['status'] == 'confirmed'
        assert entered.wait(1)
        second = service.submit(2, True, str(uuid4()), expected_account_id='42')
        release.set()
        outcome = review_done(service, second['operation_id'])
        assert remote.writes == ['1', '2'], f"accepted second write was replaced by readback only: {outcome}"
        assert outcome['status'] == 'confirmed'
    finally:
        remote.release_writes.set()
        release.set()


def test_callback_shutdown_waits_until_recovery_callback_leaves(review_context):
    database, service, remote = review_context
    entered, release = Event(), Event()
    original = service._refill_recovery
    remote.release_writes = Event()
    def held_recovery():
        entered.set()
        assert release.wait(3)
        return original()
    service._refill_recovery = held_recovery
    try:
        receipt = service.submit(1, True, str(uuid4()), expected_account_id='42')
        remote.release_writes.set()
        assert review_done(service, receipt['operation_id'])['status'] == 'confirmed'
        assert entered.wait(1)
        assert service.busy()
        assert service.stop(timeout=.02) is False
        release.set()
        assert service.stop(timeout=2)
        assert not service.busy()
    finally:
        remote.release_writes.set()
        release.set()


def test_pending_details_do_not_mean_task_history_is_truncated(review_context):
    database, service, remote = review_context
    identity = uuid4().hex
    with database.write() as session:
        session.add(Operation(id=identity, kind='verify', dedupe_key=identity, label='synthetic', state='finished'))
        session.flush()
        session.add_all([OperationItem(operation_id=identity, item_key=str(index), position=index,
            kind='verify', phase='verifying', state='failed', lane=(None, 'check', 'local', 'download')[index%4],
            target={'private': 'SECRET'}, result={'code': 'ReadTimeout'}) for index in range(201)])
    first = processing_log(database)
    assert first['details_complete'] is False and first['next_cursor']
    assert first['truncated'] is False
    second = processing_log(database, cursor=first['next_cursor'])
    assert second['details_complete'] is True and second['next_cursor'] is None
    assert second['truncated'] is False
    lines = first['lines'] + second['lines']
    assert len([line for line in lines if line.startswith('  第 ')]) == 201
    assert 'SECRET' not in '\n'.join(lines)


def test_recovery_that_sees_a_newly_committed_intent_cannot_steal_its_write(review_context):
    database, service, remote = review_context
    recovery_reading, release_recovery = Event(), Event()
    fresh_committed, release_fresh = Event(), Event()
    original_read, original_schedule = database.read, service._schedule
    receipts, recovery_results, errors = [], [], []

    @contextmanager
    def paused_recovery_read():
        with original_read() as session:
            if current_thread().name == 'review-recovery':
                recovery_reading.set()
                assert release_recovery.wait(3)
            yield session

    def paused_fresh_schedule(identity, readback_only=False):
        if not readback_only:
            fresh_committed.set()
            assert release_fresh.wait(3)
        return original_schedule(identity, readback_only)

    database.read, service._schedule = paused_recovery_read, paused_fresh_schedule
    def recover():
        try:
            recovery_results.append(service._refill_recovery())
        except BaseException as exc:
            errors.append(exc)
    def submit():
        try:
            receipts.append(service.submit(1, True, str(uuid4()), expected_account_id='42'))
        except BaseException as exc:
            errors.append(exc)
    recovery = Thread(target=recover, name='review-recovery')
    fresh = Thread(target=submit, name='review-submission')
    try:
        recovery.start()
        assert recovery_reading.wait(1)
        fresh.start()
        assert fresh_committed.wait(1)
        release_recovery.set()
        recovery.join(1)
        assert not recovery.is_alive() and recovery_results == [0]
        assert remote.writes == [] and service.busy()
        release_fresh.set()
        fresh.join(2)
        assert not fresh.is_alive() and not errors
        assert review_done(service, receipts[0]['operation_id'])['status'] == 'confirmed'
        assert remote.writes == ['1']
    finally:
        release_recovery.set()
        release_fresh.set()
        recovery.join(3)
        if fresh.ident is not None:
            fresh.join(3)
        database.read, service._schedule = original_read, original_schedule
