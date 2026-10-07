"""Transient download bytes use actual TaskQueue/TaskContext, synthetic data."""
import io
import shutil
import threading
import time

import pytest
from sqlalchemy import event, select
from contextlib import contextmanager
from PIL import Image

from src.core.archive import ArchiveHandler
from src.core.database import ArchiveDatabase
from src.core.media import Publisher
from src.core.models import ArchiveIntent, Artist, Asset, Operation, OperationItem, PublicationJournal, Work
from src.core.revisions import ensure_legacy_revision
from src.core.tasks import ItemResult, ItemSpec, TaskQueue
from src.request_policy import RequestDeferred


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    path = tmp_path_factory.mktemp("byte-schema") / "template.db"
    database = ArchiveDatabase.create(path)
    database.close()
    return path


@pytest.fixture
def case(tmp_path, template):
    path = tmp_path / "synthetic.db"
    shutil.copyfile(template, path)
    db = ArchiveDatabase(path)
    queue = TaskQueue(db, {})
    yield db, queue
    assert queue.stop()
    db.close()


def enqueue(db, queue, handler, count=1, kind="repair"):
    queue.handlers[kind] = handler
    with db.write() as session:
        return queue.enqueue_in_transaction(session, kind, {}, "synthetic byte progress",
            specs=[ItemSpec(str(index), "download", {}, "downloading", "assets") for index in range(count)])["operation_id"]


def test_actual_reports_add_no_commits_and_preserve_settlement(case):
    db, queue = case
    commits, report_commits, observed = [], [], []

    class Handler:
        def execute(self, item, context):
            before = len(commits)
            for done in (10, 50, 100):
                context.report_bytes(done, 100)
                report_commits.append(len(commits) - before)
                view = queue.details(context.operation_id)
                observed.append((view["result"]["current_item"]["done"], view["counts"], view["phase"]["done"]))
            return ItemResult()

    ident = enqueue(db, queue, Handler())

    def committed(connection):
        commits.append(1)

    event.listen(db.writer_engine, "commit", committed)
    try:
        assert queue._step("download")
    finally:
        event.remove(db.writer_engine, "commit", committed)
    assert report_commits == [0, 0, 0], f"Each display report added durable commits: {report_commits}"
    assert observed == [(done, {"running": 1}, 0) for done in (10, 50, 100)]
    assert len(commits) == 3  # Atomic claim/start, actual settlement, existing finalize.
    assert queue.details(ident)["counts"] == {"succeeded": 1}
    assert "current_item" not in queue.details(ident)["result"]
    with db.read() as session:
        result = session.get(Operation, ident).result
        assert not result.get("current_items") and "current_item" not in result


def test_real_two_download_workers_merge_each_live_item_and_clear_after_finish(case):
    db, queue = case
    entered, release, guard = threading.Event(), threading.Event(), threading.Lock()
    contexts, errors = [], []

    class Handler:
        def execute(self, item, context):
            context.report_bytes(10 + int(item.key), None)
            with guard:
                contexts.append(context)
                if len(contexts) == 2:
                    entered.set()
            assert release.wait(5)
            context.report_bytes(100, 100)
            return ItemResult()

    ident = enqueue(db, queue, Handler(), count=2)

    def run():
        try:
            deadline = time.monotonic() + 5
            while not queue._step("download"):
                assert time.monotonic() < deadline
                time.sleep(0.01)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        assert entered.wait(5)
        view = queue.details(ident)
        current = view["result"]["current_items"]
        assert sorted(value["done"] for value in current.values()) == [10, 11]
        assert all(value["total"] is None for value in current.values())
        assert view["result"]["current_item"] in current.values()
        assert view["counts"] == {"running": 2} and view["phase"]["done"] == 0
        snapshot = next(view for view in queue.snapshot()["operations"] if view["id"] == ident)
        assert snapshot["result"]["current_items"] == current
        assert len(queue._byte_attempts) == 2
        with db.read() as session:
            assert not session.get(Operation, ident).result.get("current_items")
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert not queue._byte_attempts
    assert queue.details(ident)["counts"] == {"succeeded": 2}
    for context in contexts:
        context.report_bytes(999, 1000)
    assert not queue.details(ident)["result"]["current_items"]


