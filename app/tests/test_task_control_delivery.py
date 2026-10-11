"""Consolidated delivery: synthetic fair scheduling, restartable controls and scale."""
import json
import shutil
import statistics
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event, insert, select

from src.core.database import ArchiveDatabase
from src.core.models import Operation, OperationItem, TaskRound, utcnow
from src.core.tasks import ItemResult, ItemSpec, PlanBatch, TaskQueue


class Handler:
    def __init__(self):
        self.executed = []
        self.pause_key = None
    def plan(self, parameters, context):
        return [ItemSpec(str(i), "checked", {}, "checking") for i in range(parameters.get("count", 3))]
    def execute(self, item, context):
        self.executed.append((context.operation_id, item.key))
        if item.key == self.pause_key:
            self.pause_key = None
            context.queue.set_manual_paused(context.operation_id, True)
            context.checkpoint()
        return ItemResult()


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    path = tmp_path_factory.mktemp("task-control-schema") / "template.db"
    database = ArchiveDatabase.create(path)
    database.close()
    return path


@pytest.fixture
def db(tmp_path, template):
    path = tmp_path / "archive.db"
    shutil.copyfile(template, path)
    database = ArchiveDatabase(path)
    yield database
    database.close()


def test_manual_pause_persists_and_continues_only_unfinished(db):
    handler = Handler()
    queue = TaskQueue(db, {"reconcile": handler})
    op = queue.submit("reconcile", {"count": 3}, "synthetic reconcile")["operation_id"]
    assert queue._step("check")
    assert queue._step("check")
    handler.pause_key = "1"
    assert queue._step("check")
    assert queue.details(op)["state"] == "paused"
    assert queue.details(op)["counts"] == {"pending": 2, "succeeded": 1}
    with db.read() as session:
        paused_at = session.get(Operation, op).updated_at
    queue.set_manual_paused(op, True)
    with db.read() as session:
        assert session.get(Operation, op).updated_at == paused_at
    restored = TaskQueue(db, {"reconcile": handler})
    restored.recover()
    assert restored.details(op)["pause_requested"] is True
    assert not restored._step("check")
    restored.set_manual_paused(op, False)
    with db.read() as session:
        assert session.get(Operation, op).updated_at > paused_at
    assert restored.run_round()
    assert [key for _, key in handler.executed] == ["0", "1", "1", "2"]
    assert restored.details(op)["counts"] == {"succeeded": 3}
    assert restored.details(op)["state"] == "finished"


def test_later_small_operation_runs_within_two_yields_and_big_keeps_progress(db):
    handler = Handler()
    queue = TaskQueue(db, {"reconcile": handler, "metadata": handler})
    big = queue.submit("reconcile", {"count": 80}, "big")["operation_id"]
    assert queue._step("check")
    assert queue._step("check")
    small = queue.submit("metadata", {"count": 1}, "small")["operation_id"]
    assert queue._step("check")
    assert queue._step("check")
    assert handler.executed[-1] == (small, "0")
    assert queue._step("check")
    assert handler.executed[-1] == (big, "1")


def test_full_foreground_admission_window_cannot_starve_maintenance(db):
    handler = Handler()
    queue = TaskQueue(db, {"metadata": handler})
    for number in range(130):
        queue.submit("metadata", {"count": 1, "unique": number}, "foreground")
    maintenance = queue.submit("metadata", {"count": 1, "unique": "maintenance"},
                               "background", source="maintenance")["operation_id"]
    with db.write() as session:
        first = queue._claim_round_in_transaction(session)
        assert session.get(Operation, maintenance).round_id is None
        session.get(TaskRound, first).state = "finished"
        queue._claim_round_in_transaction(session)
        assert session.get(Operation, maintenance).round_id is not None
    for _ in range(16):
        assert queue._step("check")
        if any(ident == maintenance for ident, _ in handler.executed):
            break
    assert any(ident == maintenance for ident, _ in handler.executed)


