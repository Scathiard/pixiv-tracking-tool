import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image
from sqlalchemy import select

from src.core.archive import ArchiveHandler
from src.core.database import ArchiveDatabase
from src.core.download import AssetDownloader, DownloadFailure
from src.core.library import Library, LibraryFilter
from src.core.media import Publisher
from src.core.models import ArchiveIntent, Artist, Asset, Derivative, LocalPreference, RemoteBookmark, TrackingPolicy, Work
from src.core.tasks import TaskQueue


def png(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(output, format="PNG")
    return output.getvalue()


class FakePixiv:
    def __init__(self):
        self.calls = []
        self.on_artist = lambda _: None

    def get_all_artist_illusts(self, remote_id):
        self.calls.append(remote_id)
        self.on_artist(remote_id)
        return iter(())

    def get_ugoira_metadata(self, remote_id):
        self.calls.append("ugoira-" + remote_id)
        return {"zip_urls": {"medium": "https://i.pximg.net/animation.zip"},
                "frames": [{"file": "first.png", "delay": 100}, {"file": "second.png", "delay": 200}]}


    def get_illust_detail(self, remote_id):
        self.calls.append("detail-" + remote_id)
        return {"illust_id": remote_id, "title": "legacy", "type": "ugoira", "page_count": 1}


class Download:
    def __init__(self):
        self.urls = []

    def download(self, url, destination, report=None):
        self.urls.append(url)
        if url.endswith(".zip"):
            with zipfile.ZipFile(destination, "w") as archive:
                archive.writestr("first.png", png())
                archive.writestr("second.png", png("blue"))
        else:
            destination.write_bytes(png())


@pytest.fixture
def setup_archive(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    root = tmp_path / "media"
    root.mkdir()
    publisher = Publisher(database, root)
    client, download = FakePixiv(), Download()
    handlers = {kind: ArchiveHandler(kind, database, publisher, client, download) for kind in ("check", "repair", "backfill", "verify", "thumbnails")}
    queue = TaskQueue(database, handlers)
    with database.write() as session:
        for index in (1, 2):
            artist = Artist(id=index, pixiv_user_id=str(index), name=f"artist{index}")
            artist.policy = TrackingPolicy(mode="active")
            session.add(artist)
    yield database, root, queue, client, download
    database.close()


def test_hundred_page_work_only_downloads_two_missing_pages(setup_archive):
    database, root, queue, client, download = setup_archive
    folder = root / "existing"
    folder.mkdir()
    with database.write() as session:
        work = Work(id=1, pixiv_illust_id="100", artist_id=1, title="100 pages", type="manga", page_count=100)
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])
        session.flush()
        for page in range(100):
            relative = f"existing/100_p{page}.png"
            if page not in (5, 97):
                (root / relative).write_bytes(png())
            session.add(Asset(work_id=1, page_index=page, role="image", relative_path=relative,
                              source_url=f"https://i.pximg.net/100_p{page}.png"))
    queue.submit("repair", {"work_ids": [1]}, "补下载")
    queue.run_round()
    assert download.urls == ["https://i.pximg.net/100_p5.png", "https://i.pximg.net/100_p97.png"]
    assert client.calls == []
    with database.read() as session:
        assert all(asset.state == "verified" for asset in session.scalars(select(Asset)))
    assert queue.snapshot()["round"]["outcome"] == "succeeded"


def test_started_automatic_batch_finishes_but_next_uses_new_pause(setup_archive):
    database, root, queue, client, download = setup_archive

    def pause_second(remote_id):
        if remote_id == "1":
            Library(database).set_tracking(2, "paused")

    client.on_artist = pause_second
    queue.submit("check", {}, "本轮", source="automatic")
    queue.run_round()
    assert client.calls == ["1", "2"]
    queue.submit("check", {}, "下轮", source="automatic")
    queue.run_round()
    assert client.calls == ["1", "2", "1"]
    queue.submit("check", {"artist_ids": [2]}, "主动检查")
    queue.run_round()
    assert client.calls[-1] == "2"