@pytest.mark.parametrize("failure", ["sent_retry", "unsent_defer"])
def test_retry_has_independent_token_and_late_old_callback_cannot_replace_new(case, failure):
    db, queue = case
    contexts = []

    class Retryable(RuntimeError):
        retryable, retry_after, status = True, 60, 500
        failure_code = "image_http_status"

    class Handler:
        def execute(self, item, context):
            contexts.append(context)
            context.report_bytes(10 if len(contexts) == 1 else 20, 100)
            if len(contexts) == 1:
                if failure == "unsent_defer":
                    raise RequestDeferred(60)
                context.mark_remote_attempt()
                raise Retryable("synthetic remote failure")
            old = contexts[0]
            old.report_bytes(99, 100)
            queue._end_byte_attempt(old.item_id, old._byte_token)
            assert queue.details(context.operation_id)["result"]["current_item"]["done"] == 20
            assert len(queue._byte_attempts) == 1
            return ItemResult()

    ident = enqueue(db, queue, Handler())
    assert queue._step("download")
    assert queue.details(ident)["counts"] == {"pending": 1}
    assert not queue._byte_attempts
    assert not queue.details(ident)["result"]["current_items"]
    with db.write() as session:
        session.scalar(select(OperationItem).where(OperationItem.operation_id == ident)).not_before = None
    assert queue._step("download")
    assert contexts[0]._byte_token is not contexts[1]._byte_token
    assert queue.details(ident)["counts"] == {"succeeded": 1}
    assert not queue._byte_attempts


@pytest.mark.parametrize("control", ["pause", "stop", "failure", "defer"])
def test_all_exit_paths_clear_instant_bytes_and_keep_durable_outcome(case, control):
    db, queue = case
    contexts = []

    class Handler:
        def execute(self, item, context):
            contexts.append(context)
            context.report_bytes(50, 100)
            if control == "pause":
                queue.set_manual_paused(context.operation_id, True)
                assert not queue._byte_attempts
                assert "current_item" not in queue.details(context.operation_id)["result"]
                context.checkpoint()
            elif control == "stop":
                queue.request_stop()
                assert not queue._byte_attempts
                context.checkpoint()
            elif control == "defer":
                raise RequestDeferred(60)
            else:
                raise RuntimeError("synthetic failure")

    ident = enqueue(db, queue, Handler(), kind="reconcile" if control == "pause" else "repair")
    assert queue._step("download")
    assert not queue._byte_attempts
    view = queue.details(ident)
    assert not view["result"]["current_items"] and "current_item" not in view["result"]
    assert view["counts"] == ({"failed": 1} if control == "failure" else {"pending": 1})
    if control == "failure":
        assert view["state"] == "finished" and view["result"]["outcome"] == "failed"
    if control == "pause":
        queue.set_manual_paused(ident, False)
        contexts[0].report_bytes(99, 100)
        assert not queue._byte_attempts


@pytest.mark.parametrize("state", ["running", "finished"])
def test_restart_never_revives_legacy_durable_byte_snapshots(case, state):
    db, queue = case

    class Handler:
        def execute(self, item, context):
            context.report_bytes(20, 100)
            assert queue.details(context.operation_id)["result"]["current_item"]["done"] == 20
            return ItemResult()

    ident = enqueue(db, queue, Handler())
    with db.write() as session:
        operation = session.get(Operation, ident)
        item = session.scalar(select(OperationItem).where(OperationItem.operation_id == ident))
        stale = {"id": item.id, "unit": "bytes", "done": 100, "total": 100}
        item.state = "running" if state == "running" else "succeeded"
        operation.state = state
        operation.result = {**operation.result, "current_items": {item.id: stale}, "current_item": stale}
    restored = TaskQueue(db, queue.handlers)
    assert not restored.details(ident)["result"]["current_items"]
    assert "current_item" not in restored.details(ident)["result"]
    assert all("current_item" not in view["result"] for view in restored.snapshot()["operations"])
    restored.recover()
    assert not restored._byte_attempts
    if state == "running":
        queue = restored
        assert restored._step("download")
        assert restored.details(ident)["counts"] == {"succeeded": 1}
    assert not restored._byte_attempts


