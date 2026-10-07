"""Production Runtime/proxy/client/handlers with only synthetic tokens and transport."""
import io
import asyncio
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from PIL import Image
from sqlalchemy import event, select

from src import auth, client as client_module
from src.client import PixivClient
from src.core.download import AssetDownloader
from src.core.models import Artist, Asset, Derivative, Operation, OperationItem, TrackingPolicy, Work, utcnow
from src.core.runtime import Runtime
from src.core.tasks import ItemSpec, TaskContext
from src.core.read_control import LocalReadBusy
from src.request_policy import AccountChanged


class Response:
    def __init__(self, body=None, status=200, content=b"", headers=None):
        self.body, self.status_code, self.content = body or {}, status, content
        self.headers = headers or ({"Content-Length": str(len(content))} if content else {})

    def json(self):
        return self.body

    def iter_content(self, size):
        yield self.content

    def close(self):
        pass


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    assert predicate()


def illust(ident, bookmarked=False):
    return {"id": int(ident), "title": "synthetic", "type": "illust", "page_count": 1,
        "image_urls": {}, "meta_single_page": {"original_image_url": f"https://i.pximg.net/{ident}_p0.png"},
        "is_bookmarked": bookmarked}


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_token_file", lambda: tmp_path / "synthetic-token.json")
    monkeypatch.setattr(auth, "_credential_generation", 0)
    monkeypatch.setattr(auth, "_login_generation", 0)
    monkeypatch.setattr(auth, "_refresh_flights", {})
    auth._cancel_event.clear()
    auth._save_token("synthetic-access", "synthetic-refresh", "123")
    for owner, attributes in ((PixivClient, ("_last_request", "_api_cooldown_until", "_bookmark_cooldown_until")),
                              (AssetDownloader, ("_last_request", "_cooldown_until"))):
        for name in attributes:
            monkeypatch.setattr(owner, name, 0.)
    monkeypatch.setattr(client_module, "_MIN_INTERVAL", 0.)
    def unexpected_download(*args, **kwargs):
        pytest.fail("Synthetic task tests must never reach an external CDN")
    monkeypatch.setattr(requests.Session, "get", unexpected_download)
    yield tmp_path
    auth._cancel_event.clear()


def make_runtime(tmp_path, *, check_workers=1, downloader=None):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True,
        client=PixivClient(), downloader=downloader, check_workers=check_workers)
    with runtime.database.write() as session:
        for ident in (1, 2, 3):
            artist = Artist(id=ident, pixiv_user_id=str(ident), name="synthetic", profile_refreshed_at=utcnow())
            artist.policy = TrackingPolicy(mode="active")
            session.add(artist)
    runtime.queue.maintenance = None
    return runtime


@pytest.mark.parametrize("check_workers", [1, 2])
def test_real_runtime_checks_downloads_and_previews_overlap(credentials, monkeypatch, check_workers):
    checking, release, small = threading.Event(), threading.Event(), threading.Event()
    downloading, download_release = threading.Event(), threading.Event()
    content = io.BytesIO()
    Image.new("RGB", (16, 12), "red").save(content, format="PNG")
    image = content.getvalue()

    def transport(url, **kwargs):
        params = kwargs.get("params", {})
        if url.endswith("/v1/user/detail"):
            if str(params["user_id"]) == "3":
                small.set()
            return Response({"user": {"id": int(params["user_id"]), "name": "synthetic"}})
        if url.endswith("/v1/user/illusts"):
            if str(params["user_id"]) == "2" and params["type"] == "illust":
                checking.set()
                assert release.wait(12)
            values = [illust(100), illust(101)] if str(params["user_id"]) == "1" and params["type"] == "illust" else []
            return Response({"illusts": values})
        raise AssertionError("unexpected synthetic API endpoint")

    class ImageSession:
        def get(self, url, **kwargs):
            if "100_p0" in url:
                downloading.set()
                assert download_release.wait(12)
            return Response(content=image)

    monkeypatch.setattr(client_module.requests, "get", transport)
    downloader = AssetDownloader(session=ImageSession())
    # Only the synthetic transport's delay is controlled; the production
    # downloader/verification/publication/thumbnail code remains active.
    monkeypatch.setattr(downloader, "_admit_request", lambda: None)
    runtime = make_runtime(credentials, check_workers=check_workers, downloader=downloader)
    parent = runtime.queue.submit("check", {"artist_ids": [1, 2]}, "synthetic discovery")["operation_id"]
    runtime.queue.start()
    try:
        assert checking.wait(12)
        assert downloading.wait(12)
        runtime.queue.submit("metadata", {"artist_ids": [3]}, "later small operation")

        def preview_ready():
            with runtime.database.read() as session:
                return session.scalar(select(Derivative.id).join(Asset, Asset.work_id == Derivative.work_id).where(
                    Asset.source_url.like("%101_p0%"), Derivative.kind == "thumbnail", Derivative.state == "verified")) is not None

        wait_for(preview_ready)
        assert not release.is_set() and not download_release.is_set()
        if check_workers == 2:
            assert small.wait(8)
        else:
            assert not small.wait(.15)
        release.set()
        download_release.set()
        wait_for(lambda: runtime.queue.details(parent)["state"] == "finished")
        wait_for(small.is_set)
        assert runtime.queue.worker_error is None
        with runtime.database.read() as session:
            items = list(session.scalars(select(OperationItem).where(OperationItem.state == "succeeded")))
            assert sum(item.kind == "download" for item in items) == 2
            assert sum(item.kind == "thumbnail" for item in items) >= 2
    finally:
        release.set()
        download_release.set()
        assert runtime.close(timeout=15)