def test_gif_only_archive_does_not_silently_backfill(setup_archive):
    database, root, queue, client, download = setup_archive
    Image.new("RGB", (8, 6), "red").save(root / "200.gif")
    with database.write() as session:
        work = Work(id=1, pixiv_illust_id="200", artist_id=1, title="legacy", type="ugoira",
                    source_metadata={"original_backfill_requires_selection": True})
        work.assets = [Asset(role="ugoira_zip", state="missing_deferred"), Asset(role="ugoira_timing")]
        work.derivatives = [Derivative(kind="gif", relative_path="200.gif", state="unknown")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="200")])
    queue.submit("repair", {}, "后台补下载", source="maintenance")
    queue.run_round()
    assert client.calls == [] and download.urls == []
    assert [work["id"] for work in Library(database).missing_originals()] == [1]
    queue.submit("backfill", {"work_ids": [1]}, "补回所选原包")
    queue.run_round()
    assert download.urls == ["https://i.pximg.net/animation.zip"]
    with database.read() as session:
        from src.core.revisions import latest_revision, revision_assets
        assets = revision_assets(session, latest_revision(session, 1))
        assert all(asset.state == "verified" for asset in assets)
        assert all((root / asset.relative_path).is_file() for asset in assets)
    assert "/revisions/" in Library(database).detail(1)["pages"][0]["src"]
    assert Library(database).missing_originals() == []


def add_gif_only_work(database, root, work_id=1):
    remote_id = str(200 + work_id - 1)
    Image.new("RGB", (8, 6), "red").save(root / f"{remote_id}.gif")
    with database.write() as session:
        work = Work(id=work_id, pixiv_illust_id=remote_id, artist_id=1, title="legacy", type="ugoira",
                    source_metadata={"original_backfill_requires_selection": True})
        work.assets = [Asset(role="ugoira_zip", state="missing_deferred"), Asset(role="ugoira_timing")]
        work.derivatives = [Derivative(kind="gif", relative_path=f"{remote_id}.gif", state="unknown")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id=remote_id)])


def test_selected_repair_fills_gif_only_originals_without_backfilling_other_works(setup_archive):
    database, root, queue, client, download = setup_archive
    for work_id in (1, 2):
        add_gif_only_work(database, root, work_id)
    queue.submit("repair", {"work_ids": [1]}, "补下载此作品")
    queue.run_round()
    assert client.calls == ["ugoira-200"]
    assert download.urls == ["https://i.pximg.net/animation.zip"]
    assert queue.snapshot()["round"]["outcome"] == "succeeded"
    with database.read() as session:
        selected = session.get(Work, 1)
        from src.core.revisions import latest_revision, revision_assets
        assert all(asset.state == "verified" for asset in revision_assets(session, latest_revision(session, 1)))
        assert not selected.source_metadata["original_backfill_requires_selection"]
        assert session.get(Work, 2).source_metadata["original_backfill_requires_selection"]
    assert [work["id"] for work in Library(database).missing_originals()] == [2]
    assert "/revisions/" in Library(database).detail(1)["pages"][0]["src"]


@pytest.mark.parametrize("parameters,source", [({}, "maintenance"), ({"artist_ids": [1]}, "automatic"),
    ({"work_ids": [1]}, "maintenance"), ({"work_ids": [1]}, "automatic")])
def test_unselected_repair_keeps_legacy_animation_originals_deferred(setup_archive, parameters, source):
    database, root, queue, client, download = setup_archive
    add_gif_only_work(database, root)
    original_gif = (root / "200.gif").read_bytes()
    queue.submit("repair", parameters, "补下载", source=source)
    queue.run_round()
    assert client.calls == [] and download.urls == []
    assert (root / "200.gif").read_bytes() == original_gif
    assert [work["id"] for work in Library(database).missing_originals()] == [1]


@pytest.mark.parametrize("parameters,expected", [({}, ["ugoira-200", "ugoira-201"]),
    ({"artist_ids": [1]}, ["ugoira-200"])])
