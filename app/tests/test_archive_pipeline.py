"""Real persisted queue and synthetic media; no remote account or library."""
import io
import shutil
import threading
import time

import pytest
from PIL import Image
from sqlalchemy import select

from src.core.archive import ArchiveHandler
from src.core.database import ArchiveDatabase
from src.core.media import Publisher
from src.core.models import ArchiveIntent, Artist, Asset, Derivative, Operation, OperationItem, TrackingPolicy, Work
from src.core.tasks import ItemResult, ItemSpec, TaskQueue


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (12, 10), color).save(stream, format="PNG")
    return stream.getvalue()


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "synthetic queue did not reach expected state"


@pytest.fixture(scope="module")
def database_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("pipeline-schema") / "template.db"
    db = ArchiveDatabase.create(path)
    db.close()
    return path


@pytest.fixture
def archive(tmp_path, database_template):
    path = tmp_path / "archive.db"
    shutil.copyfile(database_template, path)
    db = ArchiveDatabase(path)
    root = tmp_path / "images"
    root.mkdir()
    with db.write() as session:
        for index in (1, 2):
            artist = Artist(id=index, pixiv_user_id=str(index), name=str(index))
            artist.policy = TrackingPolicy(mode="active")
            session.add(artist)
    yield db, root
    db.close()


class Downloader:
    def __init__(self):
        self.calls = []
        self.held = threading.Event()
        self.release = threading.Event()
        self.hold_url = None
        self.error = None

    def download(self, url, destination, report=None):
        self.calls.append(url)
        if url == self.hold_url:
            if report:
                report(10, 100)
            self.held.set()
            assert self.release.wait(15)
        if self.error:
            raise self.error
        destination.write_bytes(png())


class Client:
    def __init__(self, pages=1):
        self.pages = pages
        self.held = threading.Event()
        self.release = threading.Event()

    def get_all_artist_illusts(self, remote_id):
        if remote_id == "2":
            self.held.set()
            assert self.release.wait(15)
            return
        yield {"illust_id": "100", "title": "new", "type": "manga", "page_count": self.pages,
               "urls": [{"original": f"https://i.pximg.net/100_p{i}.png"} for i in range(self.pages)]}


def queue_for(db, root, client=None, downloader=None):
    publisher = Publisher(db, root)
    return TaskQueue(db, {kind: ArchiveHandler(kind, db, publisher, client or object(), downloader or Downloader())
                         for kind in ("check", "repair", "verify", "thumbnails", "backfill", "metadata")})


def saved_file(db, root, pixiv_id, *, kind=None):
    from src.core.revisions import selected_revision, revision_assets, revision_derivatives
    with db.read() as session:
        work = session.scalar(select(Work).where(Work.pixiv_illust_id == pixiv_id))
        if work is None:
            return None
        revision = selected_revision(session, work.id)
        records = revision_derivatives(session, revision) if kind else revision_assets(session, revision)
        record = next((item for item in records if (item.kind == kind if kind else item.role == "image" and item.page_index == 0)
            and item.relative_path), None)
        path = root / record.relative_path if record else None
        return path if path and path.is_file() else None


def test_first_work_publishes_while_second_artist_is_blocked_and_pause_keeps_children(archive):
    db, root = archive
    client, downloader = Client(), Downloader()
    queue = queue_for(db, root, client, downloader)
    parent = queue.submit("check", {}, "automatic", source="automatic")["operation_id"]
    queue.start()
    try:
        assert client.held.wait(15)
        queue.set_automatic_paused(True)
        queue.submit("check", {"artist_ids": [1]}, "next automatic", source="automatic")
        wait_for(lambda: saved_file(db, root, "100") is not None)
        assert queue.details(parent)["state"] in ("ready", "running", "planning")
        with db.read() as session:
            child = session.get(Operation, session.get(Operation, parent).result["child_operation_ids"][0])
            assert child.result["parent_operation_ids"] == [parent]
            targets = list(session.scalars(select(OperationItem.target)))
            assert all(target.get("work_id") for target in targets if target.get("asset_id"))
        client.release.set()
        wait_for(lambda: queue.details(parent)["state"] == "finished")
        assert queue.details(parent)["result"]["outcome"] == "succeeded"
        assert queue.snapshot()["next_round_count"] == 1
    finally:
        client.release.set()
        assert queue.stop(15)


