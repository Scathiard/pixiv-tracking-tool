"""Synthetic gallery cards with and without a locally readable first page."""
from io import BytesIO

from PIL import Image
from sqlalchemy import event, select

from src.core.archive import ArchiveHandler
from src.core.database import ArchiveDatabase
from src.core.library import Library
from src.core.media import Publisher
from src.core.models import ArchiveIntent, Artist, Asset, Derivative, RemoteBookmark, Work
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

        statements = 0

        def count_statement(*_):
            nonlocal statements
            statements += 1

        event.listen(database.engine, "before_cursor_execute", count_statement)
        try:
            feed = library.feed(ceiling=2)
        finally:
            event.remove(database.engine, "before_cursor_execute", count_statement)
        assert statements <= 10
        second_work = [item for item in feed["items"] if item["work_id"] == 2]
        assert len(second_work) == 2
        assert all(item["bookmark"]["value"] is True for item in second_work)
        assert second_work[0]["bookmark"] == second_work[1]["bookmark"]
    finally:
        database.close()