def test_pause_parent_holds_download_child_without_changing_automatic_pause(db):
    handler = Handler()
    queue = TaskQueue(db, {"reconcile": handler, "repair": handler})
    parent = queue.submit("reconcile", {"count": 1}, "parent")["operation_id"]
    assert queue._step("check")
    with db.write() as session:
        child = queue.enqueue_in_transaction(session, "repair", {}, "child", parent_id=parent,
            specs=[ItemSpec("download", "download", {}, "downloading")])["operation_id"]
    queue.set_manual_paused(parent, True)
    restored = TaskQueue(db, queue.handlers)
    restored.recover()
    assert not restored._step("download")
    assert restored.automatic_paused() is False
    restored.set_manual_paused(parent, False)
    assert restored._step("download")
    assert restored.details(child)["state"] == "finished"


def test_cooldown_yields_worker_and_retry_budget_is_finite(db):
    class RateLimited(RuntimeError):
        retryable = True
        retry_after = 60
        failure_code = "api_rate_limit"
        status = 429
    handler = Handler()
    sent_requests = []
    def fail(item, context):
        # A server rejection consumed an actual request; local cooldown did not.
        context.mark_remote_attempt()
        sent_requests.append(item.key)
        raise RateLimited("no remote message retained")
    handler.execute = fail
    queue = TaskQueue(db, {"reconcile": handler, "metadata": Handler()})
    op = queue.submit("reconcile", {"count": 1}, "limited")["operation_id"]
    assert queue._step("check")
    assert queue._step("check")
    assert queue.details(op)["counts"] == {"pending": 1}
    assert not queue._step("check")
    small = queue.submit("metadata", {"count": 1}, "local metadata")["operation_id"]
    assert queue._step("check")
    assert queue._step("check")
    assert queue.details(small)["state"] == "finished"
    for attempt in range(2):
        with db.write() as session:
            session.scalar(select(OperationItem).where(OperationItem.operation_id == op)).not_before = utcnow() - timedelta(seconds=1)
        assert queue._step("check")
    assert queue.details(op)["state"] == "finished"
    assert queue.details(op)["items"][0]["attempts"] == 3
    assert len(sent_requests) == 3
    assert "no remote message" not in json.dumps(queue.details(op))


def test_planner_batches_resume_cursor_without_repeating_manifest(db):
    class Bounded(Handler):
        def plan_batch(self, parameters, context, cursor):
            start = cursor.get("after", 0)
            stop = min(start + 4, 20)
            return PlanBatch([ItemSpec(str(i), "checked", {}, "checking") for i in range(start, stop)],
                {"after": stop}, stop == 20)
    handler = Bounded()
    queue = TaskQueue(db, {"reconcile": handler})
    op = queue.submit("reconcile", {}, "bounded")["operation_id"]
    assert queue._step("check")
    assert queue.details(op)["result"]["planning_cursor"] == {"after": 4}
    queue.set_manual_paused(op, True)
    restored = TaskQueue(db, queue.handlers)
    restored.recover()
    assert not restored._step("check")
    restored.set_manual_paused(op, False)
    assert restored.run_round()
    assert len(handler.executed) == 20
    assert len({key for _, key in handler.executed}) == 20
    assert restored.details(op)["counts"] == {"succeeded": 20}