def test_two_downloads_and_local_progress_do_not_hide_held_download(archive):
    db, root = archive
    client, downloader = Client(pages=2), Downloader()
    downloader.hold_url = "https://i.pximg.net/100_p1.png"
    queue = queue_for(db, root, client, downloader)
    parent = queue.submit("check", {"artist_ids": [1]}, "new pages")["operation_id"]
    queue.start()
    try:
        assert downloader.held.wait(15)
        wait_for(lambda: saved_file(db, root, "100", kind="thumbnail") is not None)
        with db.read() as session:
            child_id = session.get(Operation, parent).result["child_operation_ids"][0]
        current = queue.details(child_id)
        assert current["state"] == "running" and current["lane"] == "download"
        assert current["phase"]["name"] == "downloading"
        assert current["phase"]["done"] == 1 and current["phase"]["total"] == 2
        assert current["result"]["current_item"]["done"] == 10
        downloader.release.set()
        wait_for(lambda: queue.details(parent)["state"] == "finished")
    finally:
        downloader.release.set()
        assert queue.stop(15)


def test_daily_check_does_not_stat_hundred_verified_originals(archive, monkeypatch):
    db, root = archive
    with db.write() as session:
        work = Work(id=10, pixiv_illust_id="10", artist_id=1, title="established", type="manga", page_count=100)
        work.assets = [Asset(role="image", page_index=page, relative_path=f"old/{page}.png", state="verified") for page in range(100)]
        work.derivatives = [Derivative(kind="thumbnail", relative_path="old/thumb.webp", state="verified")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="10")])
    original_stat = __import__("pathlib").Path.stat

    def guarded_stat(path, *args, **kwargs):
        assert "old" not in path.parts, "daily discovery touched an established original"
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(__import__("pathlib").Path, "stat", guarded_stat)
    client, downloader = Client(pages=2), Downloader()
    queue = queue_for(db, root, client, downloader)
    queue.submit("check", {"artist_ids": [1]}, "daily")
    assert queue.run_round()
    assert len(downloader.calls) == 2


def test_explicit_verify_counts_all_originals_and_missing_gif(archive):
    db, root = archive
    with db.write() as session:
        work = Work(id=1, pixiv_illust_id="10", artist_id=1, title="verify", type="manga", page_count=100)
        for page in range(100):
            (root / f"{page}.png").write_bytes(png())
        work.assets = [Asset(role="image", page_index=page, relative_path=f"{page}.png", state="verified") for page in range(100)]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="10")])
        gif = Work(id=2, pixiv_illust_id="20", artist_id=1, title="missing gif", type="ugoira",
                   source_metadata={"original_backfill_requires_selection": True})
        gif.assets = [Asset(role="ugoira_zip", state="missing_deferred"), Asset(role="ugoira_timing", state="missing")]
        session.add_all([gif, ArchiveIntent(pixiv_illust_id="20")])
    queue = queue_for(db, root)
    first = queue.submit("verify", {"work_ids": [1]}, "verify 100")["operation_id"]
    queue.run_round()
    assert queue.details(first)["counts"] == {"succeeded": 100}
    second = queue.submit("verify", {"work_ids": [2]}, "verify missing gif")["operation_id"]
    queue.run_round()
    assert queue.details(second)["result"]["outcome"] == "failed"
    assert any(item["key"].startswith("verify-gif") and item["state"] == "failed" for item in queue.details(second)["items"])


def test_gate_prevents_ready_claim_and_manual_priority_is_per_file(archive):
    db, root = archive
    executed = []

    class Handler:
        def plan(self, parameters, context):
            return []

        def execute(self, item, context):
            executed.append(item.key)
            if item.key == "auto-0":
                with db.write() as session:
                    queue.enqueue_in_transaction(session, "repair", {"manual": True}, "manual", specs=[ItemSpec("manual", "download", {}, "downloading")])
            return ItemResult()

    queue = TaskQueue(db, {"repair": Handler()})
    with db.write() as session:
        queue.enqueue_in_transaction(session, "repair", {}, "automatic", source="maintenance",
            specs=[ItemSpec(f"auto-{index}", "download", {}, "downloading") for index in range(3)])
    queue.accept_submissions(False)
    assert not queue._step("download") and not executed
    queue.accept_submissions(True)
    assert queue._step("download")
    assert queue._step("download")
    assert executed == ["auto-0", "manual"]


