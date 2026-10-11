"""Claim-stage display progress shares one transaction; synthetic files only."""
import io
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import event, select

from src.core.database import ArchiveDatabase
from src.core.media import Publisher
from src.core.models import ArchiveIntent, Artist, Asset, Operation, OperationItem, Work
from src.core.reconcile import ReconcileHandler
from src.core.revisions import ensure_legacy_revision
from src.core.tasks import ItemResult, ItemSpec, TaskContext, TaskQueue
from src.core import projection_maintenance as maintenance
from src.core.activity_log import processing_log


def test_item_progress_keeps_parent_event_time_and_survives_reopen(case, monkeypatch):
    db, queue = case
    from src.core import tasks
    started = datetime(2026, 10, 11, 12)
    now = [started + timedelta(hours=1)]
    monkeypatch.setattr(tasks, "utcnow", lambda: now[0])

    class ReportingHandler:
        def execute(self, item, context):
            context.report("verifying", int(item.key), 2, "assets", "synthetic progress")
            context.report_step("verifying", "assets")
            context.detail("synthetic detail")
            return ItemResult()

    queue.handlers["verify"] = ReportingHandler()
    with db.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        session.add_all([Work(id=i, pixiv_illust_id=str(i), artist_id=1,
                             title="synthetic", type="illust", page_count=1) for i in (1, 2)])
    ident = enqueue(db, queue, [ItemSpec(str(i - 1), "checked", {"work_id": i},
                                       "verifying", "assets") for i in (1, 2)])
    with db.write() as session:
        parent = session.get(Operation, ident)
        parent.state, parent.started_at, parent.updated_at = "running", started, started
        session.add(Operation(id="other-finished", kind="verify", label="older finished",
                              dedupe_key="other-finished", state="finished",
                              updated_at=started + timedelta(minutes=30),
                              finished_at=started + timedelta(minutes=30)))
    parent_rebuilds = []
    for name in ("reconcile_operation",):
        original = getattr(maintenance, name)
        def observe(*args, _name=name, _original=original, **kwargs):
            parent_rebuilds.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(maintenance, name, observe)
    with db.read() as session:
        untouched = session.connection().exec_driver_sql(
            "SELECT * FROM read_task_candidates WHERE work_id=2").all()
    assert queue._step("local")
    with db.read() as session:
        parent = session.get(Operation, ident)
        assert parent.updated_at == started
        assert (parent.phase_done, parent.phase_total, parent.detail) == (1, 2, "synthetic detail")
        assert parent.result["phase_snapshots"]["verifying:assets"]["done"] == 1
        assert session.connection().exec_driver_sql(
            "SELECT * FROM read_task_candidates WHERE work_id=2").all() == untouched
        assert session.connection().exec_driver_sql("SELECT count(*) FROM read_model_dirty").scalar() == 0
    assert parent_rebuilds == []
    queue.stop()
    db.close()
    reopened = ArchiveDatabase(db.path)
    resumed = TaskQueue(reopened, {"verify": ReportingHandler()})
    try:
        detail = resumed.details(ident)
        assert detail["phase"]["done"] == 1 and detail["phase"]["total"] == 2
        assert detail["counts"] == {"pending": 1, "succeeded": 1}
        now[0] += timedelta(hours=1)
        assert resumed._step("local")
        with reopened.read() as session:
            parent = session.get(Operation, ident)
            assert parent.state == "finished" and parent.phase_done == 2
            assert parent.updated_at == parent.finished_at == now[0]
        assert processing_log(reopened, limit=1)["lines"][0].startswith(
            now[0].isoformat(timespec="seconds") + "Z [" + ident[:8] + "]")
    finally:
        resumed.stop()
        reopened.close()


class Handler:
    def execute(self, item, context):
        return ItemResult()


