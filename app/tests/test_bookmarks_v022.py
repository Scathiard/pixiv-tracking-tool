import sqlite3
import time
from threading import Event, Thread
from uuid import uuid4

import pytest

from src.core.bookmarks import BookmarkService
from src.core.database import ArchiveDatabase
from src.core.models import ArchiveIntent, Artist, BookmarkOperation, RemoteBookmark, Work
from src.client import PixivRequestError


class Remote:
    def __init__(self):
        self.account = "123"
        self.value = False
        self.writes = 0
        self.reads = 0
        self.unknown = False
        self.block_write = None

    def get_account_id(self):
        return self.account

    def write_bookmark(self, _pixiv_id, desired, account):
        assert account == self.account
        self.writes += 1
        if self.block_write:
            self.block_write.wait(2)
        self.value = desired

    def read_bookmark_bound(self, _pixiv_id, account):
        assert account == self.account
        self.reads += 1
        return None if self.unknown else self.value


@pytest.fixture
def database(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "isolated.db")
    with db.write() as session:
        artist = Artist(id=1, pixiv_user_id="10", name="Artist")
        session.add(Work(id=2, pixiv_illust_id="20", artist=artist, title="Work"))
    yield db
    db.close()


def wait_result(service, operation_id):
    for _ in range(200):
        result = service.operation(operation_id)
        if result["status"] != "pending":
            return result
        time.sleep(.01)
    pytest.fail("bookmark operation did not finish")


def test_write_confirmed_only_after_readback_and_duplicate_suppressed(database):
    remote = Remote()
    gate = Event()
    remote.block_write = gate
    service = BookmarkService(database, remote)
    service.start()
    try:
        request_id = str(uuid4())
        first = service.submit(2, True, request_id)
        assert first["value"] is None and first["status"] == "pending"
        assert service.busy()  # A queued or active network operation retains the lease.
        assert service.submit(2, True, request_id)["operation_id"] == first["operation_id"]
        assert service.submit(2, True, str(uuid4()))["operation_id"] == first["operation_id"]
        assert service.state(2)["value"] is None
        gate.set()
        result = wait_result(service, first["operation_id"])
        assert result["status"] == "confirmed" and result["value"] is True
        assert result["confirmed_at"].endswith("Z")
        assert remote.writes == 1 and remote.reads == 1
    finally:
        gate.set()
        assert service.stop()


def test_unknown_readback_preserves_last_fact_and_never_retries_write(database):
    remote = Remote()
    with database.write() as session:
        session.add(RemoteBookmark(work_id=2, value=False, source="readback",
                                   account_id="123", version=1))
    remote.unknown = True
    service = BookmarkService(database, remote)
    service.start()
    try:
        result = service.submit(2, True, str(uuid4()))
        final = wait_result(service, result["operation_id"])
        assert final["status"] == "unconfirmed"
        assert final["value"] is False and final["cached"] is True
        assert remote.writes == 1 and remote.reads == 3
    finally:
        assert service.stop()


def test_old_metadata_response_cannot_override_manual_operation(database):
    remote = Remote()
    gate = Event()
    remote.block_write = gate
    service = BookmarkService(database, remote)
    service.start()
    try:
        old_version = service.begin_observation(2)
        receipt = service.submit(2, True, str(uuid4()))
        assert service.begin_observation(2) is None
        assert not service.apply_observation(2, False, "123", old_version)
        gate.set()
        assert wait_result(service, receipt["operation_id"])["value"] is True
        assert not service.apply_observation(2, False, "123", old_version)
    finally:
        gate.set()
        assert service.stop()


def test_restart_reconciles_without_replaying_write(database):
    with database.write() as session:
        session.add(RemoteBookmark(work_id=2, value=None, source="legacy_cache", version=4))
        session.add(BookmarkOperation(id="pending", request_id=str(uuid4()), work_id=2,
                                      desired=True, kind="write", account_id="123",
                                      status="pending", phase="writing", version=4))
    remote = Remote()
    remote.value = True
    service = BookmarkService(database, remote)
    service.start()
    try:
        assert wait_result(service, "pending")["status"] == "confirmed"
        assert remote.writes == 0 and remote.reads == 1
    finally:
        assert service.stop()