def test_same_asset_serializes_and_failure_in_child_reaches_parent(archive):
    db, root = archive
    held, release = threading.Event(), threading.Event()
    executions = []

    class Handler:
        def plan(self, parameters, context):
            return []

        def execute(self, item, context):
            executions.append(item.key)
            held.set()
            assert release.wait(15)
            return ItemResult("failed", {"message": "synthetic failure"})

    queue = TaskQueue(db, {"repair": Handler(), "check": Handler()})
    parent = queue.submit("check", {}, "parent")["operation_id"]
    with db.write() as session:
        queue.enqueue_in_transaction(session, "repair", {"first": True}, "first", parent_id=parent,
            specs=[ItemSpec("first", "download", {"asset_id": "same", "work_id": 1}, "downloading")])
        queue.enqueue_in_transaction(session, "repair", {"second": True}, "second",
            specs=[ItemSpec("second", "download", {"asset_id": "same", "work_id": 1}, "downloading")])
    queue.start()
    try:
        assert held.wait(15)
        time.sleep(0.1)
        assert executions == ["first"]
        release.set()
        wait_for(lambda: queue.details(parent)["state"] == "finished")
        assert queue.details(parent)["result"]["outcome"] == "failed"
    finally:
        release.set()
        assert queue.stop(15)


def test_source_update_failure_retains_verified_old_file_and_legacy_pending(archive):
    db, root = archive
    (root / "old.png").write_bytes(png())
    with db.write() as session:
        work = Work(id=1, pixiv_illust_id="100", artist_id=1, title="old", type="illust")
        asset = Asset(role="image", state="verified", relative_path="old.png", source_url="https://i.pximg.net/new.png")
        work.assets = [asset]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])
        session.flush()
        asset_id = asset.id
        work.source_metadata = {"source_updates": {asset_id: {"requested_url": asset.source_url, "reason": "source update"}}}
    downloader = Downloader()
    downloader.error = ConnectionError("synthetic")
    queue = queue_for(db, root, downloader=downloader)
    failed = queue.submit("repair", {"work_ids": [1]}, "update")["operation_id"]
    queue.run_round()
    with db.read() as session:
        asset = session.get(Asset, asset_id)
        assert asset.state == "verified" and asset.relative_path == "old.png"
        from src.core.revisions import latest_revision, revision_assets
        current = revision_assets(session, latest_revision(session, 1))[0]
        replacement_id = current.id
        assert replacement_id != asset_id and current.state == "failed"
        assert current.source_url.endswith("new.png") and current.error
        assert asset_id not in session.get(Work, 1).source_metadata.get("source_updates", {})
    assert queue.details(failed)["result"]["outcome"] == "failed"
    assert (root / "old.png").read_bytes() == png()
    downloader.error = None
    queue.retry(failed)
    queue.run_round()
    with db.read() as session:
        assert session.get(Asset, asset_id).state == "verified"
        assert session.get(Asset, replacement_id).state == "verified"
        assert not session.get(Work, 1).source_metadata.get("source_updates", {})
    assert (root / "old.png").read_bytes() == png()
    assert downloader.calls == ["https://i.pximg.net/new.png"] * 2


@pytest.mark.parametrize("zero", [False, True])
def test_requested_absent_or_empty_file_records_gap_and_repairs(archive, zero):
    db, root = archive
    if zero:
        (root / "original.png").write_bytes(b"")
    with db.write() as session:
        work = Work(id=1, pixiv_illust_id="100", artist_id=1, title="requested", type="illust")
        work.assets = [Asset(role="image", state="verified", relative_path="original.png", source_url="https://i.pximg.net/original.png")]
        work.derivatives = [Derivative(kind="thumbnail", state="verified", relative_path="thumb.webp")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])
    from src.core.runtime import Runtime
    downloader = Downloader()
    runtime = Runtime(db.path, root, client=object(), downloader=downloader)
    try:
        assert runtime.media_path("original.png") is None
        with runtime.database.read() as session:
            assert session.scalar(select(Asset.state)) == ("invalid" if zero else "missing")
        runtime.start()
        wait_for(lambda: bool(runtime.media_path("original.png")))
        assert downloader.calls == ["https://i.pximg.net/original.png"]
    finally:
        assert runtime.close()


