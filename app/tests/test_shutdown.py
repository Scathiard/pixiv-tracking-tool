"""Shutdown regression tests: synthetic workers and libraries only."""
from types import SimpleNamespace
from threading import Event
from uuid import uuid4
import asyncio
import time

import pytest


@pytest.mark.parametrize("busy,maintenance", [(False, False), (True, False), (False, True)])
def test_tray_quit_accepts_pending_or_paused_work(monkeypatch, busy, maintenance):
    from src import tray, web, progress

    calls = []
    monkeypatch.setattr(web, "get_tracker", lambda: SimpleNamespace(busy=lambda: busy))
    monkeypatch.setattr(web, "request_shutdown", lambda: calls.append("shutdown"))
    monkeypatch.setattr(progress, "maintenance_active", lambda: maintenance)
    icon = SimpleNamespace(stop=lambda: calls.append("stop"), notify=lambda *args: None)
    tray._quit_app(icon)
    assert calls == ["shutdown", "stop"]


@pytest.mark.parametrize("phase,kind", [("checking", "discover_work"), ("downloading", "download"), ("converting", "gif")])
def test_stop_keeps_current_safe_result_and_unclaimed_items(tmp_path, phase, kind):
    from src.core.runtime import Runtime
    from src.core.models import Operation, OperationItem
    from src.core.ownership import LibraryInUse, LibraryLease
    from src.core.tasks import ItemResult, ItemSpec
    from sqlalchemy import select

    entered, release = Event(), Event()
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True, client=object())
    runtime.maintenance.tick = lambda: None

    class Held:
        def plan(self, parameters, context):
            return [ItemSpec(str(index), kind, {}, phase, depends_on=() if index == 0 else ("0",)) for index in range(3)]

        def execute(self, item, context):
            entered.set()
            assert release.wait(3)
            # Represents a write/publication that already entered its critical
            # section: shutdown must let it commit and count as success.
            return ItemResult()

    runtime.queue.handlers["repair"] = Held()
    operation = runtime.queue.submit("repair", {}, "held")["operation_id"]
    runtime.start()
    try:
        assert entered.wait(3)
        assert runtime.close(timeout=.02) is False
        assert runtime.database._closed is False
        with pytest.raises(LibraryInUse):
            LibraryLease(tmp_path / "images").acquire()
        release.set()
        assert runtime.close(timeout=None)
        assert runtime.close()
        recovered = Runtime(tmp_path / "archive.db", tmp_path / "images", client=object())
        recovered.maintenance.tick = lambda: None
        resumed = []

        class Resume:
            def plan(self, parameters, context):
                raise AssertionError("finished inventory must not be planned again")

            def execute(self, item, context):
                resumed.append(item.key)
                return ItemResult()

        recovered.queue.handlers["repair"] = Resume()
        try:
            recovered.queue.recover()
            with recovered.database.read() as session:
                items = list(session.scalars(select(OperationItem).where(OperationItem.operation_id == operation).order_by(OperationItem.position)))
                assert [item.state for item in items] == ["succeeded", "pending", "pending"]
                assert session.get(Operation, operation).state != "finished"
            assert recovered.queue.run_round()
            assert resumed == ["1", "2"]
            assert recovered.queue.details(operation)["state"] == "finished"
        finally:
            recovered.close()
    finally:
        release.set()
        runtime.close(timeout=None)


@pytest.mark.parametrize("planning", [True, False])
def test_cooperative_stop_preserves_pending_without_failure(tmp_path, planning):
    from src.core.database import ArchiveDatabase
    from src.core.models import Operation, OperationItem
    from src.core.tasks import ItemResult, ItemSpec, TaskQueue
    from sqlalchemy import select

    db = ArchiveDatabase.create(tmp_path / "archive.db")

    class Interrupted:
        def plan(self, parameters, context):
            if planning:
                context.queue.request_stop()
                context.report("checking", 0, 2, "works", "stopping")
            return [ItemSpec("0", "download", {}, "downloading")]

        def execute(self, item, context):
            context.queue.request_stop()
            context.report_bytes(1, 100)
            return ItemResult()

    queue = TaskQueue(db, {"repair": Interrupted()})
    op = queue.submit("repair", {}, "interrupt")["operation_id"]
    try:
        assert queue.run_round() is False
        with db.read() as session:
            operation = session.get(Operation, op)
            assert operation.state == "interrupted"
            assert operation.needs_attention is False
            assert "outcome" not in operation.result
            items = list(session.scalars(select(OperationItem).where(OperationItem.operation_id == op)))
            assert [item.state for item in items] == ([] if planning else ["pending"])
        queue.recover()
        with db.read() as session:
            assert session.get(Operation, op).state in ("planning", "ready")
    finally:
        queue.stop()
        db.close()