def test_offline_start_keeps_pending_until_connection_ready_then_only_reads(database):
    with database.write() as session:
        session.add(RemoteBookmark(work_id=2, value=None, source="legacy_cache", version=5))
        session.add(BookmarkOperation(id="offline-pending", request_id=str(uuid4()),
                                      work_id=2, desired=True, kind="write",
                                      account_id="123", status="pending",
                                      phase="writing", version=5))
    remote = Remote()
    remote.value = True
    available = False
    service = BookmarkService(database, remote, is_available=lambda: available)
    service.start()
    try:
        assert service.reconcile_pending() == 0
        assert service.operation("offline-pending")["status"] == "pending"
        assert remote.writes == 0 and remote.reads == 0
        available = True
        assert service.reconcile_pending() == 1
        result = wait_result(service, "offline-pending")
        assert result["status"] == "confirmed" and result["value"] is True
        assert remote.writes == 0 and remote.reads == 1
    finally:
        assert service.stop()


def test_dormant_pending_is_not_busy_and_survives_sqlite_snapshot(database, tmp_path):
    with database.write() as session:
        session.add(RemoteBookmark(work_id=2, value=None, source="legacy_cache", version=6))
        session.add(BookmarkOperation(id="move-pending", request_id=str(uuid4()),
                                      work_id=2, desired=True, kind="write",
                                      account_id="123", status="pending",
                                      phase="writing", version=6))
    original_remote = Remote()
    source = BookmarkService(database, original_remote, is_available=lambda: False)
    source.start()
    candidate_path = tmp_path / "snapshot.db"
    try:
        assert not source.busy()
        with sqlite3.connect(database.path) as original, sqlite3.connect(candidate_path) as snapshot:
            original.backup(snapshot)
    finally:
        assert source.stop()
    copied = ArchiveDatabase(candidate_path)
    remote = Remote()
    remote.value = True
    target = BookmarkService(copied, remote)
    target.start()
    try:
        assert wait_result(target, "move-pending")["status"] == "confirmed"
        assert original_remote.writes == 0 and remote.writes == 0
        assert remote.reads == 1
    finally:
        assert target.stop()
        copied.close()


def test_suppressed_work_pending_ends_without_remote_read_or_write(database):
    with database.write() as session:
        session.add(ArchiveIntent(pixiv_illust_id="20", mode="suppressed"))
        session.add(RemoteBookmark(work_id=2, value=None, source="legacy_cache", version=7))
        session.add(BookmarkOperation(id="deleted-pending", request_id=str(uuid4()),
                                      work_id=2, desired=True, kind="write",
                                      account_id="123", status="pending",
                                      phase="writing", version=7))
    remote = Remote()
    service = BookmarkService(database, remote)
    service.start()
    try:
        assert wait_result(service, "deleted-pending")["status"] == "failed"
        assert remote.reads == 0 and remote.writes == 0
        with pytest.raises(LookupError):
            service.submit(2, True, str(uuid4()))
    finally:
        assert service.stop()


def test_fresh_same_account_readback_controls_cached_without_network_on_get(database):
    remote = Remote()
    available = [True]
    now = [100.0]
    service = BookmarkService(database, remote,
        is_available=lambda: available[0], current_account=lambda: remote.account,
        clock=lambda: now[0])
    service.start()
    try:
        receipt = service.submit(2, True, str(uuid4()))
        confirmed = wait_result(service, receipt["operation_id"])
        assert confirmed["status"] == "confirmed"
        assert confirmed["value"] is True and confirmed["cached"] is False
        assert service.state(2)["cached"] is False
        reads = remote.reads
        assert service.operation(receipt["operation_id"])["cached"] is False
        assert remote.reads == reads  # GET remains local-only.
        available[0] = False
        assert service.state(2)["cached"] is True
        available[0] = True
        assert service.state(2)["cached"] is True  # Offline invalidated freshness.
        refreshed = service.refresh(2)
        assert wait_result(service, refreshed["operation_id"])["cached"] is False
        remote.account = "999"
        assert service.state(2)["cached"] is True
        remote.account = "123"
        now[0] += 46
        assert service.state(2)["cached"] is True
    finally:
        assert service.stop()
    restarted = BookmarkService(database, remote,
        is_available=lambda: True, current_account=lambda: remote.account)
    restarted.start()
    try:
        assert restarted.state(2)["cached"] is True
    finally:
        assert restarted.stop()