def test_proxy_task_scope_has_one_transport_attempt_and_durable_retry(credentials, monkeypatch):
    calls = []
    def unavailable(*args, **kwargs):
        calls.append(1)
        raise requests.ConnectionError("synthetic transport failure")
    monkeypatch.setattr(client_module.requests, "get", unavailable)
    runtime = make_runtime(credentials)
    ident = runtime.queue.submit("check", {"artist_ids": [1]}, "synthetic retry")["operation_id"]
    assert not runtime.queue.run_round()
    with runtime.database.read() as session:
        item = session.scalar(select(OperationItem).where(OperationItem.operation_id == ident,
            OperationItem.kind == "discover_page"))
        assert item.state == "pending" and item.attempts == 1 and item.not_before > utcnow()
        deadline, item_id = item.not_before, item.id
    assert calls == [1] and not runtime.queue._resources and not runtime.queue._checking
    assert runtime.close()
    restarted = Runtime(credentials / "archive.db", credentials / "images", client=PixivClient())
    try:
        restarted.queue.recover()
        with restarted.database.read() as session:
            item = session.get(OperationItem, item_id)
            assert item.state == "pending" and item.attempts == 1 and item.not_before == deadline
        assert not restarted.queue._step("check") and calls == [1]
    finally:
        assert restarted.close()


def test_unsent_api_cooldown_keeps_retry_budget_and_releases_resources(credentials, monkeypatch):
    runtime = make_runtime(credentials)
    ident = runtime.queue.submit("check", {"artist_ids": [1]}, "synthetic cooldown")["operation_id"]
    monkeypatch.setattr(PixivClient, "_api_cooldown_until", time.monotonic() + 60)
    monkeypatch.setattr(client_module.requests, "get", lambda *a, **k: pytest.fail("cooldown must not send HTTP"))
    try:
        assert not runtime.queue.run_round()
        with runtime.database.read() as session:
            item = session.scalar(select(OperationItem).where(OperationItem.operation_id == ident,
                OperationItem.kind == "discover_page"))
            assert item.state == "pending" and item.attempts == 0 and item.not_before > utcnow()
        assert not runtime.queue._resources and runtime.queue.worker_error is None
    finally:
        assert runtime.close()


def test_refresh_singleflight_and_later_login_wins(credentials, monkeypatch):
    started, release = threading.Event(), threading.Event()
    sends = []
    def refresh(*args, **kwargs):
        sends.append(1)
        started.set()
        assert release.wait(8)
        return Response({"access_token": "synthetic-refreshed", "refresh_token": "synthetic-rotated", "user": {"id": 123}})
    monkeypatch.setattr(auth.requests, "post", refresh)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(auth._try_refresh_token) for _ in range(4)]
        assert started.wait(5)
        time.sleep(.1)
        auth._save_token("synthetic-new-login", "synthetic-new-refresh", "456")
        release.set()
        assert [future.result(timeout=5) for future in futures] == ["synthetic-new-refresh"] * 4
    assert sends == [1]
    assert auth._load_token()["user_id"] == "456"
    assert auth._load_token()["access_token"] == "synthetic-new-login"


@pytest.mark.parametrize("change", ["login", "client"])
def test_late_real_client_observation_is_not_applied_to_new_generation(credentials, monkeypatch, change):
    started, release = threading.Event(), threading.Event()
    def transport(*args, **kwargs):
        started.set()
        assert release.wait(8)
        return Response({"illusts": [illust(100, True)]})
    monkeypatch.setattr(client_module.requests, "get", transport)
    runtime = make_runtime(credentials)
    ident = runtime.queue.submit("check", {"artist_ids": [1]}, "synthetic old account")["operation_id"]
    runtime.queue._plan(ident)
    runtime.queue._plan(ident)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.queue._step, "check")
        try:
            assert started.wait(8)
            if change == "login":
                auth._save_token("synthetic-later-access", "synthetic-later-refresh", "456")
            else:
                runtime.client = PixivClient()
            release.set()
            assert future.result(timeout=8)
            with runtime.database.read() as session:
                assert session.scalar(select(Asset.id)) is None
                item = session.scalar(select(OperationItem).where(OperationItem.operation_id == ident,
                    OperationItem.kind == "discover_page"))
                assert item.state == "pending" and item.attempts == 0
        finally:
            release.set()
            assert runtime.close()