def test_manual_scope_repairs_legacy_animation_originals(setup_archive, parameters, expected):
    database, root, queue, client, download = setup_archive
    for work_id in (1, 2):
        add_gif_only_work(database, root, work_id)
    with database.write() as session:
        session.get(Work, 2).artist_id = 2
    before = [(root / f"{remote_id}.gif").read_bytes() for remote_id in (200, 201)]
    queue.submit("repair", parameters, "补下载", source="manual")
    queue.run_round()
    assert client.calls == expected
    assert len(download.urls) == len(expected)
    for index, remote_id in enumerate((200, 201)):
        assert (root / f"{remote_id}.gif").read_bytes() == before[index]
        current_src = Library(database).detail(index + 1)["pages"][0]["src"]
        with Image.open(root / current_src.removeprefix("/images/")) as preview:
            assert preview.format == "GIF"
            preview.load()
            if "ugoira-" + str(remote_id) in expected:
                assert preview.n_frames == 2
                assert preview.info["duration"] == 100
                preview.seek(1)
                assert preview.info["duration"] == 200
            else:
                assert (root / f"{remote_id}.gif").read_bytes() == before[index]
    with database.read() as session:
        from src.core.revisions import latest_revision, revision_assets
        assert all(asset.state == "verified" for asset in revision_assets(session, latest_revision(session, 1)))
        second_revision = latest_revision(session, 2)
        second_assets = revision_assets(session, second_revision) if second_revision else session.get(Work, 2).assets
        assert all(asset.state == "verified" for asset in second_assets) == (not parameters)


@pytest.mark.parametrize("detail_state", ["unavailable", "private", "available", "api_network", "api_unauthorized", "api_rate_limit"])
def test_missing_ugoira_metadata_checks_work_fact_and_preserves_local_preview(setup_archive, detail_state):
    from src.client import PixivRequestError

    database, root, queue, client, download = setup_archive
    add_gif_only_work(database, root)
    before = (root / "200.gif").read_bytes()

    def missing_metadata(remote_id):
        client.calls.append("ugoira-" + remote_id)
        raise PixivRequestError(404, "主站暂时无法提供动图原包或播放时序；已有本地内容保留",
                                retryable=False, failure_code="ugoira_metadata_unavailable")

    def detail(remote_id):
        client.calls.append("detail-" + remote_id)
        if detail_state == "available":
            return {"illust_id": remote_id, "title": "legacy", "type": "ugoira", "page_count": 1}
        code = {"unavailable": "work_unavailable", "private": "work_private"}.get(detail_state, detail_state)
        status = {"unavailable": 404, "private": 403, "api_unauthorized": 401, "api_rate_limit": 429}.get(detail_state)
        raise PixivRequestError(status, "受控的失败原因", retryable=detail_state.startswith("api_"), failure_code=code)

    client.get_ugoira_metadata, client.get_illust_detail = missing_metadata, detail
    queue.submit("repair", {"work_ids": [1]}, "补下载此作品")
    queue.run_round()
    assert client.calls == ["ugoira-200", "detail-200"]
    assert download.urls == []
    assert (root / "200.gif").read_bytes() == before
    item = Library(database).missing_originals()[0]
    availability = item["availability"]
    if detail_state in ("unavailable", "private", "available"):
        assert availability["state"] == detail_state
    else:
        assert availability is None
    assert item["recovery"]["reason"]
    assert Library(database).detail(1)["pages"][0]["src"] == "/images/200.gif"
    with database.read() as session:
        assert session.scalar(select(Derivative).where(Derivative.kind == "thumbnail")).state == "verified"
    assert queue.snapshot()["round"]["outcome"] in ("failed", "partial_failure")


