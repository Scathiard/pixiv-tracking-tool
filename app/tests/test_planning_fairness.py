"""Ready checks and unfinished planning share bounded turns; synthetic data only."""
import io
import threading
import time
from datetime import timedelta

import pytest
from PIL import Image
from sqlalchemy import select

from src.core.database import ArchiveDatabase
from src.core.models import ArchiveIntent, Artist, Operation, OperationItem, TrackingPolicy, Work, utcnow
from src.core.runtime import Runtime
from src.core.tasks import ItemResult, ItemSpec, PlanBatch, TaskQueue


class SyntheticClient:
    def get_account_id(self):
        return "synthetic-account"

    def get_artist_detail(self, ident):
        return {"user_id": ident, "name": "synthetic artist", "biography": "synthetic"}

    def get_illust_detail(self, ident):
        return {"illust_id": ident, "title": "synthetic work", "caption": "", "type": "illust",
                "page_count": 1, "is_bookmarked": False,
                "urls": [{"original": f"https://example.invalid/{ident}_p0.png"}]}


class SyntheticDownloader:
    def download(self, url, destination, report=None):
        assert url.startswith("https://example.invalid/")
        data = io.BytesIO()
        Image.new("RGB", (16, 12), "blue").save(data, "PNG")
        destination.write_bytes(data.getvalue())
        if report:
            report(len(data.getvalue()), len(data.getvalue()))


@pytest.mark.parametrize("check_workers", [1, 2])
def test_real_reconcile_checks_and_downloads_before_inventory_finishes(tmp_path, check_workers):
    runtime = Runtime(tmp_path / "synthetic.db", tmp_path / "images", create=True,
                      client=SyntheticClient(), downloader=SyntheticDownloader(), check_workers=check_workers)
    runtime.queue.maintenance = None
    db, queue, events = runtime.database, runtime.queue, []
    try:
        with db.write() as session:
            artist = Artist(id=1, pixiv_user_id="10", name="synthetic artist")
            artist.policy = TrackingPolicy(mode="stopped")
            session.add(artist)
            session.flush()
            for ident in range(1, 4):
                session.add(Work(id=ident, pixiv_illust_id=str(100 + ident), artist_id=1,
                                 title="synthetic work", type="illust", page_count=1,
                                 source_metadata={"urls": [{"original": f"https://example.invalid/{100 + ident}_p0.png"}]}))
                session.add(ArchiveIntent(pixiv_illust_id=str(100 + ident), mode="keep", revision=1))
        operation_id = queue.submit("reconcile", {}, "synthetic reconcile")["operation_id"]
        handler = queue.handlers["reconcile"]
        original = handler.execute

        def execute(item, context):
            with db.read() as session:
                complete = session.get(Operation, operation_id).result.get("planning_complete")
            events.append((item.kind, complete))
            return original(item, context)

        handler.execute = execute
        for _ in range(60):
            queue._step("check")
            for _ in range(8):
                if not queue._step("local"):
                    break
            queue._step("download")
            with db.read() as session:
                if session.get(Operation, operation_id).state == "finished":
                    break
        for kind in ("metadata_artist", "reconcile_snapshot", "reconcile_prepare", "download"):
            assert (kind, False) in events, f"{kind} waited for the full inventory"
        with db.read() as session:
            operation = session.get(Operation, operation_id)
            assert operation.state == "finished"
            assert operation.result["outcome"] == "succeeded"
        assert operation_id not in queue._check_turns
    finally:
        runtime.close()


class BatchHandler:
    def __init__(self, batches=20, items_per_batch=2):
        self.batches, self.items_per_batch = batches, items_per_batch
        self.events = []

    def plan_batch(self, parameters, context, cursor):
        batch = cursor.get("batch", 0)
        self.events.append(("plan", batch))
        specs = [ItemSpec(f"check-{batch}-{index}", "checked", {}, "checking")
                 for index in range(self.items_per_batch)]
        return PlanBatch(specs, {"batch": batch + 1}, batch + 1 == self.batches)

    def execute(self, item, context):
        self.events.append(("check", item.key))
        return ItemResult()