def test_bookmark_exit_keeps_write_and_queued_intents_for_readback(tmp_path):
    from src.core.database import ArchiveDatabase
    from src.core.bookmarks import BookmarkService
    from src.core.models import Artist, BookmarkOperation, Work

    db = ArchiveDatabase.create(tmp_path / "archive.db")
    with db.write() as session:
        artist = Artist(id=1, pixiv_user_id="1", name="synthetic")
        session.add_all([Work(id=index, pixiv_illust_id=str(index), artist=artist, title="test") for index in (2, 3)])
    entered, release = Event(), Event()
    writes, reads = [], []

    class Remote:
        def get_account_id(self):
            return "123"

        def write_bookmark(self, pixiv_id, desired, account):
            writes.append(pixiv_id)
            entered.set()
            assert release.wait(3)

        def read_bookmark_bound(self, pixiv_id, account):
            reads.append(pixiv_id)
            return True

    service = BookmarkService(db, Remote(), max_workers=1)
    service.start()
    try:
        first = service.submit(2, True, uuid4().hex)["operation_id"]
        assert entered.wait(3)
        second = service.submit(3, True, uuid4().hex)["operation_id"]
        service.request_stop()
        assert service.stop(timeout=.01) is False
        release.set()
        assert service.stop(timeout=3)
        assert writes == ["2"] and reads == []
        with db.read() as session:
            assert [session.get(BookmarkOperation, op).status for op in (first, second)] == ["pending", "pending"]
        restarted = BookmarkService(db, Remote(), max_workers=1)
        restarted.start()
        assert restarted.stop(timeout=3)
        assert writes == ["2"]
        with db.read() as session:
            assert [session.get(BookmarkOperation, op).status for op in (first, second)] == ["confirmed", "confirmed"]
    finally:
        release.set()
        service.stop(timeout=3)
        db.close()


def test_lifespan_waits_for_auth_and_real_close(tmp_path, monkeypatch):
    from src import auth, web
    from src.core.runtime import Runtime

    entered, release = Event(), Event()
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    runtime.maintenance.tick = lambda: None
    monkeypatch.setattr(web, "_shutdown_requested", Event())

    def held_auth():
        entered.set()
        assert release.wait(3)
        return True

    monkeypatch.setattr(auth, "_try_refresh_token", held_auth)
    monkeypatch.setattr(web, "cancel_auth", release.set)
    application = web.create_app(lambda: runtime, schedule=False)

    async def run():
        async with application.router.lifespan_context(application):
            while not entered.is_set():
                await asyncio.sleep(.005)
            assert runtime.database._closed is False
        assert application.state.auth_task.done()
        assert runtime.database._closed
        assert application.state.runtime is None
        assert runtime.client is None

    asyncio.run(run())


def test_repeated_shutdown_during_startup_is_remembered(monkeypatch):
    from src import web

    event = Event()
    calls = []
    state = SimpleNamespace(runtime=None)
    server = SimpleNamespace(should_exit=False, config=SimpleNamespace(app=SimpleNamespace(state=state)))
    monkeypatch.setattr(web, "_shutdown_requested", event)
    monkeypatch.setattr(web, "_server", server)
    monkeypatch.setattr(web, "cancel_auth", lambda: calls.append("cancel"))
    web.request_shutdown()
    web.request_shutdown()
    assert event.is_set() and server.should_exit and state.stopping
    state.runtime = SimpleNamespace(request_stop=lambda **kw: calls.append(kw))
    web.request_shutdown()
    assert calls[-1] == {"cancel_remote": True}