@pytest.mark.parametrize("parameters", [{}, {"artist_ids": [1]}])
def test_scope_repair_skips_known_unavailable_work_and_continues(setup_archive, parameters):
    from src.core.activity_log import processing_log
    database, root, queue, client, download = setup_archive
    for work_id in (1, 2):
        add_gif_only_work(database, root, work_id)
    with database.write() as session:
        work = session.get(Work, 1)
        work.source_metadata = {**work.source_metadata, "remote_availability": {
            "state": "unavailable", "checked_at": "2026-09-30T00:00:00+00:00",
            "confirmed": True, "policy": "source-v2",
            "reason": "主站暂时无法获取此作品，尚不能确认是否删除；已有存档保留"}}
    submitted = queue.submit("repair", parameters, "补下载")
    queue.run_round()
    assert client.calls == ["ugoira-201"]
    assert len(download.urls) == 1
    details = queue.details(submitted["operation_id"])
    skipped = [item for item in details["items"] if item["state"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["result"]["failure_code"] == "work_unavailable"
    assert details["counts"]["skipped"] == 1
    assert queue.snapshot()["round"]["counts"]["skipped"] == 1
    log = processing_log(database)["lines"]
    assert any("跳过 1 项" in line for line in log)
    assert any("已跳过" in line and "未确认删除" in line for line in log)
    assert [work["id"] for work in Library(database).missing_originals()] == [1]
    assert Library(database).detail(1)["pages"][0]["src"] == "/images/200.gif"
    client.calls.clear()
    queue.submit("repair", {"work_ids": [1]}, "主动重新核对")
    queue.run_round()
    assert client.calls == ["detail-200", "ugoira-200"]
    assert Library(database).missing_originals() == []


@pytest.mark.parametrize("code,status", [("api_network", None), ("api_unauthorized", 401), ("api_rate_limit", 429)])
def test_temporary_animation_error_does_not_probe_work_or_set_unavailable(setup_archive, code, status):
    from src.client import PixivRequestError

    database, root, queue, client, download = setup_archive
    add_gif_only_work(database, root)
    before = (root / "200.gif").read_bytes()

    def failed_metadata(remote_id):
        client.calls.append("ugoira-" + remote_id)
        raise PixivRequestError(status, "受控的临时失败", retryable=True, failure_code=code)

    client.get_ugoira_metadata = failed_metadata
    submitted = queue.submit("repair", {}, "补下载")
    queue.run_round()
    assert client.calls == ["ugoira-200"]
    assert download.urls == []
    assert Library(database).missing_originals()[0]["availability"] is None
    failed = [item for item in queue.details(submitted["operation_id"])["items"] if item["state"] == "failed"]
    assert failed[0]["result"]["failure_code"] == code
    assert (root / "200.gif").read_bytes() == before


@pytest.mark.parametrize("failure", ["network", "invalid_zip"])
def test_selected_animation_repair_failure_preserves_existing_gif(setup_archive, failure):
    database, root, queue, client, download = setup_archive
    add_gif_only_work(database, root)
    original_gif = (root / "200.gif").read_bytes()

    def failed_download(url, destination, report=None):
        download.urls.append(url)
        if failure == "network":
            raise DownloadFailure("network", "测试网络中断", retryable=True)
        destination.write_bytes(b"not an animation archive")

    download.download = failed_download
    queue.submit("repair", {"work_ids": [1]}, "补下载此作品")
    queue.run_round()
    assert download.urls == ["https://i.pximg.net/animation.zip"]
    if failure == "network":
        from src.core.models import OperationItem
        for _ in range(2):
            with database.write() as session:
                for item in session.scalars(select(OperationItem).where(OperationItem.state == "pending")):
                    item.not_before = None
            queue.run_round()
        assert len(download.urls) == 3
    assert queue.snapshot()["round"]["outcome"] in ("failed", "partial_failure")
    assert (root / "200.gif").read_bytes() == original_gif
    assert Library(database).detail(1)["pages"][0]["src"] == "/images/200.gif"
    assert [work["id"] for work in Library(database).missing_originals()] == [1]


def test_suppressed_work_and_paused_artist_stay_separate_from_visibility(setup_archive):
    database, root, queue, client, download = setup_archive
    with database.write() as session:
        session.add_all([Work(id=1, pixiv_illust_id="10", artist_id=1, title="keep"),
                         Work(id=2, pixiv_illust_id="20", artist_id=2, title="deleted"),
                         ArchiveIntent(pixiv_illust_id="10"), ArchiveIntent(pixiv_illust_id="20", mode="suppressed")])
    library = Library(database)
    library.set_tracking(1, "stopped")
    assert [work["id"] for work in library.query()["items"]] == [1]
    assert library.query(LibraryFilter(artist_ids=()))["items"] == []
    with pytest.raises(KeyError):
        library.detail(2)


def test_valid_gif_does_not_mask_mismatching_original_timing(setup_archive):
    database, root, queue, client, download = setup_archive
    Image.new("RGB", (8, 6), "red").save(root / "200.gif")
    with zipfile.ZipFile(root / "200.zip", "w") as archive:
        archive.writestr("first.png", png())
    (root / "200.frames.json").write_text(json.dumps({"frames": [{"file": "wrong.png", "delay": 100}]}))
    with database.write() as session:
        work = Work(id=1, pixiv_illust_id="200", artist_id=1, title="mismatch", type="ugoira")
        work.assets = [Asset(role="ugoira_zip", relative_path="200.zip"),
                       Asset(role="ugoira_timing", relative_path="200.frames.json")]
        work.derivatives = [Derivative(kind="gif", relative_path="200.gif")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="200")])
    queue.submit("verify", {}, "本地核验")
    queue.run_round()
    assert queue.snapshot()["round"]["outcome"] == "partial_failure"
    assert queue.snapshot()["round"]["counts"] == {"failed": 1, "succeeded": 3}
    assert Library(database).detail(1)["pages"][0]["src"] == "/images/200.gif"
    assert Library(database).feed(offset=0, limit=1, ceiling=1)["items"][0]["src"] == "/images/200.gif"
    assert len(Library(database).missing_originals()) == 1
    assert client.calls == []
    queue.submit("repair", {"work_ids": [1]}, "修复配对")
    queue.run_round()
    assert queue.snapshot()["round"]["outcome"] == "succeeded"
    assert Library(database).missing_originals() == []