def test_stale_source_response_cannot_replace_or_clear_newer_source(archive):
    db, root = archive
    (root / "old.png").write_bytes(png())
    with db.write() as session:
        work = Work(id=1, pixiv_illust_id="100", artist_id=1, title="changing", type="illust")
        asset = Asset(role="image", state="verified", relative_path="old.png", source_url="https://i.pximg.net/v2.png")
        work.assets = [asset]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])
        session.flush()
        asset_id = asset.id
        work.source_metadata = {"source_updates": {asset_id: {"requested_url": asset.source_url, "reason": "source update"}}}
    downloader = Downloader()
    downloader.hold_url = "https://i.pximg.net/v2.png"
    queue = queue_for(db, root, downloader=downloader)
    operation_id = queue.submit("repair", {"work_ids": [1]}, "changing")["operation_id"]
    queue.start()
    try:
        assert downloader.held.wait(15)
        with db.write() as session:
            from src.core.revisions import latest_revision, revision_assets
            asset = revision_assets(session, latest_revision(session, 1))[0]
            current_asset_id = asset.id
            assert current_asset_id != asset_id
            asset.source_url = "https://i.pximg.net/v3.png"
            session.get(Work, 1).source_metadata = {"source_updates": {current_asset_id: {"requested_url": asset.source_url, "reason": "newer update"}}}
        downloader.release.set()
        wait_for(lambda: queue.details(operation_id)["state"] == "finished")
        with db.read() as session:
            assert session.get(Work, 1).source_metadata["source_updates"][current_asset_id]["requested_url"].endswith("v3.png")
            assert session.get(Asset, asset_id).state == "verified"
            assert session.get(Asset, current_asset_id).state != "verified"
        assert (root / "old.png").read_bytes() == png()
        assert queue.details(operation_id)["counts"]["skipped"] >= 1
    finally:
        downloader.release.set()
        assert queue.stop(15)


def test_local_claim_skips_two_thousand_waiting_dependencies_in_sql(archive):
    db, root = archive
    executions = []

    class Handler:
        def plan(self, parameters, context):
            return []

        def execute(self, item, context):
            executions.append(item.key)
            return ItemResult()

    queue = TaskQueue(db, {"repair": Handler()})
    specs = [ItemSpec("download", "download", {}, "downloading")]
    specs += [ItemSpec(f"waiting-{index}", "thumbnail", {}, "thumbnails", depends_on=("download",)) for index in range(2000)]
    specs.append(ItemSpec("runnable", "thumbnail", {}, "thumbnails"))
    with db.write() as session:
        queue.enqueue_in_transaction(session, "repair", {}, "many dependencies", specs=specs)
    from sqlalchemy import event
    queries, vm_steps = [], [0]

    def progress():
        vm_steps[0] += 1000
        return 0

    def observe(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement)
        connection.connection.driver_connection.set_progress_handler(progress, 1000)

    event.listen(db.engine, "before_cursor_execute", observe)
    try:
        assert queue._step("local")
    finally:
        event.remove(db.engine, "before_cursor_execute", observe)
        with db.engine.connect() as connection:
            connection.connection.driver_connection.set_progress_handler(None, 0)
    assert executions == ["runnable"]
    with db.read() as session:
        waiting = list(session.scalars(select(OperationItem).where(OperationItem.item_key.like("waiting-%"))))
        assert len(waiting) == 2000
        assert all(item.state == "pending" and item.attempts == 0 for item in waiting)
    candidate_queries = [statement for statement in queries if "operation_items.position >" in statement]
    assert 1 <= len(candidate_queries) <= 64
    assert all("LIMIT" in statement and "operation_items.target" not in statement for statement in candidate_queries)
    assert vm_steps[0] < 1_000_000


