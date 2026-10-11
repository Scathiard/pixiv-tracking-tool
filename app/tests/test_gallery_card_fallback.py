"""Synthetic gallery cards with and without a locally readable first page."""
from io import BytesIO

from PIL import Image
from sqlalchemy import event, select

from src.core.archive import ArchiveHandler
from src.core.database import ArchiveDatabase
from src.core.library import Library
from src.core.media import Publisher
from src.core.models import ApplicationState, ArchiveIntent, Artist, Asset, Derivative, RemoteBookmark, Work
from src.core.tasks import TaskQueue


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (24, 16), "blue").save(output, format="PNG")
    return output.getvalue()


class LocalDownload:
    def download(self, url, destination, report=None):
        destination.write_bytes(image_bytes())


def test_missing_original_stays_visible_and_repair_then_generates_thumbnail(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    images = tmp_path / "images"
    images.mkdir()
    publisher = Publisher(database, images)
    queue = TaskQueue(database, {kind: ArchiveHandler(kind, database, publisher, object(), LocalDownload())
                                 for kind in ("thumbnails", "repair")})
    try:
        with database.write() as session:
            session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
            for work_id in (1, 2):
                work = Work(id=work_id, pixiv_illust_id=str(100 + work_id), artist_id=1,
                            title=f"work {work_id}", type="illust", page_count=2 if work_id == 2 else 1)
                work.bookmark = RemoteBookmark(value=work_id == 2)
                work.assets = [Asset(role="image", page_index=0, state="missing" if work_id == 1 else "unknown",
                                     relative_path=None if work_id == 1 else "102_p0.png",
                                     source_url="https://i.pximg.net/synthetic.png")]
                session.add_all([work, ArchiveIntent(pixiv_illust_id=str(100 + work_id))])
        (images / "102_p0.png").write_bytes(image_bytes())
        library = Library(database)
        cards = {item["id"]: item for item in library.query(compact=True)["items"]}
        assert cards[1]["thumbnail"] is cards[1]["preview"] is None
        assert cards[1]["preview_status"] == "pending"
        assert cards[2]["thumbnail"] is None
        assert cards[2]["preview"] == "/images/102_p0.png"
        assert cards[2]["preview_status"] == "ready"

        missing = queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
        assert queue.run_round()
        assert queue.details(missing["operation_id"])["result"]["outcome"] == "empty"
        with database.read() as session:
            assert session.scalar(select(Derivative).where(Derivative.work_id == 1)) is None
        assert library.query(compact=True)["total"] == 2
        missing_card = next(item for item in library.query(compact=True)["items"] if item["id"] == 1)
        assert missing_card["preview_status"] == "pending"

        repaired = queue.submit("repair", {"work_ids": [1]}, "synthetic")
        assert queue.run_round()
        assert queue.details(repaired["operation_id"])["result"]["outcome"] == "succeeded"
        repaired_card = next(item for item in library.query(compact=True)["items"] if item["id"] == 1)
        assert repaired_card["preview"] and repaired_card["thumbnail"]
        assert repaired_card["preview_status"] == "ready"

        statements = []

        def count_statement(conn, cursor, sql, params, context, many):
            statements.append(sql)

        event.listen(database.engine, "before_cursor_execute", count_statement)
        try:
            feed = library.feed(ceiling=2)
        finally:
            event.remove(database.engine, "before_cursor_execute", count_statement)
        # Verified state/display projections add three fixed batch statements;
        # preserve the N+1 guard rather than counting the old aggregate path.
        assert len(statements) <= 13
        assert len([sql for sql in statements if sql.startswith("SELECT work_id,value FROM read_work_state")]) == 1
        assert len([sql for sql in statements if sql.startswith("SELECT scope,availability FROM read_display_metadata")]) == 1
        second_work = [item for item in feed["items"] if item["work_id"] == 2]
        assert len(second_work) == 2
        assert all(item["bookmark"]["value"] is True for item in second_work)
        assert second_work[0]["bookmark"] == second_work[1]["bookmark"]
    finally:
        database.close()


def test_maintenance_feedback_is_shared_and_limited_to_requested_works(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    try:
        with database.write() as session:
            session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
            session.add(Work(id=1, pixiv_illust_id="101", artist_id=1, title="missing pages", page_count=2))
            session.add(ApplicationState(key="maintenance_work:1", value={
                "status": "waiting", "kind": "repair", "next_retry_at": "2026-09-29T09:00:00",
                "reason": "图片来源暂时拒绝访问（403）", "private_debug": "must not expose"}))
            session.add(ApplicationState(key="maintenance_work:999", value={"status": "queued"}))
        library = Library(database)
        card = library.query(compact=True)["items"][0]
        detail = library.detail(1)
        feed = library.feed()["items"]
        assert card["recovery"] == detail["recovery"] == feed[0]["recovery"] == feed[1]["recovery"]
        assert card["recovery"] == {"status": "waiting", "kind": "repair",
            "next_retry_at": "2026-09-29T09:00:00Z", "reason": "图片来源暂时拒绝访问（403）"}
        assert card["preview_status"] == "pending" and card["thumbnail"] is None
        assert all(page["src"] is None for page in feed)
    finally:
        database.close()


def test_complete_original_set_overrides_old_retry_ledger_but_first_page_does_not(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    try:
        with database.write() as session:
            session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
            work = Work(id=1, pixiv_illust_id="101", artist_id=1, title="repaired", page_count=2)
            work.assets = [Asset(role="image", page_index=page, state="verified",
                                 relative_path=f"101_p{page}.png") for page in range(2)]
            work.derivatives = [Derivative(kind="thumbnail", page_index=0, spec_key="v1",
                                           state="verified", relative_path="101.webp")]
            session.add(work)
            session.add(ApplicationState(key="maintenance_work:1", value={
                "status": "waiting", "kind": "repair", "reason": "old failure",
                "next_retry_at": "2026-09-30T09:00:00"}))
        library = Library(database)
        card = library.query(compact=True)["items"][0]
        assert card["recovery"]["status"] == "ready"
        assert card["archive"]["state"] == "complete"
        assert library.detail(1)["recovery"]["status"] == "ready"
        assert all(item["recovery"]["status"] == "ready" for item in library.feed()["items"])
        with database.write() as session:
            session.scalar(select(Asset).where(Asset.page_index == 1)).state = "failed"
        card = library.query(compact=True)["items"][0]
        assert card["preview_status"] == "ready"
        assert card["recovery"]["status"] == "waiting"
        assert card["archive"] == {"state": "partial", "total": 2, "verified": 1, "missing": 1, "unknown": 0}
        assert library.detail(1, page_offset=1, page_limit=1)["recovery"]["status"] == "waiting"
        pages = library.feed()["items"]
        assert pages[0]["recovery"]["status"] == "waiting"
        assert pages[0]["src"]
        assert pages[1]["recovery"]["status"] == "waiting"
        with database.read() as session:
            assert session.get(ApplicationState, "maintenance_work:1").value["reason"] == "old failure"
    finally:
        database.close()