def test_closing_replaced_runtime_does_not_cancel_shared_client(tmp_path):
    from src.core.runtime import Runtime

    calls = []
    client = SimpleNamespace(request_stop=lambda: calls.append("stop"))
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True, client=client)
    assert runtime.close()
    assert calls == []


def test_worker_stop_at_maintenance_boundary_is_clean(tmp_path, monkeypatch):
    import threading
    from src.core.runtime import Runtime

    entered, release = Event(), Event()
    errors = []
    monkeypatch.setattr(threading, "excepthook", errors.append)
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True,
                      client=SimpleNamespace(get_account_id=lambda: None))

    def held_maintenance():
        entered.set()
        assert release.wait(3)
        runtime.remote.get_account_id()

    runtime.maintenance.tick = held_maintenance
    try:
        runtime.start()
        assert entered.wait(3)
        runtime.request_stop()
        release.set()
        assert runtime.close(timeout=None)
        assert not errors
        assert runtime.queue.worker_error is None
    finally:
        release.set()
        runtime.close(timeout=None)


def test_tray_remains_visible_until_service_has_released_library(monkeypatch):
    from src import tray, web

    done, stopped = Event(), Event()
    calls = []
    monkeypatch.setattr(web, "request_shutdown", lambda: calls.append("request"))
    icon = SimpleNamespace(_shutdown_complete=done, stop=stopped.set, notify=lambda *args: calls.append("notice"))
    tray._quit_app(icon)
    tray._quit_app(icon)
    assert "正在退出" in icon.title
    assert calls == ["notice", "request"]
    assert not stopped.is_set()
    done.set()
    assert stopped.wait(1)