def test_newer_discovery_has_durable_child_while_older_download_is_held(archive):
    db, root = archive
    first_discovered, newer_discovered = threading.Event(), threading.Event()
    second_allowed = threading.Event()

    class ChangingClient:
        def get_all_artist_illusts(self, remote_id):
            if remote_id == "1":
                yield {"illust_id": "100", "title": "changing", "type": "illust", "page_count": 1,
                       "urls": [{"original": "https://i.pximg.net/a.png"}]}
                first_discovered.set()
                assert second_allowed.wait(15)
                yield {"illust_id": "100", "title": "changing", "type": "illust", "page_count": 1,
                       "urls": [{"original": "https://i.pximg.net/b.png"}]}
                newer_discovered.set()

    downloader = Downloader()
    downloader.hold_url = "https://i.pximg.net/a.png"
    queue = queue_for(db, root, ChangingClient(), downloader)
    parent = queue.submit("check", {"artist_ids": [1]}, "source progression")["operation_id"]
    queue.start()
    try:
        assert downloader.held.wait(15) and first_discovered.wait(15)
        second_allowed.set()
        assert newer_discovered.wait(15)
        with db.read() as session:
            children = session.get(Operation, parent).result["child_operation_ids"]
            assert len(children) == 2
            assert len({session.get(Operation, child).parameters["asset_requests"][0]["requested_url"] for child in children}) == 2
        downloader.release.set()
        wait_for(lambda: queue.details(parent)["state"] == "finished")
        with db.read() as session:
            work = session.scalar(select(Work).where(Work.pixiv_illust_id == "100"))
            from src.core.revisions import latest_revision, revision_assets
            assert not work.source_metadata.get("source_updates")
            asset = revision_assets(session, latest_revision(session, work.id))[0]
            assert asset.state == "verified" and asset.source_url.endswith("b.png")
        assert downloader.calls == ["https://i.pximg.net/a.png", "https://i.pximg.net/b.png"]
    finally:
        second_allowed.set()
        downloader.release.set()
        assert queue.stop(15)


def test_pending_source_rotation_reaches_older_work_behind_attention_window(archive):
    db, root = archive
    from src.core.maintenance import MissingMediaMaintenance, LEDGER_PREFIX
    from src.core.models import ApplicationState
    with db.write() as session:
        for index in range(1, 130):
            work = Work(id=index, pixiv_illust_id=str(1000 + index), artist_id=1, title=str(index), type="illust")
            asset = Asset(role="image", state="verified", source_url=f"https://i.pximg.net/{index}.png")
            work.assets = [asset]
            work.derivatives = [Derivative(kind="thumbnail", state="verified")]
            session.add_all([work, ArchiveIntent(pixiv_illust_id=str(1000 + index))])
            session.flush()
            work.source_metadata = {"source_updates": {asset.id: {"requested_url": asset.source_url, "reason": "source changed"}}}
            if index != 1:
                session.add(ApplicationState(key=LEDGER_PREFIX + str(index), value={"status": "attention", "tracks": {"remote": {"status": "attention"}}}))
    queue = queue_for(db, root)
    maintenance = MissingMediaMaintenance(db, queue, remote_available=lambda: True, monotonic=lambda: 0)
    assert maintenance.tick(force=True) == []
    queued = maintenance.tick(force=True)
    assert len(queued) == 1
    with db.read() as session:
        assert session.get(Operation, queued[0]["operation_id"]).parameters["work_ids"] == [1]


def test_maintenance_source_refresh_rebuilds_first_page_thumbnail(archive):
    db, root = archive
    (root / "old.png").write_bytes(png("blue"))
    with db.write() as session:
        work = Work(id=1, pixiv_illust_id="100", artist_id=1, title="source preview", type="illust")
        work.assets = [Asset(role="image", state="unknown", relative_path="old.png", source_url="https://i.pximg.net/old.png")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])
    queue = queue_for(db, root)
    queue.submit("repair", {"work_ids": [1]}, "establish")
    queue.run_round()
    thumbnail_path = saved_file(db, root, "100", kind="thumbnail")
    assert thumbnail_path is not None
    old_thumbnail = thumbnail_path.read_bytes()
    with db.write() as session:
        asset = session.scalar(select(Asset))
        asset.source_url = "https://i.pximg.net/new.png"
        session.get(Work, 1).source_metadata = {"source_updates": {asset.id: {"requested_url": asset.source_url, "reason": "source changed"}}}
    result = queue.submit("repair", {"work_ids": [1]}, "maintenance refresh", source="maintenance")
    queue.run_round()
    current_thumbnail = saved_file(db, root, "100", kind="thumbnail")
    assert current_thumbnail is not None
    assert current_thumbnail.read_bytes() != old_thumbnail
    assert thumbnail_path.read_bytes() == old_thumbnail
    with db.read() as session:
        from src.core.revisions import latest_revision, revision_assets, revision_derivatives
        revision = latest_revision(session, 1)
        asset = revision_assets(session, revision)[0]
        thumbnail = next(record for record in revision_derivatives(session, revision) if record.kind == "thumbnail")
        assert thumbnail.source_version == asset.sha256
        assert not session.get(Work, 1).source_metadata["source_updates"]
    assert queue.details(result["operation_id"])["counts"] == {"succeeded": 2}