@pytest.mark.parametrize("size", [2000, 20000, 100000])
def test_indexed_claim_scale_and_short_write_boundary(db, size, tmp_path):
    handler = Handler()
    queue = TaskQueue(db, {"reconcile": handler})
    with db.write() as session:
        session.add(TaskRound(id="scale-round"))
        session.flush()
        session.add(Operation(id="scale-op", kind="reconcile", label="scale", dedupe_key="scale",
            state="ready", round_id="scale-round", result={"planning_complete": True}))
        session.flush()
        rows = []
        for i in range(size):
            # Mixed real dependency pattern; unresolved children interleave runnable inspections.
            rows.append(dict(id=f"item-{i}", operation_id="scale-op", item_key=str(i), position=i,
                kind="checked", target={}, phase="checking", unit="items", lane="check", state="pending",
                depends_on=[str(i-1)] if i % 3 == 1 else [], attempts=0, result={}))
        for start in range(0, size, 1000):
            session.execute(insert(OperationItem), rows[start:start+1000])
    candidate_times, write_times, plans = [], [], []
    def before(connection, cursor, statement, parameters, context, many):
        context._task_query_started = time.perf_counter()
        if "operation_items.position >" in statement or "operation_items.item_key IN" in statement:
            cursor.execute("EXPLAIN QUERY PLAN " + statement, parameters)
            plans.extend([list(row) for row in cursor.fetchall()])
    def after(connection, cursor, statement, parameters, context, many):
        if "operation_items.position >" in statement:
            candidate_times.append((time.perf_counter() - context._task_query_started) * 1000)
    write = db.write
    @contextmanager
    def measured_write():
        with write() as session:
            started = time.perf_counter()
            yield session
        write_times.append((time.perf_counter() - started) * 1000)
    db.write = measured_write
    event.listen(db.engine, "before_cursor_execute", before)
    event.listen(db.engine, "after_cursor_execute", after)
    try:
        for _ in range(20):
            assert queue._step("check")
    finally:
        event.remove(db.engine, "before_cursor_execute", before)
        event.remove(db.engine, "after_cursor_execute", after)
        db.write = write
    assert candidate_times and write_times and plans
    p95 = sorted(candidate_times)[int((len(candidate_times)-1) * .95)]
    result = {"size": size, "claim_query_ms": candidate_times, "p95_ms": p95,
        "write_boundary_ms": write_times, "max_write_ms": max(write_times), "query_plan": plans}
    (tmp_path / "claim-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"scale={size} p95_claim_ms={p95:.3f} max_write_ms={max(write_times):.3f}")
    assert p95 <= 200
    assert max(write_times) < 1000
    assert any("operation_id=? AND item_key=?" in row[-1] for row in plans)
    assert any("ix_items_candidate" in row[-1] for row in plans)


def test_two_thousand_page_inventory_is_bounded_and_pause_cursor_survives(db, tmp_path):
    from sqlalchemy import func
    from src.core.media import Publisher
    from src.core.models import ArchiveIntent, Artist, Asset, Work
    from src.core.reconcile import ReconcileHandler
    with db.write() as session:
        artist = Artist(id=1, pixiv_user_id="1", name="synthetic")
        session.add_all([Work(id=1, pixiv_illust_id="100", artist=artist, title="large manga", type="manga", page_count=2000),
            ArchiveIntent(pixiv_illust_id="100")])
    images = tmp_path / "images"
    images.mkdir()
    handler = ReconcileHandler(db, Publisher(db, images), object())
    queue = TaskQueue(db, {"reconcile": handler})
    op = queue.submit("reconcile", {"work_ids": [1]}, "large page inventory")["operation_id"]
    assert queue._step("check")
    with db.read() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 64
        assert session.get(Operation, op).result["planning_cursor"]["work_inventory"]["page_after"] == 64
    queue.set_manual_paused(op, True)
    restored = TaskQueue(db, queue.handlers)
    restored.recover()
    assert not restored._step("check")
    assert not restored._plan(op)
    with db.read() as session:
        assert session.get(Operation, op).pause_requested is True
        assert session.get(Operation, op).result["planning_cursor"]["work_inventory"]["page_after"] == 64
    restored.set_manual_paused(op, False)
    # Drive the real inventory seam here: _step deliberately alternates ready
    # checking with planning, so a pure-inventory attempts=0 assertion cannot
    # describe a complete scheduler run.
    assert restored._plan(op)
    with db.read() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 128
        assert session.get(Operation, op).result["planning_cursor"]["work_inventory"]["page_after"] == 128
    while restored.details(op)["result"]["planning_complete"] is False:
        with db.read() as session:
            before = session.scalar(select(func.count()).select_from(Asset))
        assert restored._plan(op)
        with db.read() as session:
            after = session.scalar(select(func.count()).select_from(Asset))
            assert 0 <= after - before <= 64
    with db.read() as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 2000
        rows = list(session.scalars(select(OperationItem).where(OperationItem.operation_id == op)))
        assert max(map(lambda item: len(item.depends_on), rows)) <= 65
        assert len([item for item in rows if item.kind == "reconcile_asset"]) == 2000
        assert all(item.attempts == 0 for item in rows)
    # Keep the separate scheduler contract: a ready local inspection really
    # claims/settles once, without accidentally executing the remaining pages.
    assert restored._step("local")
    with db.read() as session:
        claimed = list(session.scalars(select(OperationItem).where(OperationItem.operation_id == op,
            OperationItem.attempts != 0)))
        assert len(claimed) == 1
        assert claimed[0].kind == "reconcile_asset" and claimed[0].attempts == 1
        assert claimed[0].state == "succeeded"
        assert session.scalar(select(func.count()).select_from(Asset)) == 2000


@pytest.mark.parametrize("interrupted", [False, True])
def test_legacy_bookmark_item_keeps_identity_and_dependency_barrier_across_pause(db, tmp_path, interrupted):
    from src.core.archive import ArchiveHandler
    from src.core.bookmarks import BookmarkService
    from src.core.media import Publisher
    from src.core.models import ArchiveIntent, Artist, RemoteBookmark, Work
    class Remote:
        def __init__(self):
            self.calls = []
        def get_account_id(self):
            return "9"
        def get_artist_illusts_page(self, remote_id, work_type="illust", offset=0):
            self.calls.append((work_type, offset))
            if work_type == "manga":
                return {"items": [], "next_offset": None}
            end = min(offset + 2, 6)
            return {"items": [{"illust_id": str(100 + i), "is_bookmarked": True} for i in range(offset, end)],
                    "next_offset": end if end < 6 else None}
        def add_bookmark(self, *_):
            raise AssertionError("legacy sync must never issue a bookmark write")
        delete_bookmark = add_bookmark
    class LegacyHandler(ArchiveHandler):
        def execute(self, item, context):
            if item.kind == "checked":
                with db.read() as session:
                    assert all(session.scalars(select(RemoteBookmark.value)))
                return ItemResult()
            return super().execute(item, context)
    with db.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        session.flush()
        for i in range(6):
            work = Work(id=i + 1, pixiv_illust_id=str(100 + i), artist_id=1, title="synthetic")
            work.bookmark = RemoteBookmark(value=False)
            session.add_all([work, ArchiveIntent(pixiv_illust_id=str(100 + i))])
    remote = Remote()
    images = tmp_path / "images"
    images.mkdir()
    handler = LegacyHandler("reconcile", db, Publisher(db, images), remote,
                            bookmarks=BookmarkService(db, remote))
    queue = TaskQueue(db, {"reconcile": handler})
    with db.write() as session:
        operation = queue.enqueue_in_transaction(session, "reconcile", {"work_ids": []}, "legacy",
            specs=[ItemSpec("legacy-bookmark", "bookmark_artist", {"artist_id": 1}, "bookmarks", "artists"),
                   ItemSpec("after-bookmarks", "checked", {}, "checking", depends_on=("legacy-bookmark",))])["operation_id"]
        round_record = TaskRound(state="running", frozen_at=utcnow())
        session.add(round_record)
        session.flush()
        op = session.get(Operation, operation)
        op.round_id, op.result = round_record.id, {**op.result, "planning_complete": True}
        if interrupted:
            op.state = "interrupted"
        legacy = session.scalar(select(OperationItem).where(OperationItem.item_key == "legacy-bookmark"))
        ident = legacy.id
        legacy.state = "running" if interrupted else "pending"
    queue.recover()
    assert queue._step("check")  # One bounded reservation, no remote call.
    assert remote.calls == []
    assert queue._step("check")  # Exactly one remote page; original barrier remains pending.
    assert remote.calls == [("illust", 0)]
    assert queue.details(operation)["phase"]["total"] is None
    with db.read() as session:
        assert session.get(OperationItem, ident).state == "pending"
        assert session.get(OperationItem, ident).attempts == 0
        assert session.get(OperationItem, ident).target["legacy_bookmark_cursor"]["offset"] == 2
        assert session.scalar(select(OperationItem.state).where(OperationItem.item_key == "after-bookmarks")) == "pending"
    queue.set_manual_paused(operation, True)
    restored = TaskQueue(db, queue.handlers)
    restored.recover()
    assert not restored._step("check")
    assert remote.calls == [("illust", 0)]
    restored.set_manual_paused(operation, False)
    assert restored.run_round()
    with db.read() as session:
        assert session.get(OperationItem, ident).state == "succeeded"
        assert session.get(OperationItem, ident).item_key == "legacy-bookmark"
        assert session.scalar(select(OperationItem.state).where(OperationItem.item_key == "after-bookmarks")) == "succeeded"
        assert all(session.scalars(select(RemoteBookmark.value)))
    assert remote.calls == [("illust", 0), ("illust", 2), ("illust", 4), ("manga", 0)]