@pytest.mark.parametrize("mode", ["idle", "paused", "check", "download", "local", "auth", "starting", "factory"])
def test_actual_tray_and_server_process_finishes(tmp_path, mode):
    import json
    from pathlib import Path
    import subprocess
    import sys

    checkout = Path(__file__).resolve().parents[2]
    project = checkout.parents[1] if checkout.parent.name == ".worktrees" else checkout
    allowed = project / ".devtools" / "test-runs"
    if tmp_path.is_relative_to(allowed) and tmp_path.relative_to(allowed).parts[0].startswith("b7-shutdown-"):
        root = tmp_path / "child"
    else:
        # Full-suite callers can choose another fresh basetemp; the retained
        # process probe still uses its dedicated and explicitly isolated root.
        root = allowed / ("b7-shutdown-process-" + uuid4().hex)
    result = subprocess.run([sys.executable, str(Path(__file__).with_name("shutdown_process.py")), str(root), mode],
                            capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stderr + result.stdout + (
        (root / "data/tracker.log").read_text(encoding="utf-8") if (root / "data/tracker.log").exists() else "")
    evidence = json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert evidence["closed"]
    assert evidence["callback_seconds"] < .3
    assert evidence["exit_seconds"] < (1 if mode in ("idle", "paused", "starting") else 2)
    assert len(evidence["notices"]) == 1
    if mode in ("check", "download", "local", "auth", "factory"):
        assert evidence["waiting_visible"]
    if mode == "paused":
        assert evidence["states"] == [{"state": "queued", "items": []}]
    elif mode == "download":
        assert evidence["states"] == [{"state": "interrupted", "items": ["pending", "pending"]}]
    elif mode == "local":
        assert evidence["states"] == [{"state": "running", "items": ["succeeded", "pending"]}]
    elif mode == "check":
        assert evidence["states"] == [{"state": "interrupted", "items": []}]


def test_client_stop_prevents_next_page_or_buffered_work(monkeypatch):
    from src.client import PixivClient
    from src.request_policy import ShutdownRequested

    client = PixivClient()
    pages = []

    def page(*args):
        pages.append(args)
        return {"illusts": [1, 2], "next_url": "https://app-api.pixiv.net/v1/user/illusts?offset=30"}

    monkeypatch.setattr(client, "_get", page)
    monkeypatch.setattr(client, "_parse_illust", lambda item: item)
    works = client.get_all_artist_illusts("1")
    assert next(works) == 1
    client.request_stop()
    with pytest.raises(ShutdownRequested):
        next(works)
    assert len(pages) == 1


def test_client_stop_prevents_connection_retry(monkeypatch):
    from src import client as module
    from src.request_policy import ShutdownRequested

    client = module.PixivClient()
    calls = []

    def failed(*args, **kwargs):
        calls.append(1)
        client.request_stop()
        raise module.requests.ConnectionError("synthetic")

    monkeypatch.setattr(module.requests, "get", failed)
    started = time.monotonic()
    with pytest.raises(ShutdownRequested):
        client._do_get("/synthetic", {}, {})
    assert calls == [1]
    assert time.monotonic() - started < .1


def test_real_downloader_unwinds_then_archive_resumes_without_false_failure(tmp_path, monkeypatch):
    import io
    from PIL import Image
    from sqlalchemy import select
    from src.core.download import AssetDownloader
    from src.core.models import ArchiveIntent, Artist, Asset, OperationItem, Work
    from src.core.runtime import Runtime

    output = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(output, format="PNG")
    media = output.getvalue()
    responses = []
    runtime_holder = []
    observed = []

    class Response:
        status_code = 200
        headers = {"Content-Length": str(len(media))}
        closed = False

        def iter_content(self, size):
            if len(responses) == 1:
                with runtime_holder[0].database.read() as session:
                    asset = session.scalar(select(Asset))
                    observed.append((asset.state, asset.error))
                runtime_holder[0].queue.request_stop()
            yield media

        def close(self):
            self.closed = True

    class Session:
        def get(self, *args, **kwargs):
            response = Response()
            responses.append(response)
            return response

    monkeypatch.setattr(AssetDownloader, "_before_request", classmethod(lambda cls: None))
    downloader = AssetDownloader(session=Session())
    connected = SimpleNamespace(get_account_id=lambda: "synthetic")
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True, downloader=downloader, client=connected)
    runtime_holder.append(runtime)
    with runtime.database.write() as session:
        work = Work(id=1, pixiv_illust_id="10", title="synthetic", artist=Artist(id=1, pixiv_user_id="1", name="synthetic"))
        work.assets = [Asset(role="image", state="missing", page_index=0, relative_path="10_p0.png", source_url="https://i.pximg.net/10_p0.png")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="10")])
    operation = runtime.queue.submit("repair", {"work_ids": [1]}, "synthetic")["operation_id"]
    try:
        assert runtime.queue.run_round() is False
        assert responses[0].closed
        with runtime.database.read() as session:
            asset = session.scalar(select(Asset))
            assert (asset.state, asset.error) == observed[0]
            assert asset.state != "failed"
            assert set(session.scalars(select(OperationItem.state))) <= {"pending"}
    finally:
        runtime.close()
    recovered = Runtime(tmp_path / "archive.db", tmp_path / "images", downloader=downloader, client=connected)
    try:
        recovered.queue.recover()
        # The reused downloader still enforces its real request interval after
        # restart; a synchronous round can legitimately yield during cooldown.
        deadline = time.monotonic() + 10
        while not recovered.queue.run_round():
            assert time.monotonic() < deadline, "recovered download did not finish after normal admission"
            time.sleep(0.05)
        assert recovered.queue.details(operation)["state"] == "finished"
        with recovered.database.read() as session:
            asset = session.scalar(select(Asset))
            assert asset.state == "verified" and asset.error is None
            assert (recovered.images / asset.relative_path).is_file()
        assert len(responses) == 2 and all(response.closed for response in responses)
    finally:
        recovered.close()


@pytest.mark.parametrize("already_cancelled", [False, True])
def test_auth_cancel_does_not_save_late_token_or_start_another_request(monkeypatch, already_cancelled):
    from src import auth

    event = Event()
    if already_cancelled:
        event.set()
    monkeypatch.setattr(auth, "_cancel_event", event)
    monkeypatch.setattr(auth, "_load_token", lambda: {"refresh_token": "synthetic"})
    requests, saves = [], []

    def post(*args, **kwargs):
        requests.append(1)
        event.set()
        return SimpleNamespace(status_code=200, json=lambda: {"access_token": "synthetic", "refresh_token": "synthetic"})

    monkeypatch.setattr(auth.requests, "post", post)
    monkeypatch.setattr(auth, "_save_token", lambda *args: saves.append(1))
    assert auth._try_refresh_token() is None
    assert requests == ([] if already_cancelled else [1])
    assert saves == []
