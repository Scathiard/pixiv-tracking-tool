"""Micro synthetic WAL ownership and lifecycle; no remote or real library."""
import sqlite3
import io
import threading
import time

import pytest
from sqlalchemy import event, text, select
from PIL import Image

from src.core.database import ArchiveDatabase
from src.core.runtime import Runtime
from src.core.models import Artist, Asset, Work, ArchiveIntent, PublicationJournal, OperationItem, Operation
from src.core.archive import ArchiveHandler
from src.core.media import Publisher
from src.core.revisions import ensure_legacy_revision
from src.core.tasks import ItemSpec, ItemResult, TaskQueue
from src.core.wal_maintenance import WalMaintenance
from src.core.ownership import LibraryLease, LibraryInUse


def writer_policy(database):
    with database.writer_engine.connect() as connection:
        return (connection.scalar(text("PRAGMA synchronous")),
                connection.scalar(text("PRAGMA wal_autocheckpoint")))


def test_runtime_owns_checkpoint_only_after_start(tmp_path):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    runtime.queue.maintenance = None
    try:
        assert writer_policy(runtime.database) == (2, 1000)
        runtime.start()
        assert writer_policy(runtime.database) == (2, 0)
    finally:
        assert runtime.close(timeout=5)


def test_standalone_database_preserves_default(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    try:
        assert writer_policy(database) == (2, 1000)
    finally:
        database.close()


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.fixture
def database(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "archive.db")
    yield db
    db.close()


def add_payload(db, ident, size=24000):
    with db.write() as session:
        session.add(Artist(id=ident, pixiv_user_id=str(ident), name="x" * size))


def controlled_owner(db, **kwargs):
    owner = WalMaintenance(db.path, **kwargs)
    connection = owner._connect()
    # Controlled SQL seam, exclusively used by this test's calling thread.
    with db.writer_engine.connect() as writer:
        writer.exec_driver_sql("PRAGMA wal_autocheckpoint=0")
    db._writer_autocheckpoint = 0
    db._wal_owner = owner
    return owner, connection


def test_actual_owner_thread_full_and_idle_tail(database):
    owner = WalMaintenance(database.path, trigger_frames=1000, interval=.05)
    try:
        owner.start()
        database.enable_wal_maintenance(owner)
        assert not owner._thread.daemon
        add_payload(database, 1)
        wait_for(lambda: owner.snapshot()["log_frames"] and owner.snapshot()["pending_frames"] == 0)
        state = owner.snapshot()
        assert 0 < state["log_frames"] < 1000
        assert state["running"] and state["max_seconds"] >= state["last_seconds"] >= 0
        assert writer_policy(database) == (2, 0)
    finally:
        assert owner.stop(5)


def test_writer_reconnection_obeys_enabled_policy(database):
    owner = WalMaintenance(database.path)
    try:
        owner.start()
        database.enable_wal_maintenance(owner)
        with database.writer_engine.connect() as connection:
            connection.invalidate()
        assert writer_policy(database) == (2, 0)
    finally:
        assert owner.stop(5)


def test_full_snapshot_rollback_and_reopen(database):
    owner, connection = controlled_owner(database, trigger_frames=1)
    reader = sqlite3.connect(database.path)
    try:
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        add_payload(database, 1)
        reader.execute("BEGIN")
        assert reader.execute("SELECT count(*) FROM artists").fetchone() == (1,)
        add_payload(database, 2)
        assert owner._cycle(connection)
        assert reader.execute("SELECT count(*) FROM artists").fetchone() == (1,)
        with pytest.raises(ValueError):
            with database.write() as session:
                session.execute(text("DELETE FROM artists"))
                raise ValueError("synthetic rollback")
        reader.rollback()
        assert reader.execute("SELECT count(*) FROM artists").fetchone() == (2,)
    finally:
        reader.close()
        connection.close()
    reopened = sqlite3.connect(database.path)
    try:
        assert reopened.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert reopened.execute("SELECT count(*) FROM artists").fetchone() == (2,)
    finally:
        reopened.close()


def test_checkpointed_long_reader_limited_probe_then_release_recovers(database):
    owner, connection = controlled_owner(database, high_bytes=4096 * 20, trigger_frames=1, interval=.001)
    reader = sqlite3.connect(database.path)
    try:
        add_payload(database, 1, size=150000)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM artists").fetchone()
        assert owner._cycle(connection)
        state = owner.snapshot()
        assert state["log_frames"] == state["checkpointed_frames"] > 0
        assert state["pending_frames"] == 0 and state["pressure"]
        assert owner.can_probe()
        old_log = owner.snapshot()["log_frames"]
        assert owner._cycle(connection)
        assert owner.pressured() and owner.can_probe() and owner.snapshot()["log_frames"] == old_log
        with database.write() as session:
            allowed, token = database.begin_background_step()
            assert allowed and token is not None
            session.add(Artist(id=2, pixiv_user_id="2", name="small"))
        assert not owner.can_probe()
        assert owner._cycle(connection)
        assert owner.snapshot()["pending_frames"] > 0 and owner.pressured()
        database.finish_background_step(token)
        assert owner._cycle(connection) and not owner.can_probe()
        reader.rollback()
        time.sleep(.002)
        assert owner._cycle(connection) and owner.can_probe()
        with database.write() as session:
            allowed, token = database.begin_background_step()
            assert allowed
            session.add(Artist(id=3, pixiv_user_id="3", name="small"))
        database.finish_background_step(token)
        assert owner._cycle(connection)
        assert owner.snapshot()["log_frames"] < old_log and not owner.pressured()
    finally:
        reader.close()
        connection.close()


def test_continuous_short_reads_do_not_require_file_truncation(database):
    owner, connection = controlled_owner(database, high_bytes=8192, trigger_frames=1)
    try:
        for ident in range(1, 9):
            add_payload(database, ident)
            reader = sqlite3.connect(database.path)
            try:
                reader.execute("BEGIN")
                assert reader.execute("SELECT count(*) FROM artists").fetchone() == (ident,)
                assert owner._cycle(connection)
                assert owner.pressured()
            finally:
                reader.close()
            assert owner._cycle(connection)
            assert owner.pressured()
            assert database.path.with_name(database.path.name + "-wal").stat().st_size > 0
            owner.notify_commit()
    finally:
        connection.close()


def test_large_transaction_and_foreground_writes_can_exceed_soft_watermark(database):
    owner, connection = controlled_owner(database, high_bytes=8192, trigger_frames=1)
    reader = sqlite3.connect(database.path)
    try:
        add_payload(database, 1)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM artists").fetchone()
        add_payload(database, 2, size=100000)
        assert owner._cycle(connection)
        before = owner.snapshot()["log_frames"]
        assert before > owner.snapshot()["high_frames"]
        add_payload(database, 3, size=100000)  # Accepted foreground work is not gated.
        assert owner._cycle(connection)
        assert owner.snapshot()["log_frames"] > before
        assert owner.pressured()
    finally:
        reader.close()
        connection.close()


@pytest.mark.parametrize("row", [(1, 12, 12), (0, -1, -1), (0, 2, 3)])
def test_busy_unknown_counts_never_clear_pressure(database, row):
    owner, connection = controlled_owner(database)
    class Observation:
        def execute(self, sql):
            return self
        def fetchone(self):
            return row
    try:
        assert not owner._checkpoint(Observation(), "NOOP")
        assert owner.pressured()
        assert owner.snapshot()["status"] in ("busy", "unknown")
    finally:
        connection.close()


def test_io_failure_retained_and_recoverable(database):
    owner, connection = controlled_owner(database)
    class BrokenIO:
        def execute(self, sql):
            raise sqlite3.OperationalError("synthetic disk I/O error")
    try:
        with pytest.raises(sqlite3.OperationalError):
            owner._checkpoint(BrokenIO(), "NOOP")
        assert owner.snapshot()["error_count"] == 1
        assert owner.pressured()
        assert owner._cycle(connection)
        assert not owner.pressured()
        assert owner.snapshot()["last_error"] == "OperationalError"
    finally:
        connection.close()


def test_failed_start_never_disables_default(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    def fail():
        raise sqlite3.OperationalError("synthetic startup I/O")
    monkeypatch.setattr(runtime.wal_maintenance, "_connect", fail)
    try:
        with pytest.raises(RuntimeError):
            runtime.start()
        assert writer_policy(runtime.database) == (2, 1000)
        assert not runtime.queue._threads
    finally:
        assert runtime.close(5)


def test_failed_enable_restores_default(database, monkeypatch):
    owner = WalMaintenance(database.path)
    def reject(connection, cursor, statement, parameters, context, executemany):
        if statement == "PRAGMA wal_autocheckpoint=0":
            raise ValueError("synthetic enable failure")
    try:
        owner.start()
        event.listen(database.writer_engine, "before_cursor_execute", reject)
        with pytest.raises(ValueError):
            database.enable_wal_maintenance(owner)
        event.remove(database.writer_engine, "before_cursor_execute", reject)
        assert writer_policy(database) == (2, 1000)
    finally:
        assert owner.stop(5)


def test_close_timeout_keeps_owner_and_library_lease(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    runtime.queue.maintenance = None
    entered, release = threading.Event(), threading.Event()
    original = runtime.wal_maintenance._checkpoint
    def block(connection, mode):
        if mode == "PASSIVE" and runtime.wal_maintenance._stop.is_set():
            entered.set()
            assert release.wait(5)
        return original(connection, mode)
    monkeypatch.setattr(runtime.wal_maintenance, "_checkpoint", block)
    try:
        runtime.start()
        assert runtime.queue.stop(5)
        assert runtime.bookmarks.stop(5)
        assert not runtime.close(.05)
        assert entered.wait(2)
        assert runtime.wal_maintenance.alive() and not runtime.database._closed
        with pytest.raises(LibraryInUse):
            LibraryLease(tmp_path / "images").acquire()
        release.set()
        assert runtime.close(5)
        assert runtime.close(0)
        lease = LibraryLease(tmp_path / "images").acquire()
        lease.close()
    finally:
        release.set()
        runtime.close(5)


def test_restart_new_runtime_recovers(tmp_path):
    first = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    first.queue.maintenance = None
    try:
        first.start()
        add_payload(first.database, 1)
    finally:
        assert first.close(5)
    second = Runtime(tmp_path / "archive.db", tmp_path / "images")
    second.queue.maintenance = None
    try:
        assert writer_policy(second.database) == (2, 1000)
        second.start()
        assert writer_policy(second.database) == (2, 0)
        with second.database.read() as session:
            assert session.scalar(text("SELECT count(*) FROM artists")) == 1
    finally:
        assert second.close(5)


def test_inflight_actual_file_publication_settles_under_pressure(database, tmp_path, monkeypatch):
    pressured = threading.Event()
    monkeypatch.setattr(database, "background_pressured", pressured.is_set)
    queue = TaskQueue(database, {})
    publisher = Publisher(database, tmp_path / "images")
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(image, "PNG")
    payload = image.getvalue()
    class Downloader:
        def download(self, url, destination, report):
            pressured.set()  # Pressure appears after this item was claimed.
            report(len(payload), len(payload))
            destination.write_bytes(payload)
    queue.handlers["repair"] = ArchiveHandler("repair", database, publisher, object(), Downloader())
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        session.flush()
        work = Work(id=1, pixiv_illust_id="101", artist_id=1, title="synthetic", type="illust", page_count=1)
        session.add_all([work, ArchiveIntent(pixiv_illust_id="101", mode="keep", revision=1)])
        session.flush()
        asset = Asset(work_id=1, role="image", page_index=0, state="missing", source_url="https://example.invalid/101.png")
        session.add(asset)
        session.flush()
        revision = ensure_legacy_revision(session, work)
        ident = queue.enqueue_in_transaction(session, "repair", {}, "pressure publication", specs=[
            ItemSpec("file", "download", {"work_id": 1, "asset_id": asset.id, "revision": 1,
                "work_revision_id": revision.id, "url": asset.source_url, "path": "synthetic/101.png"},
                "downloading", "assets")])["operation_id"]
        asset_id = asset.id
    try:
        assert queue._step("download")
        assert pressured.is_set() and not queue._stop.is_set() and queue._accepting
        assert (publisher.root / "synthetic/101.png").read_bytes() == payload
        with database.read() as session:
            assert session.get(Asset, asset_id).state == "verified"
            assert session.scalar(select(PublicationJournal.step)) == "committed"
            assert session.scalar(select(OperationItem.state).where(OperationItem.operation_id == ident)) == "succeeded"
        # New accepted user intent can save while background claims yield.
        submitted = queue.submit("repair", {"work_ids": [1]}, "accepted under pressure")
        assert submitted["created"]
        assert not queue._step("download")
        assert not queue._step("check")
    finally:
        queue.stop(5)


def test_owner_waits_outside_business_lock_and_status_never_sql(database, monkeypatch):
    owner = WalMaintenance(database.path, interval=.02, trigger_frames=1)
    entered, release = threading.Event(), threading.Event()
    original = owner._checkpoint
    def delayed(connection, mode):
        if mode == "PASSIVE" and not owner._stop.is_set():
            entered.set()
            assert release.wait(5)
        return original(connection, mode)
    monkeypatch.setattr(owner, "_checkpoint", delayed)
    try:
        owner.start()
        database.enable_wal_maintenance(owner)
        add_payload(database, 1)
        assert entered.wait(3)
        assert database._write_lock.acquire(blocking=False)
        database._write_lock.release()
        add_payload(database, 2)  # Can commit while the owner is paused.
        state = owner.snapshot()
        assert state["running"] and state["attempts"] >= 1
    finally:
        release.set()
        assert owner.stop(5)


def test_minimum_sqlite_and_unknown_mode_do_not_silently_checkpoint(database, monkeypatch):
    owner = WalMaintenance(database.path)
    real_connect = sqlite3.connect
    calls = []
    class OldSQLite:
        def execute(self, sql):
            calls.append(sql)
            return self
        def fetchone(self):
            return ("3.51.2",)
        def close(self):
            calls.append("close")
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: OldSQLite())
    with pytest.raises(RuntimeError, match="3.51.3"):
        owner._connect()
    assert calls == ["SELECT sqlite_version()", "close"]
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    connection = owner._connect()
    try:
        with pytest.raises(ValueError):
            owner._checkpoint(connection, "unknown_typo")
    finally:
        connection.close()


def test_busy_and_io_retries_back_off(database, monkeypatch):
    owner = WalMaintenance(database.path, interval=.02)
    attempts = []
    def fail(connection):
        attempts.append(time.monotonic())
        raise sqlite3.OperationalError("synthetic I/O")
    monkeypatch.setattr(owner, "_cycle", fail)
    try:
        owner.start()
        wait_for(lambda: len(attempts) >= 4)
        gaps = [right - left for left, right in zip(attempts, attempts[1:])]
        assert gaps[0] >= .035 and gaps[1] >= .075 and gaps[2] >= .15
    finally:
        assert owner.stop(5)


def test_empty_wal_can_idle_and_failed_owner_gates_new_steps(database):
    owner = WalMaintenance(database.path, interval=.02)
    try:
        owner.start()
        database.enable_wal_maintenance(owner)
        wait_for(lambda: owner.snapshot()["attempts"] >= 3)
        assert owner.snapshot()["pending_frames"] == 0
        assert not database.background_pressured()
        assert owner.stop(5)
        assert database.background_pressured()  # Never feed a dead owner.
    finally:
        owner.stop(5)


def test_start_timeout_late_connection_keeps_lease_until_closed(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    owner = runtime.wal_maintenance
    entered, release = threading.Event(), threading.Event()
    connect, start = owner._connect, owner.start
    def late_connect():
        entered.set()
        assert release.wait(5)
        return connect()
    monkeypatch.setattr(owner, "_connect", late_connect)
    monkeypatch.setattr(owner, "start", lambda: start(timeout=.03))
    try:
        with pytest.raises(TimeoutError):
            runtime.start()
        assert entered.is_set()
        assert writer_policy(runtime.database) == (2, 1000)
        assert not runtime.close(.03)
        assert owner.alive() and not runtime.database._closed
        with pytest.raises(LibraryInUse):
            LibraryLease(tmp_path / "images").acquire()
        release.set()
        assert runtime.close(5)
        assert not owner.alive()
    finally:
        release.set()
        runtime.close(5)


def test_pressure_yields_maintenance_without_stopping_admission(database, monkeypatch):
    pressure, ticked = threading.Event(), threading.Event()
    pressure.set()
    monkeypatch.setattr(database, "background_pressured", pressure.is_set)
    queue = TaskQueue(database, {})
    class Maintenance:
        def tick(self):
            ticked.set()
    queue.maintenance = Maintenance()
    try:
        queue.start()
        assert not ticked.wait(.15)
        assert queue._accepting and not queue._stop.is_set()
        add_payload(database, 1)
        pressure.clear()
        assert ticked.wait(2)
    finally:
        assert queue.stop(5)


def test_restart_must_not_backfill_new_foreground_commit(database, monkeypatch):
    owner, connection = controlled_owner(database, high_bytes=8192, trigger_frames=1)
    add_payload(database, 1)
    original = owner._checkpoint
    pending_at_restart, errors = [], []
    def foreground():
        try:
            add_payload(database, 2)
        except BaseException as exc:
            errors.append(exc)
    def gap_commit(connection, mode):
        if mode == "RESTART":
            _, log, checkpointed = connection.execute("PRAGMA main.wal_checkpoint(NOOP)").fetchone()
            pending_at_restart.append(log - checkpointed)
        result = original(connection, mode)
        if mode == "PASSIVE":
            writer = threading.Thread(target=foreground)
            writer.start()
            writer.join(3)
            assert not writer.is_alive() and not errors
        return result
    monkeypatch.setattr(owner, "_checkpoint", gap_commit)
    try:
        owner._cycle(connection)
        assert not pending_at_restart or max(pending_at_restart) == 0, pending_at_restart
        assert not owner.can_probe() and owner.pressured()
    finally:
        connection.close()


def test_one_probe_real_multiworker_empty_lane_does_not_consume(database):
    owner, connection = controlled_owner(database, high_bytes=4096, trigger_frames=1, interval=.001)
    queue = TaskQueue(database, {})
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []
    class Handler:
        def execute(self, item, context):
            entered.set()
            assert release.wait(5)
            return ItemResult()
    queue.handlers["repair"] = Handler()
    with database.write() as session:
        ident = queue.enqueue_in_transaction(session, "repair", {}, "two workers", specs=[
            ItemSpec("one", "download", {}, "downloading", "assets"),
            ItemSpec("two", "download", {}, "downloading", "assets")])["operation_id"]
    queue._claim_round()
    assert owner._cycle(connection) and owner.can_probe() and owner.pressured()
    assert not queue._step("check")
    assert owner.can_probe() and owner.snapshot()["probe_count"] == 0
    def step():
        try:
            results.append(queue._step("download"))
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=step) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        assert entered.wait(3)
        assert owner.snapshot()["probe_inflight"]
        assert owner._cycle(connection)
        assert not owner.can_probe()
        assert owner.snapshot()["probe_count"] == 1
        release.set()
        for thread in threads:
            thread.join(5)
        assert not any(thread.is_alive() for thread in threads)
        assert not errors and sorted(results) == [False, True]
        assert queue.details(ident)["counts"] == {"pending": 1, "succeeded": 1}
        assert not owner.snapshot()["probe_inflight"]
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(5)
        queue.stop(5)
        connection.close()


def test_commit_revokes_probe_inside_writer_lock_and_rollback_preserves(database, monkeypatch):
    owner, connection = controlled_owner(database, high_bytes=4096, trigger_frames=1)
    try:
        add_payload(database, 1)
        assert owner._cycle(connection) and owner.can_probe()
        generation = owner._commit_generation
        def reject(connection):
            raise ValueError("synthetic failed commit")
        event.listen(database.writer_engine, "commit", reject)
        try:
            with pytest.raises(ValueError):
                add_payload(database, 2)
        finally:
            event.remove(database.writer_engine, "commit", reject)
        assert owner.can_probe() and owner._commit_generation == generation
        original = owner.notify_commit
        observed = []
        def notified():
            observed.append(database._write_lock._is_owned())
            original()
        monkeypatch.setattr(owner, "notify_commit", notified)
        add_payload(database, 2)
        assert observed == [True]
        assert owner._commit_generation == generation + 1 and not owner.can_probe()
    finally:
        connection.close()


def test_round_allocation_is_one_probe_then_plan_is_another(database):
    owner, connection = controlled_owner(database, high_bytes=4096, trigger_frames=1, interval=.001)
    calls = []
    class Handler:
        def plan(self, parameters, context):
            calls.append("plan")
            return [ItemSpec("download", "download", {}, "downloading", "assets")]
    queue = TaskQueue(database, {"repair": Handler()})
    ident = queue.submit("repair", {}, "round probe")["operation_id"]
    try:
        assert owner._cycle(connection) and owner.can_probe()
        assert queue._step("check")
        assert owner.snapshot()["probe_count"] == 1 and not calls
        with database.read() as session:
            operation = session.get(Operation, ident)
            assert operation.round_id is not None and operation.state == "queued"
        assert not queue._step("check")
        time.sleep(.002)
        assert owner._cycle(connection) and owner.can_probe()
        assert queue._step("check")
        assert owner.snapshot()["probe_count"] == 2 and calls == ["plan"]
    finally:
        queue.stop(5)
        connection.close()


def test_close_budget_includes_wait_for_start_recovery_lock(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    entered, release = threading.Event(), threading.Event()
    errors = []
    def recover():
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(runtime.publisher, "recover", recover)
    def start():
        try:
            runtime.start()
        except RuntimeError as exc:
            errors.append(exc)
    thread = threading.Thread(target=start)
    try:
        thread.start()
        assert entered.wait(3)
        started = time.monotonic()
        assert not runtime.close(.05)
        assert time.monotonic() - started < .3
        assert runtime.wal_maintenance.alive() and not runtime.database._closed
        with pytest.raises(LibraryInUse):
            LibraryLease(tmp_path / "images").acquire()
        release.set()
        thread.join(5)
        assert not thread.is_alive() and errors
        assert not runtime.queue._threads
        assert runtime.close(5)
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(5)
        runtime.close(5)


def test_noop_success_does_not_repeat_false_recovery_log(database, caplog):
    owner, connection = controlled_owner(database, trigger_frames=1)
    class PassiveFailure:
        def execute(self, sql):
            if sql.endswith("(PASSIVE)"):
                raise sqlite3.OperationalError("synthetic sensitive details must not be logged")
            return connection.execute(sql)
    try:
        add_payload(database, 1)
        with caplog.at_level("INFO", logger="src.core.wal_maintenance"):
            for _ in range(3):
                with pytest.raises(sqlite3.OperationalError):
                    owner._cycle(PassiveFailure())
            messages = [record.getMessage() for record in caplog.records]
            assert len(messages) == 1 and "OperationalError" in messages[0]
            assert "sensitive" not in messages[0]
            assert owner._cycle(connection)
            assert len(caplog.records) == 2 and "已恢复" in caplog.records[-1].getMessage()
    finally:
        connection.close()