def test_missing_read_waiting_for_writer_cannot_undo_new_publication(archive, monkeypatch):
    db, root = archive
    from contextlib import contextmanager
    from src.core.runtime import Runtime
    from src.core.catalogue_revision import catalogue_revision
    from src.core.media import verify_media
    with db.write() as session:
        work = Work(id=1, pixiv_illust_id="100", artist_id=1, title="read race", type="illust")
        asset = Asset(role="image", state="verified", relative_path="original.png")
        work.assets = [asset]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])
        session.flush()
        asset_id = asset.id
    runtime = Runtime(db.path, root)
    observed, allow_write = threading.Event(), threading.Event()
    original_write = runtime.database.write

    @contextmanager
    def delayed_write():
        observed.set()
        assert allow_write.wait(15)
        with original_write() as session:
            yield session

    monkeypatch.setattr(runtime.database, "write", delayed_write)
    result = []
    reader = threading.Thread(target=lambda: result.append(runtime.media_path("original.png")))
    reader.start()
    try:
        assert observed.wait(15)
        publisher = Publisher(db, root)
        relative, staged = publisher.staging()
        staged.write_bytes(png())
        journal = publisher.prepare(asset_id, relative, "original.png", verify_media(staged, "image"), expected_revision=1)
        assert publisher.publish(journal) == "committed"
        with db.read() as session:
            revision = catalogue_revision(session)
        allow_write.set()
        reader.join(15)
        assert result == [root / "original.png"]
        with db.read() as session:
            assert session.get(Asset, asset_id).state == "verified"
            assert catalogue_revision(session) == revision
    finally:
        allow_write.set()
        reader.join(15)
        assert runtime.close()


def test_manual_repair_streams_first_work_while_second_work_planning_is_held(archive, monkeypatch):
    db, root = archive
    with db.write() as session:
        for index in (1, 2):
            work = Work(id=index, pixiv_illust_id=str(100 + index), artist_id=1, title=str(index), type="illust")
            work.assets = [Asset(role="image", state="missing", source_url=f"https://i.pximg.net/{index}.png")]
            session.add_all([work, ArchiveIntent(pixiv_illust_id=str(100 + index))])
    queue = queue_for(db, root)
    original_plan = queue.handlers["repair"]._plan_work
    held, release = threading.Event(), threading.Event()

    def held_plan(work_id, parameters, context):
        if work_id == 2:
            held.set()
            assert release.wait(15)
        return original_plan(work_id, parameters, context)

    monkeypatch.setattr(queue.handlers["repair"], "_plan_work", held_plan)
    operation_id = queue.submit("repair", {"work_ids": [1, 2]}, "manual streamed repair")["operation_id"]
    queue.start()
    try:
        assert held.wait(15)
        wait_for(lambda: saved_file(db, root, "101") is not None)
        assert queue.details(operation_id)["state"] != "finished"
        assert queue.snapshot()["round"]["total"] is None
        release.set()
        wait_for(lambda: queue.details(operation_id)["state"] == "finished")
        assert queue.details(operation_id)["result"]["outcome"] == "succeeded"
    finally:
        release.set()
        assert queue.stop(15)