def test_changed_original_invalidates_thumbnail_but_unchanged_files_are_reused(setup_archive, monkeypatch):
    database, root, queue, client, download = setup_archive
    with database.write() as session:
        work = Work(id=1, pixiv_illust_id="100", artist_id=1, title="image")
        work.assets = [Asset(role="image", relative_path="100.png")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])
    (root / "100.png").write_bytes(png())
    queue.submit("repair", {}, "首次核验")
    queue.run_round()
    with database.read() as session:
        first_hash = session.scalar(select(Derivative)).sha256
    (root / "100.png").write_bytes(png("blue"))
    queue.submit("repair", {}, "重建缩略图")
    queue.run_round()
    with database.read() as session:
        assert session.scalar(select(Derivative)).sha256 != first_hash
    assert download.urls == []
    import src.core.archive as module

    def unexpected_decode(*_):
        raise AssertionError("unchanged verified bytes must not be decoded again on routine checks")

    monkeypatch.setattr(module, "verify_media", unexpected_decode)
    queue.submit("repair", {}, "复查")
    queue.run_round()
    assert queue.snapshot()["round"]["outcome"] == "empty"


def test_query_snapshot_excludes_newly_added_works_and_neighbors_use_same_scope(setup_archive):
    database, root, queue, client, download = setup_archive
    with database.write() as session:
        session.add_all([Work(id=index, pixiv_illust_id=str(10 + index), artist_id=1, title=str(index)) for index in range(1, 5)])
    library = Library(database)
    first = library.query(limit=2)
    assert [work["id"] for work in first["items"]] == [4, 3]
    with database.write() as session:
        session.add(Work(id=5, pixiv_illust_id="15", artist_id=1, title="new"))
    assert [work["id"] for work in library.query(offset=2, limit=2, ceiling=first["ceiling"])["items"]] == [2, 1]
    assert library.neighbors(3, ceiling=first["ceiling"]) == {"previous": 4, "next": 2, "index": 1}


def test_metadata_refresh_only_updates_existing_scope_and_preserves_local_choices(setup_archive):
    database, root, queue, client, download = setup_archive
    with database.write() as session:
        work = Work(id=1, pixiv_illust_id="100", artist_id=1, title="old", caption="old caption")
        work.preference = LocalPreference(is_hidden=True, rating=4)
        work.bookmark = RemoteBookmark(value=True)
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])

    class MetadataClient:
        def get_artist_detail(self, remote_id):
            return {"user_id": remote_id, "name": "new name"}

        def get_illust_detail(self, remote_id):
            assert remote_id == "100"
            return {"illust_id": remote_id, "title": "new title", "type": "illust", "page_count": 1,
                    "caption": None, "tags": ["new tag"], "is_bookmarked": None, "urls": []}

        def get_all_artist_illusts(self, remote_id):
            raise AssertionError("metadata refresh must not discover/download a whole artist")

    queue.handlers["metadata"] = ArchiveHandler("metadata", database, Publisher(database, root), MetadataClient(), download)
    operation = queue.submit("metadata", {"artist_ids": [1]}, "刷新信息")
    queue.run_round()
    with database.read() as session:
        work = session.get(Work, 1)
        assert work.title == "new title" and work.caption == "old caption"
        assert work.preference.is_hidden and work.preference.rating == 4
        assert work.bookmark.value and work.bookmark.confirmed_at is None
        assert session.get(Artist, 1).name == "new name"
        assert len(list(session.scalars(select(Work)))) == 1
    result = queue.details(operation["operation_id"])
    assert result["result"]["outcome"] == "succeeded" and result["counts"]["succeeded"] == 2
    assert download.urls == []