def test_metadata_observation_gets_same_short_freshness(database):
    remote = Remote()
    now = [50.0]
    service = BookmarkService(database, remote,
        current_account=lambda: remote.account, clock=lambda: now[0])
    service.start()
    try:
        version = service.begin_observation(2)
        assert service.apply_observation(2, True, "123", version)
        state = service.state(2)
        assert state["value"] is True and state["cached"] is False
        now[0] += 45
        assert service.state(2)["cached"] is True
        assert remote.reads == 0 and remote.writes == 0
    finally:
        assert service.stop()


def test_confirmed_and_fresh_are_atomic_to_polling_reader(database):
    remote = Remote()
    service = BookmarkService(database, remote,
        current_account=lambda: remote.account)
    entered, release, reader_started, reader_done = Event(), Event(), Event(), Event()
    original_mark = service._mark_fresh

    def delayed_mark(*args):
        entered.set()
        assert release.wait(2)
        original_mark(*args)

    service._mark_fresh = delayed_mark
    service.start()
    reader_result = {}
    try:
        receipt = service.submit(2, True, str(uuid4()))
        assert entered.wait(2)  # The confirmed DB transaction has committed.

        def read_state():
            reader_started.set()
            reader_result.update(service.state(2))
            reader_done.set()

        reader = Thread(target=read_state)
        reader.start()
        assert reader_started.wait(1)
        assert not reader_done.wait(.05)  # Cannot observe confirmed before freshness.
        release.set()
        reader.join(2)
        assert reader_done.is_set()
        assert reader_result["status"] == "confirmed"
        assert reader_result["cached"] is False
        assert wait_result(service, receipt["operation_id"])["cached"] is False
    finally:
        release.set()
        assert service.stop()


def test_old_failed_operation_does_not_taint_newer_metadata_fact(database):
    remote = Remote()
    with database.write() as session:
        session.add(RemoteBookmark(work_id=2, value=True, source="metadata",
                                   account_id="123", version=2))
        session.add(BookmarkOperation(id="older-failure", request_id=str(uuid4()),
                                      work_id=2, desired=False, kind="write",
                                      account_id="123", status="failed", phase="finished",
                                      version=1))
    service = BookmarkService(database, remote,
        current_account=lambda: remote.account)
    service.start()
    try:
        service._mark_fresh(2, 2, "123")
        state = service.state(2)
        assert state["status"] == "confirmed" and state["cached"] is False
        assert service.operation("older-failure")["cached"] is False
    finally:
        assert service.stop()


def test_missing_account_fails_closed(database):
    remote = Remote()
    remote.account = None
    service = BookmarkService(database, remote)
    service.start()
    try:
        receipt = service.submit(2, True, str(uuid4()))
        assert wait_result(service, receipt["operation_id"])["status"] == "unconfirmed"
        assert remote.writes == 0
    finally:
        assert service.stop()


def test_account_change_after_write_cannot_confirm_other_account(database):
    remote = Remote()

    def switch(_pixiv_id, _desired, _account):
        remote.writes += 1
        remote.account = "999"

    remote.write_bookmark = switch
    service = BookmarkService(database, remote)
    service.start()
    try:
        receipt = service.submit(2, True, str(uuid4()))
        final = wait_result(service, receipt["operation_id"])
        assert final["status"] == "unconfirmed" and final["value"] is None
        assert remote.writes == 1 and remote.reads == 0
    finally:
        assert service.stop()


def test_explicit_rejection_with_opposite_readback_is_failed_fact(database):
    remote = Remote()

    def reject(_pixiv_id, _desired, _account):
        remote.writes += 1
        raise PixivRequestError(403, "refused")

    remote.write_bookmark = reject
    service = BookmarkService(database, remote)
    service.start()
    try:
        receipt = service.submit(2, True, str(uuid4()))
        final = wait_result(service, receipt["operation_id"])
        assert final["status"] == "failed" and final["value"] is False
        assert final["confirmed_at"] is not None and remote.writes == 1
    finally:
        assert service.stop()
