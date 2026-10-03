from datetime import datetime
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from src.core.database import ArchiveDatabase
from src.core.library import Library, LibraryFilter
from src.core.models import Artist, Asset, Work, WorkRevision, RevisionAsset
from src.core.reading import ReadingSessions
from src.routes.media_response import local_media_response
from src.single_instance import ApplicationInstance


def seed(path, works=7, long_pages=3):
    database = ArchiveDatabase.create(path)
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="101", name="isolated")); session.flush()
        for index in range(1, works + 1):
            count = long_pages if index == 1 else 1
            work = Work(id=index, pixiv_illust_id=str(1000 + index), artist_id=1, title=f"Work {index:05}", page_count=count,
                type="manga", default_revision_id=f"old-{index}", latest_revision_id=f"old-{index}")
            session.add(work); session.flush()
            revision = WorkRevision(id=f"old-{index}", work_id=index, ordinal=1, title=work.title, type="manga", page_count=count,
                caption="Old caption", caption_state="available", tags=["old"], source_metadata={}, source_signature="old", change_kind="initial", state="complete")
            session.add(revision); session.flush()
            for page in range(count):
                asset = Asset(id=f"old-{index}-{page}", work_id=index, revision_id=revision.id, role="image", page_index=page,
                    state="verified", relative_path=f"old/{index}/{page}.png", width=100, height=120)
                session.add(asset); session.flush()
                session.add(RevisionAsset(revision_id=revision.id, role="image", page_index=page, asset_id=asset.id))
    return database, Library(database)


def test_visit_pins_membership_and_revision_while_current_catalogue_changes(tmp_path):
    db, library = seed(tmp_path / "archive.db")
    sessions = ReadingSessions(library)
    visit = sessions.create(LibraryFilter(sort="title"))
    token = visit["session_id"]
    with db.write() as session:
        work = session.get(Work, 1)
        revision = WorkRevision(id="new", work_id=1, ordinal=2, title="Z changed", type="manga", page_count=1,
            caption="New", caption_state="available", tags=["new"], source_metadata={}, source_signature="new", change_kind="source", state="complete")
        session.add(revision); session.flush()
        asset = Asset(id="new-image", work_id=1, revision_id="new", role="image", state="verified", relative_path="new.png")
        session.add(asset); session.flush()
        session.add(RevisionAsset(revision_id="new", role="image", page_index=0, asset_id=asset.id))
        work.default_revision_id = work.latest_revision_id = "new"
        work.title, work.page_count = "Z changed", 1
    pinned = sessions.feed(token, limit=3)
    assert [page["src"] for page in pinned["items"]] == [f"/images/old/1/{page}.png" for page in range(3)]
    assert {page["title"] for page in pinned["items"]} == {"Work 00001"}
    assert library.detail(1)["title"] == "Z changed"
    assert library.query(compact=True)["items"][0]["id"] != 1  # default order tie: newest ID
    assert library.detail(1, revision_id="old-1")["archive"]["total"] == 3
    assert library.detail(1)["archive"]["total"] == 1
    assert len(library.detail(1)["revisions"]) == 2
    db.close()


def test_random_rounds_no_repeat_stable_back_and_exclusion_keeps_relative_order(tmp_path):
    db, library = seed(tmp_path / "archive.db")
    sessions = ReadingSessions(library)
    visit = sessions.create(LibraryFilter(sort="title"))
    token, pages = visit["session_id"], visit["page_total"]
    sequences = []
    for number in range(12):
        rows = sessions.feed(token, mode="random", offset=number * pages, limit=pages)["items"]
        ids = [page["work_id"] for page in rows if page["page"] == 0]
        assert len(set(ids)) == len(ids) == 7
        if sequences: assert ids[0] != sequences[-1][-1]
        sequences.append(ids)
    assert [page["work_id"] for page in sessions.feed(token, mode="random", offset=pages, limit=pages)["items"] if page["page"] == 0] == sequences[1]
    sessions.exclude(token, sequences[1][2])
    members, _ = sessions._visits[token].order("random", 1)
    assert [member.work_id for member in members] == [value for value in sequences[1] if value != sequences[1][2]]
    assert len(sessions._visits[token].orders) <= 3
    db.close()


def test_large_manifest_small_pages_window_and_light_counts(tmp_path):
    db, library = seed(tmp_path / "archive.db", works=5000, long_pages=2000)
    sessions = ReadingSessions(library)
    visit = sessions.create(LibraryFilter(sort="title"))
    rows = sessions.feed(visit["session_id"], offset=1950, limit=120)
    assert len(rows["items"]) == 120 and rows["items"][0]["page"] == 1950
    assert rows["items"][-1]["work_id"] == 71
    statements = []
    def record(_a, _b, statement, *_): statements.append(statement)
    event.listen(db.engine, "before_cursor_execute", record)
    assert library.counts()["total"] == 5000
    event.remove(db.engine, "before_cursor_execute", record)
    assert not any("operation_items" in sql or "FROM assets" in sql or "FROM derivatives" in sql for sql in statements)
    assert len(statements) < 10
    db.close()


