"""Server-owned preparation, narrow updates and shutdown; synthetic data only."""
from dataclasses import replace
from threading import Event, Thread
import time

import pytest
from sqlalchemy import event

from src.core.library import LibraryFilter
from src.core.models import Artist, LocalPreference, Work, WorkRevision
from src.core.read_control import LocalReadBusy, ReadCancelled, current_read
from src.core.reading import ReadingSessions
from test_reading_manifest_projection import REFERENCE, seed


def wait_until(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "preparer did not reach expected state"
        time.sleep(.005)


def rows(visit):
    return [vars(member) for member in visit.members]


@pytest.fixture
def catalogue(tmp_path):
    database, library = seed(tmp_path / "synthetic.db", count=25)
    sessions = ReadingSessions(library)
    try:
        yield database, library, sessions
    finally:
        assert sessions.stop(10)
        database.close()


def test_start_without_browser_reads_once_and_memory_scopes_match(catalogue, monkeypatch):
    database, library, sessions = catalogue
    calls, statements, ready = [], [], Event()
    basis = library.reading_basis
    def observed(ids=None):
        assert current_read.get().lane == "background"
        calls.append(ids)
        result = basis(ids)
        ready.set()
        return result
    monkeypatch.setattr(library, "reading_basis", observed)
    def sql(_a, _b, statement, *_):
        statements.append(statement)
    event.listen(database.engine, "before_cursor_execute", sql)
    sessions.start()
    assert ready.wait(10)  # No prepare call, browser or HTTP request.
    wait_until(lambda: sessions._preparation._stock is not None)
    event.remove(database.engine, "before_cursor_execute", sql)
    assert calls == [None]
    assert len([query for query in statements if "FROM read_work_order" in query]) == 1
    assert len([query for query in statements if "ix_works_settings_shape" in query]) == 1
    first, second = (sessions.prepare(LibraryFilter()).result(10) for _ in range(2))
    assert first.seed != second.seed
    assert first.orders is not second.orders and first.excluded is not second.excluded
    first.excluded.add(1)
    assert not second.excluded
    for sort in ("newest", "oldest", "added", "title"):
        for scope in ({}, {"artist_ids": (1,)}, {"types": ("manga",)}, {"favorites": True},
                      {"artist_ids": ()}, {"types": ("illust", "manga"), "favorites": True},
                      {"artist_ids_exclude": (2,)}):
            filters = LibraryFilter(sort=sort, **scope)
            expected = library.reading_manifest(filters, ceiling=20)
            actual = sessions.prepare(filters, ceiling=20).result(10)
            assert rows(actual) == expected
    assert calls == [None]


def test_delta_updates_new_visits_and_preserves_pinned_sessions(catalogue, monkeypatch):
    database, library, sessions = catalogue
    calls = []
    basis = library.reading_basis
    def observed(ids=None):
        calls.append(ids)
        return basis(ids)
    monkeypatch.setattr(library, "reading_basis", observed)
    old = sessions.prepare(LibraryFilter()).result(10)
    token = sessions.install(old)["session_id"]
    before = rows(old)
    with database.write() as session:
        session.add(WorkRevision(id="new-revision", work_id=1, ordinal=2, title="Changed",
            type="manga", page_count=8, source_signature="new", state="complete"))
        session.flush()
        work = session.get(Work, 1)
        work.page_count, work.type, work.default_revision_id = 8, "manga", "new-revision"
        session.add(LocalPreference(work_id=3, is_hidden=True))
        session.add(Work(id=26, pixiv_illust_id="26", artist_id=1, title="Added", page_count=3))
    new = sessions.prepare(LibraryFilter()).result(10)
    assert rows(new) == library.reading_manifest()
    assert rows(old) == before and sessions._visits[token] is old
    assert sum(call is None for call in calls) == 1
    assert {1, 3, 26}.issubset(set().union(*(call for call in calls if call is not None)))
    count = len(calls)
    with database.write() as session:
        session.get(Artist, 1).name = "Renamed"
    sessions.prepare(LibraryFilter(q="Renamed")).result(10)
    assert len(calls) == count  # Empty-ID events invalidate complex scopes only.


def test_complex_singleflight_independent_cancel_and_recent_keys(catalogue, monkeypatch):
    _database, library, sessions = catalogue
    sessions.prepare(LibraryFilter()).result(10)
    entered, release, calls = Event(), Event(), []
    manifest = library.reading_manifest_snapshot
    def held(filters, **kwargs):
        calls.append(filters)
        entered.set()
        assert release.wait(10)
        return manifest(filters, **kwargs)
    monkeypatch.setattr(library, "reading_manifest_snapshot", held)
    filters = LibraryFilter(q="Alpha")
    cancelled = sessions.prepare(filters)
    assert entered.wait(10)
    other = sessions.prepare(filters)
    assert cancelled.cancel()
    release.set()
    result = other.result(10)
    assert rows(result) == library.reading_manifest(filters)
    assert len(calls) == 1
    assert sessions.prepare(filters).result(10).seed != result.seed
    assert len(calls) == 1
    # Frozen recent scopes reuse; moving wall-clock scopes do not.
    frozen = LibraryFilter(recent=True, as_of=REFERENCE)
    sessions.prepare(frozen).result(10)
    sessions.prepare(frozen).result(10)
    assert len(calls) == 2
    moving = replace(frozen, as_of=None)
    sessions.prepare(moving).result(10)
    sessions.prepare(moving).result(10)
    assert len(calls) == 4


def test_delta_ahead_of_notification_waits_and_rereads_complete_set(catalogue, monkeypatch):
    database, library, sessions = catalogue
    delayed, entered, allow_read, read_returned = [], Event(), Event(), Event()
    hold = [False]
    subscribe = database.subscribe_catalogue_changes
    def subscribing(callback):
        def notify(generation, ids):
            if hold[0]:
                delayed.append((callback, generation, ids))
            else:
                callback(generation, ids)
        return subscribe(notify)
    monkeypatch.setattr(database, "subscribe_catalogue_changes", subscribing)
    sessions.prepare(LibraryFilter()).result(10)
    baseline = sessions._preparation._generation
    basis, deltas = library.reading_basis, []
    def held(ids=None):
        if ids is not None:
            deltas.append(ids)
            if len(deltas) == 1:
                entered.set()
                assert allow_read.wait(10)
        result = basis(ids)
        if ids is not None:
            read_returned.set()
        return result
    monkeypatch.setattr(library, "reading_basis", held)
    with database.write() as session:
        session.get(Work, 1).page_count = 7
    assert entered.wait(10)
    waiting = sessions.prepare(LibraryFilter())
    hold[0] = True
    with database.write() as session:
        session.get(Work, 4).page_count = 9
    allow_read.set()
    assert read_returned.wait(10)
    assert sessions._preparation._generation == baseline
    assert not waiting.done()
    hold[0] = False
    for callback, generation, ids in delayed:
        callback(generation, ids)
    result = waiting.result(10)
    assert rows(result) == library.reading_manifest()
    assert len(deltas) >= 2 and {1, 4}.issubset(deltas[-1])
    assert sessions._preparation._generation > baseline


def test_failure_is_explicit_retry_recovers_and_shutdown_cancels_read(catalogue, monkeypatch):
    _database, library, sessions = catalogue
    basis = library.reading_basis
    entered, fail = Event(), Event()
    def broken(_ids=None):
        entered.set()
        assert fail.wait(10)
        raise RuntimeError("synthetic read failure")
    monkeypatch.setattr(library, "reading_basis", broken)
    future = sessions.prepare(LibraryFilter())
    assert entered.wait(10)
    fail.set()
    with pytest.raises(RuntimeError, match="synthetic read failure"):
        future.result(10)
    monkeypatch.setattr(library, "reading_basis", basis)
    assert sessions.prepare(LibraryFilter()).result(10).members
    entered.clear()
    def blocked(filters, **kwargs):
        entered.set()
        budget = current_read.get()
        assert budget.cancelled.wait(10)
        budget.checkpoint()
    monkeypatch.setattr(library, "reading_manifest_snapshot", blocked)
    waiting = sessions.prepare(LibraryFilter(tags=("A",)))
    assert entered.wait(10)
    sessions.request_stop()
    assert sessions.stop(10)
    with pytest.raises(ReadCancelled):
        waiting.result(10)


def test_full_scan_retains_commits_after_snapshot(catalogue, monkeypatch):
    database, library, sessions = catalogue
    read, release = Event(), Event()
    basis, calls = library.reading_basis, []
    def held(ids=None):
        calls.append(ids)
        result = basis(ids)
        if ids is None:
            read.set()
            assert release.wait(10)
        return result
    monkeypatch.setattr(library, "reading_basis", held)
    sessions.start()
    assert read.wait(10)
    with database.write() as session:
        session.get(Work, 1).page_count = 11
    waiting = sessions.prepare(LibraryFilter())
    release.set()
    result = waiting.result(10)
    assert rows(result) == library.reading_manifest()
    assert calls[0] is None and 1 in calls[1]


def test_claim_waits_for_real_commit_notification_barrier(catalogue, monkeypatch):
    database, _library, sessions = catalogue
    sessions.prepare(LibraryFilter()).result(10)
    wait_until(lambda: sessions._preparation._stock is not None)
    committed, release, barrier_entered = Event(), Event(), Event()
    errors = []
    publish, barrier = database._publish_catalogue_changes, database.catalogue_commit_barrier
    def held_publish(generation, ids):
        committed.set()
        assert release.wait(10)
        publish(generation, ids)
    def observed_barrier(checkpoint):
        barrier_entered.set()
        barrier(checkpoint)
    monkeypatch.setattr(database, "_publish_catalogue_changes", held_publish)
    monkeypatch.setattr(database, "catalogue_commit_barrier", observed_barrier)
    def write():
        try:
            with database.write() as session:
                session.add(Work(id=26, pixiv_illust_id="26", artist_id=1, title="Committed", page_count=1))
        except Exception as exc:
            errors.append(exc)
    writer = Thread(target=write)
    writer.start()
    try:
        assert committed.wait(10)
        # The row is already visible in SQL, while its callback is held.
        with database.read() as session:
            assert session.get(Work, 26) is not None
        waiting = sessions.prepare(LibraryFilter())
        assert barrier_entered.wait(10)
        assert not waiting.done()
        release.set()
        result = waiting.result(10)
        assert 26 in {member.work_id for member in result.members}
    finally:
        release.set()
        writer.join(10)
    assert not writer.is_alive() and not errors


def test_pending_queue_is_bounded_and_prunes_cancelled(catalogue, monkeypatch):
    database, _library, sessions = catalogue
    sessions.prepare(LibraryFilter()).result(10)
    entered, release = Event(), Event()
    barrier = database.catalogue_commit_barrier
    def held(checkpoint):
        entered.set()
        assert release.wait(10)
        barrier(checkpoint)
    monkeypatch.setattr(database, "catalogue_commit_barrier", held)
    running = sessions.prepare(LibraryFilter())
    assert entered.wait(10)
    queued = [sessions.prepare(LibraryFilter()) for _ in range(63)]
    overflow = sessions.prepare(LibraryFilter())
    with pytest.raises(LocalReadBusy):
        overflow.result(1)
    assert queued[0].cancel()
    replacement = sessions.prepare(LibraryFilter())
    assert not replacement.done()
    # Keep this test focused on admission rather than building 64 visits.
    for future in queued[1:]:
        future.cancel()
    release.set()
    assert running.result(10).members
    assert replacement.result(10).members


def test_stop_during_member_construction_skips_shuffle(catalogue, monkeypatch):
    _database, _library, sessions = catalogue
    sessions.prepare(LibraryFilter()).result(10)
    preparation = sessions._preparation
    make = preparation.make_visit
    from src.core.reading import Visit
    shuffle_calls = []
    original_order = Visit.order
    def ordered(visit, *args):
        shuffle_calls.append(args)
        return original_order(visit, *args)
    def interrupted(manifest):
        def members():
            for item in manifest:
                sessions.request_stop()
                yield item
        return make(members())
    # Let the initial standby finish before replacing its constructor.
    wait_until(lambda: preparation._stock is not None)
    monkeypatch.setattr(Visit, "order", ordered)
    monkeypatch.setattr(preparation, "make_visit", interrupted)
    waiting = sessions.prepare(LibraryFilter(sort="title"))
    with pytest.raises(ReadCancelled):
        waiting.result(10)
    assert sessions.stop(10)
    assert not shuffle_calls