@pytest.mark.parametrize("after_send", [False, True])
def test_local_pressure_after_claim_never_strands_running_item(credentials, monkeypatch, after_send):
    sends = []
    monkeypatch.setattr(client_module.requests, "get", lambda *a, **k:
        sends.append(1) or Response({"user": {"id": 1, "name": "updated synthetic"}}))
    runtime = make_runtime(credentials)
    ident = runtime.queue.submit("metadata", {"artist_ids": [1]}, "synthetic resource pressure")["operation_id"]
    runtime.queue._plan(ident)
    runtime.queue._plan(ident)
    if after_send:
        original_save = runtime.profiles.save
        def pressured_save(*args):
            original_save(*args)
            raise LocalReadBusy("synthetic pressure after a real HTTP response")
        monkeypatch.setattr(runtime.profiles, "save", pressured_save)
    else:
        original_read = runtime.database.read
        original_execute = runtime.queue._execute_item
        claimed, pressured = [], []
        def execute_claimed(operation_id, item, *args, **kwargs):
            # Candidate scanning may add reads. Fence the fault to the real
            # persisted claim instead of guessing a global read ordinal.
            with original_read() as session:
                stored = session.get(OperationItem, item.id)
                assert stored.state == "running" and stored.attempts == 1
            claimed.append(item.id)
            return original_execute(operation_id, item, *args, **kwargs)
        @contextmanager
        def pressured_read(*args, **kwargs):
            if claimed and not pressured:
                pressured.append(True)
                raise LocalReadBusy("synthetic pressure after the atomic claim")
            with original_read(*args, **kwargs) as session:
                yield session
        monkeypatch.setattr(runtime.queue, "_execute_item", execute_claimed)
        monkeypatch.setattr(runtime.database, "read", pressured_read)
    try:
        deferred_after = utcnow()
        assert runtime.queue._step("check")
        with runtime.database.read() as session:
            item = session.scalar(select(OperationItem).where(OperationItem.operation_id == ident,
                OperationItem.kind == "metadata_artist"))
            assert item.state == "pending" and item.attempts == int(after_send)
            assert item.not_before > deferred_after
            item_id = item.id
        assert len(sends) == int(after_send)
        assert not runtime.queue._resources and not runtime.queue._checking
        assert runtime.queue.worker_error is None
        if after_send:
            monkeypatch.setattr(runtime.profiles, "save", original_save)
        else:
            assert claimed == [item_id] and pressured == [True]
            monkeypatch.setattr(runtime.database, "read", original_read)
            monkeypatch.setattr(runtime.queue, "_execute_item", original_execute)
        with runtime.database.write() as session:
            session.get(OperationItem, item_id).not_before = None
        assert runtime.queue._step("check")
        with runtime.database.read() as session:
            assert session.get(OperationItem, item_id).state == "succeeded"
    finally:
        assert runtime.close()


def test_phase_baseline_recertifies_old_writer_states_and_then_stays_incremental(credentials):
    runtime = make_runtime(credentials)
    with runtime.database.write() as session:
        ident = runtime.queue.enqueue_in_transaction(session, "verify", {}, "synthetic phases", specs=[
            ItemSpec(str(index), "checked", {}, "inventory") for index in range(4)])["operation_id"]
    assert runtime.queue._step("local")
    with runtime.database.read() as session:
        assert session.get(Operation, ident).result["step_progress"]["inventory:items"] == {"done": 1, "total": 4}
    assert runtime.close()
    restarted = Runtime(credentials / "archive.db", credentials / "images", client=PixivClient())
    # An older build understands these canonical rows but preserves the new
    # JSON key. Simulate a completion it cannot update in the new cache.
    with restarted.database.write() as session:
        item = session.scalar(select(OperationItem).where(OperationItem.operation_id == ident,
            OperationItem.state == "pending").order_by(OperationItem.position))
        item.state = "succeeded"
    counts_queries = []
    def observe(connection, cursor, statement, params, context, many):
        if "count(" in statement.lower() and "operation_items.phase" in statement:
            counts_queries.append(statement)
    event.listen(restarted.database.writer_engine, "before_cursor_execute", observe)
    try:
        restarted.queue.recover()
        assert restarted.queue._step("local")
        assert restarted.queue._step("local")
        assert len(counts_queries) == 1
        with restarted.database.read() as session:
            operation = session.get(Operation, ident)
            assert operation.result["step_progress"]["inventory:items"] == {"done": 4, "total": 4}
            assert operation.state == "finished"
        assert not restarted.queue._validated_phases
    finally:
        event.remove(restarted.database.writer_engine, "before_cursor_execute", observe)
        assert restarted.close()