def test_custom_dates_use_device_boundary_preserve_legacy_unknown(tmp_path):
    db, library = seed(tmp_path / "archive.db", works=3, long_pages=1)
    with db.write() as session:
        for index, stamp, basis in [(1, datetime(2026, 10, 2, 16), "utc"), (2, datetime(2026, 10, 3, 16), "utc"),
                                     (3, datetime(2026, 10, 3, 0), "legacy_unknown")]:
            work = session.get(Work, index); work.posted_at, work.time_basis = stamp, basis
    result = library.query(LibraryFilter(posted_after=datetime(2026, 10, 3), posted_before=datetime(2026, 10, 4), tz_offset_minutes=-480))
    assert {work["id"] for work in result["items"]} == {1, 3}
    with db.read() as session:
        assert session.get(Work, 3).posted_at == datetime(2026, 10, 3)
    db.close()


def test_conditional_media_precedence_and_byte_changes(tmp_path):
    from fastapi import Request
    path = tmp_path / "test.png"; path.write_bytes(b"first")
    app = FastAPI()
    @app.get("/media")
    def media(request: Request): return local_media_response(request, path)
    with TestClient(app) as client:
        first = client.get("/media")
        assert client.get("/media", headers={"If-None-Match": first.headers["etag"]}).status_code == 304
        assert client.get("/media", headers={"If-Modified-Since": first.headers["last-modified"]}).status_code == 304
        assert client.get("/media", headers={"If-None-Match": '"other"', "If-Modified-Since": first.headers["last-modified"]}).status_code == 200
        path.write_bytes(b"second")
        assert client.get("/media", headers={"If-None-Match": first.headers["etag"]}).content == b"second"


def test_single_instance_waits_for_ready_then_unlocks(tmp_path):
    first, second = ApplicationInstance(tmp_path), ApplicationInstance(tmp_path)
    assert first.acquire() and not second.acquire()
    values = []
    waiter = threading.Thread(target=lambda: values.append(second.existing_url(timeout=3)))
    waiter.start(); time.sleep(.12)
    assert not values
    first.mark_ready("http://localhost:1919")
    waiter.join(3)
    assert values == ["http://localhost:1919"]
    first.close()
    third = ApplicationInstance(tmp_path)
    assert third.acquire()
    third.close()


def test_single_instance_is_process_wide_and_released_after_crash(tmp_path):
    import subprocess
    import sys
    script = "from src.single_instance import ApplicationInstance; import sys,os; owner=ApplicationInstance(sys.argv[1]); assert owner.acquire(); owner.mark_ready('http://localhost:1919'); print('ready',flush=True); sys.stdin.readline(); os._exit(23)"
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        second = ApplicationInstance(tmp_path)
        assert not second.acquire()
        assert second.existing_url(timeout=2) == "http://localhost:1919"
    finally:
        # Crash the lock-owning interpreter itself; a Windows venv redirector
        # may be only a parent of the actual Python process.
        child.stdin.write("crash\n"); child.stdin.flush(); child.wait(timeout=5)
    assert child.returncode == 23
    replacement = ApplicationInstance(tmp_path)
    assert replacement.acquire()
    replacement.close()


def test_conditional_media_does_not_bypass_removed_work_or_registered_path(tmp_path):
    from src.core.runtime import Runtime
    from src.core.models import ArchiveIntent
    from src.routes.archive_api import create_router
    from PIL import Image
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True, client=object())
    path = runtime.images / "one.png"
    with Image.new("RGB", (16, 16), "blue") as picture: picture.save(path)
    with runtime.database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="isolated"))
        session.add(Work(id=1, pixiv_illust_id="1", artist_id=1, title="isolated"))
        session.add(ArchiveIntent(pixiv_illust_id="1")); session.flush()
        session.add(Asset(work_id=1, role="image", relative_path="one.png", state="verified"))
    app = FastAPI(); app.include_router(create_router(lambda: runtime))
    try:
        with TestClient(app) as client:
            original = client.get("/images/one.png")
            assert original.status_code == 200
            assert client.get("/images/one.png", headers={"If-None-Match": original.headers["etag"]}).status_code == 304
            with runtime.database.write() as session:
                session.get(ArchiveIntent, "1").mode = "suppressed"
            assert client.get("/images/one.png", headers={"If-None-Match": original.headers["etag"]}).status_code == 404
            assert client.get("/images/../archive.db", headers={"If-None-Match": "*"}).status_code != 304
    finally:
        runtime.close()
