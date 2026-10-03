"""Cross-service checks use only generated media and an in-memory remote."""
import io
import threading
import asyncio
from uuid import uuid4

import pytest
from PIL import Image

from src.core.models import ArchiveIntent, Artist, BookmarkOperation, RemoteBookmark, TrackingPolicy, Work
from src.core.runtime import Runtime
from src.core.tasks import ItemSpec


class Remote:
    def __init__(self):
        self.saved = False

    def get_account_id(self):
        return "42"

    def get_artist_detail(self, remote_id):
        return {"user_id": str(remote_id), "name": "画师的新名字", "biography": "本地可读的简介", "avatar_url": "https://i.pximg.net/avatar.png"}

    def add_bookmark(self, remote_id, restrict="public"):
        self.saved = True

    def delete_bookmark(self, remote_id):
        self.saved = False

    def read_bookmark(self, remote_id):
        return self.saved


@pytest.fixture
def library(tmp_path):
    remote = Remote()
    runtime = Runtime(tmp_path / "data/archive.db", tmp_path / "images", create=True, client=remote)
    with runtime.database.write() as session:
        artist = Artist(id=1, pixiv_user_id="7", name="旧名字", biography="旧简介")
        artist.policy = TrackingPolicy(mode="active")
        work = Work(id=1, pixiv_illust_id="100", artist=artist, title="work")
        work.bookmark = RemoteBookmark(value=False)
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])
    runtime.bookmarks.start()
    yield runtime, remote
    assert runtime.close()


def test_profile_cache_is_local_allowlisted_and_failed_refresh_keeps_avatar(library):
    runtime, _ = library
    payload = io.BytesIO()
    Image.new("RGB", (20, 30), "blue").save(payload, format="PNG")

    class Download:
        def download(self, url, destination):
            destination.write_bytes(payload.getvalue())

    runtime.profiles.downloader = Download()
    runtime.profiles.refresh(1)
    with runtime.database.read() as session:
        artist = session.get(Artist, 1)
        relative = artist.avatar_path
        assert artist.biography == "本地可读的简介" and artist.profile_refreshed_at
    path = runtime.media_path(relative)
    assert path and path.is_file()
    assert runtime.media_path(".profiles/1/not-referenced.webp") is None

    class FailedDownload:
        def download(self, url, destination):
            raise OSError("synthetic disk full")

    runtime.profiles.downloader = FailedDownload()
    with pytest.raises(OSError):
        runtime.profiles.refresh(1)
    with runtime.database.read() as session:
        assert session.get(Artist, 1).avatar_path == relative
    assert runtime.media_path(relative) == path


def test_late_discovery_cannot_overwrite_new_user_bookmark(library):
    runtime, remote = library
    handler = runtime.queue.handlers["check"]
    versions, account = handler._bookmark_scope(1)
    submitted = runtime.bookmarks.submit(1, True, str(uuid4()))
    assert runtime.bookmarks.stop(timeout=5)
    assert runtime.bookmarks.operation(submitted["operation_id"])["status"] == "confirmed"
    handler._apply_bookmark(1, {"is_bookmarked": False}, versions["100"], account)
    with runtime.database.read() as session:
        assert session.get(RemoteBookmark, 1).value is True


def test_missing_or_invalid_metadata_never_means_unbookmarked(library):
    runtime, _ = library
    with runtime.database.write() as session:
        session.get(RemoteBookmark, 1).value = True
    handler = runtime.queue.handlers["check"]
    for value in (None, "false", 0):
        versions, account = handler._bookmark_scope(1)
        handler._apply_bookmark(1, {"is_bookmarked": value}, versions["100"], account)
    with runtime.database.read() as session:
        assert session.get(RemoteBookmark, 1).value is True


def test_bulk_sync_reports_unconfirmed_fields_and_includes_stopped_artists(library):
    runtime, remote = library
    with runtime.database.write() as session:
        session.get(TrackingPolicy, 1).mode = "stopped"
    remote.get_all_artist_illusts = lambda _: iter([{"illust_id": "100"}])
    operation = runtime.queue.submit("bookmarks", {}, "同步收藏")
    runtime.queue.run_round()
    result = runtime.queue.details(operation["operation_id"])
    assert result["counts"]["failed"] == 1
    failed = [item for item in result["items"] if item["state"] == "failed"]
    assert failed[0]["result"]["unconfirmed_works"] == 1


def test_metadata_response_for_wrong_work_is_not_saved(library):
    runtime, remote = library
    remote.get_illust_detail = lambda _: {"illust_id": "999", "title": "wrong", "type": "illust", "page_count": 1}
    with pytest.raises(ValueError):
        runtime.queue.handlers["metadata"]._refresh_work(1, "100", 1)
    assert runtime.library.query()["total"] == 1
    assert runtime.library.detail(1)["title"] == "work"


def test_runtime_keeps_ownership_while_bookmark_readback_is_running(library):
    runtime, remote = library
    entered, release = threading.Event(), threading.Event()

    def read(remote_id):
        entered.set()
        assert release.wait(10)
        return False

    remote.read_bookmark = read
    runtime.bookmarks.refresh(1)
    assert entered.wait(3)
    assert runtime.busy()
    assert not runtime.bookmarks.stop(timeout=0.01)
    assert runtime.library.query()["total"] == 1
    release.set()
    assert runtime.bookmarks.stop(timeout=5)


@pytest.mark.parametrize("identity_valid", [True, False])
def test_startup_auth_wakes_readback_only_and_bad_identity_stays_offline(tmp_path, monkeypatch, identity_valid):
    from src import auth, web
    runtime = Runtime(tmp_path / "data/archive.db", tmp_path / "images", create=True)
    with runtime.database.write() as session:
        work = Work(id=1, pixiv_illust_id="100", title="recovery",
                    artist=Artist(pixiv_user_id="7", name="artist"))
        work.bookmark = RemoteBookmark(value=False, version=1)
        session.add(work)
        session.flush()
        session.add(BookmarkOperation(id="recover-on-login", request_id=str(uuid4()), work_id=1,
            desired=True, kind="write", account_id="42", status="pending", phase="writing", version=1))

    class ReadyRemote(Remote):
        reads = 0

        def get_account_id(self):
            if not identity_valid:
                raise ValueError("synthetic invalid identity")
            return "42"

        def read_bookmark(self, remote_id):
            self.reads += 1
            return True

        def add_bookmark(self, *args, **kwargs):
            raise AssertionError("recovery must never send a write")

    remote = ReadyRemote()
    monkeypatch.setattr(auth, "_try_refresh_token", lambda: True)
    monkeypatch.setattr(auth, "cancel_auth", lambda: None)
    monkeypatch.setattr(web, "PixivClient", lambda: remote)
    application = web.create_app(lambda: runtime, authenticate=True, schedule=False)

    async def exercise():
        async with application.router.lifespan_context(application):
            await application.state.auth_task
            if identity_valid:
                for _ in range(100):
                    if runtime.bookmarks.operation("recover-on-login")["status"] == "confirmed":
                        break
                    await asyncio.sleep(.02)
                assert runtime.bookmarks.operation("recover-on-login")["status"] == "confirmed"
                assert remote.reads == 1
            else:
                assert runtime.client is None and runtime.account_id is None
                assert runtime.bookmarks.operation("recover-on-login")["status"] == "pending"
                assert remote.reads == 0
                assert application.state.login_error

    asyncio.run(exercise())