def test_byte_reports_never_borrow_database_and_merge_returns_read_connection_first(case, monkeypatch):
    db, queue = case
    original_read, original_merge = db.read, queue._merge_byte_progress
    depth, merged = threading.local(), []

    @contextmanager
    def tracked_read():
        depth.value = getattr(depth, "value", 0) + 1
        try:
            with original_read() as session:
                yield session
        finally:
            depth.value -= 1

    def merge(*args):
        assert getattr(depth, "value", 0) == 0
        merged.append(1)
        return original_merge(*args)

    monkeypatch.setattr(db, "read", tracked_read)
    monkeypatch.setattr(queue, "_merge_byte_progress", merge)

    class Handler:
        def execute(self, item, context):
            def forbidden():
                raise AssertionError("Byte reports must not borrow database connections")

            with monkeypatch.context() as patch:
                patch.setattr(db, "read", forbidden)
                patch.setattr(db, "write", forbidden)
                generation = queue.byte_progress_generation
                context.report_bytes(10, 100)
                assert queue.byte_progress_generation == generation + 1
                context.report_bytes(10, 100)
                assert queue.byte_progress_generation == generation + 1
            assert queue.details(context.operation_id)["result"]["current_item"]["done"] == 10
            assert next(view for view in queue.snapshot()["operations"]
                        if view["id"] == context.operation_id)["result"]["current_item"]["done"] == 10
            return ItemResult()

    enqueue(db, queue, Handler())
    assert queue._step("download")
    assert len(merged) == 2


@pytest.mark.parametrize("display_fault", [False, True])
def test_real_archive_download_keeps_verified_file_journal_and_settlement(case, tmp_path, monkeypatch, display_fault):
    db, queue = case
    root = tmp_path / "images"
    root.mkdir()
    commits, report_commits = [], []
    content = io.BytesIO()
    Image.new("RGB", (12, 10), "blue").save(content, "PNG")
    payload = content.getvalue()

    class Downloader:
        def download(self, url, destination, report):
            before = len(commits)
            for done in (10, 50, 100):
                report(done, 100)
                report_commits.append(len(commits) - before)
            destination.write_bytes(payload)

    queue.handlers["repair"] = ArchiveHandler("repair", db, Publisher(db, root), object(), Downloader())
    with db.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        session.flush()
        work = Work(id=1, pixiv_illust_id="101", artist_id=1, title="synthetic", type="illust", page_count=1)
        session.add_all([work, ArchiveIntent(pixiv_illust_id="101", mode="keep", revision=1)])
        session.flush()
        asset = Asset(work_id=1, role="image", page_index=0, state="missing", source_url="https://example.invalid/101.png")
        session.add(asset)
        session.flush()
        revision = ensure_legacy_revision(session, work)
        asset_id = asset.id
        ident = queue.enqueue_in_transaction(session, "repair", {}, "real synthetic file", specs=[
            ItemSpec("file", "download", {"work_id": 1, "asset_id": asset.id, "revision": 1,
                "work_revision_id": revision.id, "url": asset.source_url, "path": "synthetic/101.png"},
                "downloading", "assets")])["operation_id"]

    if display_fault:
        def fail(*args):
            raise RuntimeError("synthetic display failure")
        monkeypatch.setattr(queue, "_record_byte_progress", fail)

    def committed(connection):
        commits.append(1)

    event.listen(db.writer_engine, "commit", committed)
    try:
        assert queue._step("download")
    finally:
        event.remove(db.writer_engine, "commit", committed)
    assert report_commits == [0, 0, 0]
    assert (root / "synthetic/101.png").read_bytes() == payload
    with db.read() as session:
        assert session.get(Asset, asset_id).state == "verified"
        assert session.get(Asset, asset_id).sha256 and session.get(Asset, asset_id).byte_size == len(payload)
        assert session.scalar(select(PublicationJournal.step)) == "committed"
        assert session.scalar(select(OperationItem.state).where(OperationItem.operation_id == ident)) == "succeeded"
    assert queue.details(ident)["result"]["outcome"] == "succeeded"
    assert not queue._byte_attempts
