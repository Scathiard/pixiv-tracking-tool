"""v0.2.2 API behavior on a synthetic local archive only."""
from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import ArchiveDatabase
from src.core.library import Library, LibraryFilter
from src.core.models import Artist, Asset, RemoteBookmark, Work, WorkTag, utcnow
from src.core.tasks import ItemResult, ItemSpec, TaskQueue
from src.routes.archive_api import create_router


class Handler:
    def __init__(self):
        self.planned = []
        self.on_plan = None
        self.on_execute = None

    def plan(self, parameters, context):
        self.planned.append(parameters)
        if self.on_plan:
            self.on_plan(parameters, context)
        return [ItemSpec("one", "one", {}, "checking")]

    def execute(self, item, context):
        if self.on_execute:
            self.on_execute(item, context)
        return ItemResult()


def test_shared_filters_count_feed_and_location(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "synthetic.db")
    reference = utcnow()
    with database.write() as session:
        alice = Artist(id=1, pixiv_user_id="100", name="Alice", biography="artist")
        bob = Artist(id=2, pixiv_user_id="200", name="Bob")
        first = Work(id=1, pixiv_illust_id="11", artist=alice, title="Sun", page_count=2,
                     posted_at=reference - timedelta(days=3), downloaded_at=reference - timedelta(days=2))
        first.tags = [WorkTag(tag="flower")]
        first.bookmark = RemoteBookmark(value=True, source="readback", version=1, confirmed_at=reference)
        second = Work(id=2, pixiv_illust_id="22", artist=bob, title="Moon", page_count=1,
                      posted_at=reference - timedelta(days=2), downloaded_at=reference - timedelta(days=45))
        second.tags = [WorkTag(tag="night")]
        third = Work(id=3, pixiv_illust_id="33", artist=alice, title="Flower", page_count=1,
                     posted_at=reference - timedelta(days=1), downloaded_at=reference - timedelta(days=1))
        third.tags = [WorkTag(tag="light")]
        session.add_all([first, second, third])
    library = Library(database)
    selected = LibraryFilter(q="flower", recent=True, as_of=reference, sort="title")
    page = library.query(selected)
    assert [item["id"] for item in page["items"]] == [3, 1]
    assert page["total"] == 2
    assert library.feed(selected, ceiling=page["ceiling"])["total"] == 3
    assert library.locate(1, selected, ceiling=page["ceiling"])["index"] == 1
    assert library.neighbors(1, selected, ceiling=page["ceiling"])["previous"] == 3
    assert library.query(LibraryFilter(favorites=True))["items"][0]["id"] == 1
    compact = library.query(LibraryFilter(favorites=True), compact=True)["items"][0]
    assert compact["bookmark"]["value"] is True and compact["is_bookmarked"] is True
    assert library.query(LibraryFilter(recent=True, as_of=reference))["total"] == 2
    artist = library.artist_page(limit=1)
    assert artist["total"] == 2 and len(artist["items"]) == 1
    assert library.artist_detail(1)["work_count"] == 2
    assert len(library.artist_detail(1)["recent_works"]) == 2
    database.close()


def test_automatic_pause_survives_queue_recreation_and_manual_jobs_run(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "tasks.db")
    handler = Handler()
    queue = TaskQueue(database, {"check": handler})
    automatic = queue.submit("check", {}, "scheduled", source="automatic")
    queue.set_automatic_paused(True)
    assert not queue.run_round()
    assert queue.snapshot()["next_round_count"] == 1
    manual = queue.submit("check", {}, "manual")
    assert queue.run_round()
    assert queue.details(manual["operation_id"])["state"] == "finished"
    assert queue.details(automatic["operation_id"])["state"] == "queued"
    reopened = TaskQueue(database, {"check": handler})
    assert reopened.automatic_paused() is True
    reopened.set_automatic_paused(False)
    assert reopened.run_round()
    assert reopened.details(automatic["operation_id"])["state"] == "finished"
    database.close()


def test_pause_between_round_claim_and_automatic_start_defers_only_unstarted_job(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "boundary.db")
    handler = Handler()
    queue = TaskQueue(database, {"check": handler})
    manual = queue.submit("check", {"kind": "manual"}, "manual")
    automatic = queue.submit("check", {"kind": "automatic"}, "scheduled", source="automatic")
    handler.on_plan = lambda parameters, _: queue.set_automatic_paused(True) if parameters["kind"] == "manual" else None
    assert queue.run_round()
    assert queue.details(manual["operation_id"])["state"] == "finished"
    assert queue.details(automatic["operation_id"])["state"] == "queued"
    assert queue.snapshot()["next_round_count"] == 1
    handler.on_plan = None
    queue.set_automatic_paused(False)
    assert queue.run_round()
    assert queue.details(automatic["operation_id"])["state"] == "finished"
    database.close()


def test_started_automatic_round_finishes_after_pause(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "started.db")
    handler = Handler()
    queue = TaskQueue(database, {"check": handler})
    first = queue.submit("check", {"artist": 1}, "first", source="automatic")
    second = queue.submit("check", {"artist": 2}, "second", source="automatic")
    handler.on_execute = lambda *_: queue.set_automatic_paused(True)
    assert queue.run_round()
    assert queue.details(first["operation_id"])["state"] == "finished"
    assert queue.details(second["operation_id"])["state"] == "finished"
    database.close()


def test_settings_and_artist_routes_on_offline_synthetic_runtime(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "routes.db")
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="10", name="Local Artist", biography="local"))
    queue = TaskQueue(database, {"check": Handler()})
    class Runtime:
        client = None
        def __init__(self):
            self.database, self.queue, self.library = database, queue, Library(database)
    runtime = Runtime()
    app = FastAPI()
    app.include_router(create_router(lambda: runtime))
    with TestClient(app) as client:
        settings = client.get("/api/settings").json()
        assert settings["login"]["status"] == "offline"
        assert settings["stats"]["artists"] == 1
        assert client.patch("/api/settings", json={"automatic_paused": True}).json()["automatic_paused"] is True
        assert client.get("/api/artists?q=Local").json()["total"] == 1
        assert client.get("/api/artists/1").json()["biography"] == "local"
        assert client.get("/api/artists/999").status_code == 404
        assert client.delete("/api/artists/1").status_code == 422
        assert client.delete("/api/artists/1?delete_files=false").status_code == 422
        assert client.get("/api/library?favorites=maybe").status_code == 422
    database.close()