def test_established_ugoira_check_does_not_refetch_timing_or_touch_originals(archive, monkeypatch):
    db, root = archive
    with db.write() as session:
        work = Work(id=1, pixiv_illust_id="100", artist_id=1, title="established animation", type="ugoira", source_metadata={"urls": []})
        work.assets = [Asset(role="ugoira_zip", state="verified", relative_path="old/100.zip"), Asset(role="ugoira_timing", state="verified", relative_path="old/100.frames.json")]
        work.derivatives = [Derivative(kind="gif", state="verified", relative_path="old/100.gif", sha256="a" * 64),
                            Derivative(kind="thumbnail", state="verified", relative_path="old/thumb.webp"),
                            Derivative(kind="thumbnail", spec_key="card-motion-v1", state="verified",
                                       relative_path="old/card.webp", source_version="a" * 64)]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])

    class EstablishedClient:
        def get_all_artist_illusts(self, remote_id):
            yield {"illust_id": "100", "title": "established animation", "type": "ugoira", "page_count": 1, "urls": []}

        def get_ugoira_metadata(self, remote_id):
            pytest.fail("established animation refetched timing")

    original_stat = __import__("pathlib").Path.stat

    def guarded_stat(path, *args, **kwargs):
        assert "old" not in path.parts
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(__import__("pathlib").Path, "stat", guarded_stat)
    queue = queue_for(db, root, EstablishedClient())
    operation_id = queue.submit("check", {"artist_ids": [1]}, "established animation")["operation_id"]
    queue.run_round()
    assert queue.details(operation_id)["result"]["outcome"] == "succeeded"


def test_legacy_timing_source_update_clears_only_after_new_frames_publish(archive):
    db, root = archive
    import json
    import zipfile
    with zipfile.ZipFile(root / "original.zip", "w") as stream:
        stream.writestr("first.png", png())
    (root / "original.frames.json").write_text(json.dumps({"frames": [{"file": "first.png", "delay": 100}]}), encoding="utf-8")
    with db.write() as session:
        work = Work(id=1, pixiv_illust_id="100", artist_id=1, title="timing update", type="ugoira")
        work.assets = [Asset(role="ugoira_zip", state="unknown", relative_path="original.zip", source_url="https://i.pximg.net/original.zip"), Asset(role="ugoira_timing", state="source_changed", relative_path="original.frames.json")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])

    class TimingClient:
        def get_ugoira_metadata(self, remote_id):
            return {"zip_urls": {"original": "https://i.pximg.net/original.zip"}, "frames": [{"file": "first.png", "delay": 200}]}

    class ZipDownloader(Downloader):
        def download(self, url, destination, report=None):
            self.calls.append(url)
            destination.write_bytes((root / "original.zip").read_bytes() if url.endswith(".zip") else png())
    old_timing = (root / "original.frames.json").read_bytes()
    queue = queue_for(db, root, TimingClient(), ZipDownloader())
    operation_id = queue.submit("repair", {"work_ids": [1]}, "timing update")["operation_id"]
    queue.run_round()
    assert queue.details(operation_id)["result"]["outcome"] == "succeeded"
    assert (root / "original.frames.json").read_bytes() == old_timing
    with db.read() as session:
        from src.core.revisions import latest_revision, revision_assets
        assets = revision_assets(session, latest_revision(session, 1))
        current_timing = next(asset for asset in assets if asset.role == "ugoira_timing")
        assert json.loads((root / current_timing.relative_path).read_text(encoding="utf-8"))["frames"][0]["delay"] == 200
        assert not session.get(Work, 1).source_metadata.get("source_updates")
        assert all(asset.state == "verified" for asset in assets)


def test_parent_completion_gate_does_not_materialize_five_thousand_children(archive):
    db, root = archive
    queue = queue_for(db, root)
    with db.write() as session:
        parent = Operation(kind="check", parameters={}, label="large parent", dedupe_key="parent", state="ready")
        session.add(parent)
        session.flush()
        children = [Operation(id=f"child-{index}", kind="repair", parameters={}, label="child", dedupe_key=f"child-{index}", state="ready") for index in range(5000)]
        session.add_all(children)
        parent.result = {"child_operation_ids": [child.id for child in children]}
        parent_id = parent.id
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    queries, entities = [], []

    def query_observer(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    def entity_observer(session, instance):
        if isinstance(instance, Operation):
            entities.append(instance.id)

    event.listen(db.writer_engine, "before_cursor_execute", query_observer)
    event.listen(Session, "loaded_as_persistent", entity_observer)
    try:
        with db.write() as session:
            queue._finalize(session, [parent_id])
    finally:
        event.remove(db.writer_engine, "before_cursor_execute", query_observer)
        event.remove(Session, "loaded_as_persistent", entity_observer)
    assert entities == [parent_id]
    assert len(queries) < 10
    assert any("json_each" in statement and "LIMIT" in statement for statement in queries)