def test_candidate_epoch_rejects_stale_advance_and_reset(credentials):
    runtime = make_runtime(credentials, check_workers=2)
    try:
        key = ("synthetic-operation", "check")
        epoch = runtime.queue._advance_candidate_cursor(key, 0, 64)
        assert epoch == 1
        assert runtime.queue._advance_candidate_cursor(key, 0, 32) is None
        assert runtime.queue._advance_candidate_cursor(key, 0, None) is None
        assert runtime.queue._candidate_after[key] == 64
        assert runtime.queue._advance_candidate_cursor(key, epoch, 128) == 2
    finally:
        assert runtime.close()


def test_persisted_remote_generation_cannot_match_a_fresh_runtime(credentials):
    runtime = make_runtime(credentials)
    previous = runtime.remote.generation_context()
    assert runtime.close()
    restarted = Runtime(credentials / "archive.db", credentials / "images", client=PixivClient())
    try:
        current = restarted.remote.generation_context()
        assert previous["identity"] == current["identity"]
        assert previous["client"] == current["client"]
        assert previous["runtime"] != current["runtime"]
        with pytest.raises(AccountChanged):
            restarted.remote.assert_generation(previous)
    finally:
        assert restarted.close()


def test_legacy_token_identity_resolution_stays_within_the_same_login(credentials, monkeypatch):
    auth._save_token("synthetic-legacy-access", "synthetic-legacy-refresh")
    monkeypatch.setattr(auth.requests, "post", lambda *a, **k: Response({
        "access_token": "synthetic-resolved-access", "refresh_token": "synthetic-resolved-refresh", "user": {"id": 123}}))
    runtime = make_runtime(credentials)
    try:
        context = TaskContext(runtime.queue, "synthetic-only-context")
        with runtime.remote.task_scope(context):
            assert runtime.remote.get_account_id() == "123"
            context.checkpoint()
        assert runtime.account_id == runtime.library.account_id == "123"
    finally:
        assert runtime.close()


def test_displaced_web_connect_does_not_disconnect_a_later_client(credentials, monkeypatch):
    from src import web
    runtime = make_runtime(credentials)
    runtime.client = None
    class LaterClient:
        def get_account_id(self):
            return "456"
    later = LaterClient()
    class DisplacedClient:
        def get_account_id(self):
            runtime.client = later
            runtime.account_id = runtime.library.account_id = "456"
            return "123"
    monkeypatch.setattr(auth, "_try_refresh_token", lambda: True)
    monkeypatch.setattr(web, "PixivClient", DisplacedClient)
    monkeypatch.setattr(web, "_shutdown_requested", threading.Event())
    application = web.create_app(lambda: runtime, authenticate=True, schedule=False)
    async def exercise():
        async with application.router.lifespan_context(application):
            await application.state.auth_task
            assert runtime.client is later and runtime.account_id == "456"
            assert application.state.auth_task.exception() is None
    asyncio.run(exercise())


def test_old_persisted_page_without_generation_is_read_before_new_account_fact(credentials, monkeypatch):
    reads = []
    def transport(url, **kwargs):
        assert url.endswith("/v1/illust/detail")
        assert kwargs["params"]["illust_id"] == "100"
        reads.append(1)
        return Response({"illust": illust(100, False)})
    monkeypatch.setattr(client_module.requests, "get", transport)
    runtime = make_runtime(credentials)
    old_data = PixivClient._parse_illust(illust(100, True))
    with runtime.database.write() as session:
        runtime.queue.enqueue_in_transaction(session, "check", {}, "synthetic legacy page", specs=[
            ItemSpec("legacy-work-100", "discover_work", {"artist_id": 1, "data": old_data}, "checking", "works")])
    try:
        assert runtime.queue._step("check")
        assert reads == [1]
        with runtime.database.read() as session:
            work = session.scalar(select(Work).where(Work.pixiv_illust_id == "100"))
            assert work.bookmark.account_id == "123" and work.bookmark.value is False
            item = session.scalar(select(OperationItem).where(OperationItem.item_key == "legacy-work-100"))
            assert item.target["remote_generation"] == runtime.remote.generation_context()
    finally:
        assert runtime.close()