@pytest.fixture
def case(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    queue = TaskQueue(db, {"verify": Handler()})
    yield db, queue
    queue.stop()
    db.close()


def enqueue(db, queue, specs):
    with db.write() as session:
        return queue.enqueue_in_transaction(session, "verify", {}, "synthetic", specs=specs)["operation_id"]


def test_real_valid_zip_keeps_facts_and_uses_three_dml_commits(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    images = tmp_path / "images"
    images.mkdir()
    content = io.BytesIO()
    Image.new("RGB", (16, 12), "blue").save(content, "PNG")
    with zipfile.ZipFile(images / "synthetic.zip", "w") as archive:
        archive.writestr("0.png", content.getvalue())
        archive.writestr("1.png", content.getvalue())
    queue = TaskQueue(db, {"reconcile": ReconcileHandler(db, Publisher(db, images), object())})
    commits, changed = [], {}

    def statement(connection, cursor, sql, parameters, context, many):
        if sql.lstrip().split()[0].upper() in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            changed[id(connection)] = True

    def commit(connection):
        commits.append(changed.pop(id(connection), False))

    try:
        with db.write() as session:
            session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
            session.flush()
            work = Work(id=1, pixiv_illust_id="101", artist_id=1, title="synthetic", type="ugoira", page_count=1)
            session.add(work)
            session.add(ArchiveIntent(pixiv_illust_id="101", mode="keep", revision=1))
            session.flush()
            asset = Asset(work_id=1, role="ugoira_zip", page_index=0, relative_path="synthetic.zip", state="unknown")
            session.add(asset)
            session.flush()
            revision = ensure_legacy_revision(session, work)
            asset_id = asset.id
            ident = queue.enqueue_in_transaction(session, "reconcile", {}, "synthetic zip", specs=[
                ItemSpec("zip", "reconcile_asset", {"work_id": 1, "revision": 1,
                         "work_revision_id": revision.id, "asset_id": asset.id}, "verifying", "assets"),
                ItemSpec("later", "checked", {}, "inventory")])["operation_id"]
        event.listen(db.writer_engine, "before_cursor_execute", statement)
        event.listen(db.writer_engine, "commit", commit)
        assert queue._step("local")
        # Old valid-ZIP path had 4 DML commits plus empty finalization. Claim
        # and starting display progress now commit together: 3 DML + 1 empty.
        assert commits == [True, True, True, False]
        with db.read() as session:
            asset = session.get(Asset, asset_id)
            assert asset.state == "verified" and asset.sha256 and asset.byte_size
            operation = session.get(Operation, ident)
            assert operation.result["step_progress"]["verifying:assets"] == {"done": 1, "total": 1}
            assert operation.result["phase_snapshots"]["verifying:assets"] == {
                "phase": "verifying", "unit": "assets", "done": 1, "total": 1}
            item = session.scalar(select(OperationItem).where(OperationItem.item_key == "zip"))
            assert item.state == "succeeded"
    finally:
        event.remove(db.writer_engine, "before_cursor_execute", statement)
        event.remove(db.writer_engine, "commit", commit)
        queue.stop()
        db.close()


def test_empty_lanes_do_not_borrow_writer_but_later_work_still_finishes(case, monkeypatch):
    db, queue = case
    original, writes = db.write, []

    @contextmanager
    def observe():
        writes.append(True)
        with original() as session:
            yield session

    monkeypatch.setattr(db, "write", observe)
    for lane in ("check", "download", "local"):
        assert not queue._step(lane)
    assert not writes
    ident = enqueue(db, queue, [ItemSpec("one", "checked", {}, "verifying")])
    assert queue._wake.is_set()
    assert queue._step("local")
    with db.read() as session:
        assert session.get(Operation, ident).state == "finished"
    writes.clear()
    for lane in ("check", "download", "local"):
        assert not queue._step(lane)
    assert not writes


@pytest.mark.parametrize("direct_context", [False, True])
def test_rollback_cannot_validate_uncommitted_phase_or_strand_claim(case, monkeypatch, direct_context):
    db, queue = case
    ident = enqueue(db, queue, [ItemSpec("one", "checked", {}, "verifying", "assets"),
                               ItemSpec("two", "checked", {}, "verifying", "assets")])
    original = db.write

    @contextmanager
    def rollback():
        with original() as session:
            yield session
            raise RuntimeError("synthetic commit failure")

    monkeypatch.setattr(db, "write", rollback)
    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        if direct_context:
            TaskContext(queue, ident).report_step("verifying", "assets")
        else:
            queue._step("local")
    assert not queue._validated_phases and not queue._resources and not queue._checking
    with db.read() as session:
        operation = session.get(Operation, ident)
        assert not operation.result.get("step_progress")
        items = list(session.scalars(select(OperationItem).where(OperationItem.operation_id == ident)))
        assert all(item.state == "pending" and item.attempts == 0 for item in items)
    monkeypatch.setattr(db, "write", original)
    assert queue._step("local")
    with db.read() as session:
        assert session.get(Operation, ident).result["step_progress"]["verifying:assets"] == {"done": 1, "total": 2}


def test_claim_multichannel_snapshots_and_direct_report_recertify_restart(case):
    db, queue = case
    ident = enqueue(db, queue, [ItemSpec("verify-1", "checked", {}, "verifying", "assets"),
                               ItemSpec("verify-2", "checked", {}, "verifying", "assets"),
                               ItemSpec("check", "checked", {}, "checking", "works"),
                               ItemSpec("download", "download", {}, "downloading", "assets")])
    assert queue._step("local")
    assert queue._step("check")
    assert queue._step("download")
    with db.read() as session:
        operation = session.get(Operation, ident)
        assert operation.result["step_progress"] == {
            "verifying:assets": {"done": 1, "total": 2},
            "checking:works": {"done": 1, "total": 1},
            "downloading:assets": {"done": 1, "total": 1}}
    # Simulate an older writer settling a row while retaining the cached JSON.
    with db.write() as session:
        item = session.scalar(select(OperationItem).where(OperationItem.item_key == "verify-2"))
        item.state = "succeeded"
    restarted = TaskQueue(db, queue.handlers)
    restarted.recover()
    TaskContext(restarted, ident).report_step("verifying", "assets")
    with db.read() as session:
        operation = session.get(Operation, ident)
        assert operation.result["step_progress"]["verifying:assets"] == {"done": 2, "total": 2}
        assert operation.result["phase_snapshots"]["checking:works"]["done"] == 1
        assert operation.result["phase_snapshots"]["downloading:assets"]["done"] == 1