@pytest.fixture
def image_server():
    class ServerHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/unauthorized":
                self.send_response(403)
                self.end_headers()
            elif self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://unapproved.invalid/image")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(len(png()) if self.path == "/image" else 999))
                self.end_headers()
                self.wfile.write(png())

    server = ThreadingHTTPServer(("127.0.0.1", 0), ServerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


def test_requests_transport_complete_auth_partial_and_redirect_contracts(tmp_path, image_server):
    downloader = AssetDownloader(allowed_hosts=("127.0.0.1",), allow_http=True)
    result = downloader.download(image_server + "/image", tmp_path / "ok.part")
    assert result["bytes"] == len(png())
    assert (tmp_path / "ok.part").read_bytes() == png()
    for name, code in [("unauthorized", "authentication"), ("partial", "network"), ("redirect", "invalid_source")]:
        with pytest.raises(DownloadFailure) as caught:
            downloader.download(image_server + "/" + name, tmp_path / (name + ".part"))
        assert caught.value.code == code
    assert not (tmp_path / "unauthorized.part").exists()


@pytest.fixture
def pixiv_header_server():
    expected_agent = "PixivIOSApp/7.19.1 (iOS 16.6; iPhone14,5)"
    animation = io.BytesIO()
    with zipfile.ZipFile(animation, "w") as archive:
        archive.writestr("000000.png", png())
    payloads = {"/original.png": png(), "/original.zip": animation.getvalue()}
    received = []

    class ServerHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            received.append((self.path, self.headers.get("User-Agent"),
                             self.headers.get("Referer"), self.headers.get("Authorization")))
            if (self.headers.get("User-Agent") != expected_agent
                    or self.headers.get("Referer") != "https://www.pixiv.net/"
                    or self.headers.get("Authorization")):
                self.send_response(403)
                self.end_headers()
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/original.png")
                self.end_headers()
                return
            if self.path == "/unapproved":
                self.send_response(302)
                self.send_header("Location", "http://unapproved.invalid/original.png")
                self.end_headers()
                return
            body = payloads[self.path]
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ServerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", payloads, received
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("path", ["/original.png", "/original.zip"])
def test_pixiv_image_headers_fetch_original_image_and_ugoira_zip(tmp_path, pixiv_header_server, path):
    base_url, payloads, received = pixiv_header_server
    destination = tmp_path / path[1:]
    result = AssetDownloader(allowed_hosts=("127.0.0.1",), allow_http=True).download(
        base_url + path, destination)
    assert result["bytes"] == len(payloads[path])
    assert destination.read_bytes() == payloads[path]
    assert received[-1][0] == path
    if path.endswith(".zip"):
        with zipfile.ZipFile(destination) as archive:
            assert archive.namelist() == ["000000.png"]


def test_pixiv_image_headers_survive_redirect_without_allowing_other_hosts(tmp_path, pixiv_header_server):
    base_url, payloads, received = pixiv_header_server
    downloader = AssetDownloader(allowed_hosts=("127.0.0.1",), allow_http=True)
    destination = tmp_path / "redirected.png"
    downloader.download(base_url + "/redirect", destination)
    assert destination.read_bytes() == payloads["/original.png"]
    assert [request[0] for request in received] == ["/redirect", "/original.png"]
    with pytest.raises(DownloadFailure) as caught:
        downloader.download(base_url + "/unapproved", tmp_path / "unapproved.png")
    assert caught.value.code == "invalid_source"
    assert [request[0] for request in received] == ["/redirect", "/original.png", "/unapproved"]