@pytest.fixture
def queue_case(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    handler = BatchHandler()
    queue = TaskQueue(db, {"reconcile": handler})
    ident = queue.submit("reconcile", {}, "synthetic")["operation_id"]
    yield db, handler, queue, ident
    queue.stop()
    db.close()


def test_ready_backlog_cannot_starve_later_inventory_batches(queue_case):
    db, handler, queue, ident = queue_case
    for _ in range(80):
        queue._step("check")
    assert [event[0] for event in handler.events[:40]] == ["plan", "check"] * 20
    assert [event[1] for event in handler.events if event[0] == "plan"] == list(range(20))
    executed = [event[1] for event in handler.events if event[0] == "check"]
    assert len(executed) == len(set(executed)) == 40
    with db.read() as session:
        assert session.get(Operation, ident).state == "finished"


def test_planning_cooldown_does_not_hide_already_ready_checks(queue_case):
    _, handler, queue, ident = queue_case
    assert queue._step("check")
    queue._defer_plan(ident, 60, "synthetic planning cooldown")
    assert queue._step("check")
    assert queue._step("check")
    assert [event[0] for event in handler.events] == ["plan", "check", "check"]
    assert not queue._step("check")


def test_checking_cooldown_does_not_hide_remaining_planning(queue_case):
    db, handler, queue, ident = queue_case
    handler.items_per_batch = 1
    assert queue._step("check")
    with db.write() as session:
        item = session.scalar(select(OperationItem).where(OperationItem.operation_id == ident))
        item.not_before = utcnow() + timedelta(seconds=60)
    # Item turn has no eligible checking; planning makes progress immediately.
    assert queue._step("check")
    assert handler.events == [("plan", 0), ("plan", 1)]
    assert queue._step("check")
    assert handler.events[-1] == ("check", "check-1-0")
    with db.read() as session:
        old = session.scalar(select(OperationItem).where(OperationItem.item_key == "check-0-0"))
        assert old.state == "pending" and old.attempts == 0


def test_pause_offline_and_restart_preserve_cursor_and_pending_checks(queue_case):
    db, handler, queue, ident = queue_case
    handler.batches = 4
    assert queue._step("check")
    queue.set_manual_paused(ident, True)
    assert not queue._step("check")
    queue.set_manual_paused(ident, False)
    queue.allowed_kinds = lambda: ()
    assert not queue._step("check")
    queue.allowed_kinds = None
    # Recreate the queue, just as recovery does. A fresh fairness hint may
    # choose one planning step first but must not drain the entire inventory.
    queue = TaskQueue(db, {"reconcile": handler})
    queue.recover()
    assert queue._step("check")
    assert queue._step("check")
    assert handler.events == [("plan", 0), ("plan", 1), ("check", "check-0-0")]
    for _ in range(20):
        queue._step("check")
    assert [batch for kind, batch in handler.events if kind == "plan"] == [0, 1, 2, 3]
    checks = [key for kind, key in handler.events if kind == "check"]
    assert len(checks) == len(set(checks)) == 8
    with db.read() as session:
        operation = session.get(Operation, ident)
        assert operation.state == "finished" and operation.result["planning_cursor"] == {"batch": 4}


def test_real_two_check_workers_exclude_same_operation_and_advance_both(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    entered, overlap, release = threading.Event(), threading.Event(), threading.Event()
    lock, active, peaks, events = threading.Lock(), {}, {}, []

    class ConcurrentHandler:
        def record(self, context, kind, value):
            ident = context.operation_id
            with lock:
                active[ident] = active.get(ident, 0) + 1
                peaks[ident] = max(peaks.get(ident, 0), active[ident])
                events.append((ident, kind, value))
                first = not entered.is_set()
                if first:
                    entered.set()
                if len([count for count in active.values() if count]) == 2:
                    overlap.set()
            if first:
                assert release.wait(5)
            time.sleep(.01)
            with lock:
                active[ident] -= 1

        def plan_batch(self, parameters, context, cursor):
            batch = cursor.get("batch", 0)
            self.record(context, "plan", batch)
            return PlanBatch([ItemSpec(f"check-{batch}", "checked", {}, "checking")],
                             {"batch": batch + 1}, batch == 5)

        def execute(self, item, context):
            self.record(context, "check", item.key)
            return ItemResult()

    queue = TaskQueue(db, {"reconcile": ConcurrentHandler()}, check_workers=2)
    ids = [queue.submit("reconcile", {"scope": index}, "synthetic")["operation_id"] for index in (1, 2)]
    try:
        queue.start()
        assert entered.wait(5)
        assert overlap.wait(5), "Second check worker did not advance an independent operation"
        release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with db.read() as session:
                finished = all(session.get(Operation, ident).state == "finished" for ident in ids)
            if finished:
                break
            time.sleep(.02)
        assert finished and queue.worker_error is None
        assert peaks == {ident: 1 for ident in ids}
        for ident in ids:
            own = [(kind, value) for operation, kind, value in events if operation == ident]
            assert [kind for kind, _ in own] == ["plan", "check"] * 6
            assert [value for kind, value in own if kind == "plan"] == list(range(6))
    finally:
        release.set()
        assert queue.stop()
        db.close()
