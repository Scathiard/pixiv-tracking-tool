from datetime import datetime

from src.core.database import ArchiveDatabase
from src.core.library import Library, LibraryFilter
from src.core.models import Artist, Work, Asset, ArchiveIntent
from src.routes.archive_api import parse_filters


def test_page_windows_cross_work_boundaries_without_loading_whole_long_work(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "reading.db")
    try:
        with db.write() as session:
            artist = Artist(id=1, pixiv_user_id="1", name="local")
            session.add(artist)
            for index, (kind, count) in enumerate((("illust", 1), ("manga", 2000), ("ugoira", 60)), 1):
                work = Work(id=index, pixiv_illust_id=str(index), artist=artist, title=str(index), type=kind, page_count=count)
                session.add_all([work, ArchiveIntent(pixiv_illust_id=str(index))])
            session.add(Asset(work_id=2, role="image", page_index=1999, relative_path="2_p1999.png", width=900, height=1200))
        library = Library(db)
        filters = LibraryFilter(sort="oldest")
        result = library.feed(filters, offset=1980, limit=48, ceiling=3)
        assert result["total"] == 2002 and len(result["items"]) == 22
        assert result["items"][-2]["src"] == "/images/2_p1999.png"
        assert result["items"][-1]["work_id"] == 3 and result["items"][-1]["page_count"] == 1
        assert result["items"][0]["page"] == 1979 and result["items"][0]["state"] == "missing"
        anchor = library.locate(2, filters, ceiling=3, page=1500)
        assert anchor == {"work_id": 2, "work_index": 1, "page": 1500, "index": 1501, "next_work_index": 2001}
        assert library.feed(filters, offset=anchor["index"], limit=1, ceiling=3)["items"][0]["page"] == 1500
        assert library.feed(LibraryFilter(artist_ids=()), ceiling=3)["items"] == []
    finally:
        db.close()


def test_relative_period_is_frozen_by_browser_snapshot_time():
    filters = parse_filters({"period": "week", "as_of": "2026-09-26T12:00:00", "sort": "oldest"})
    assert filters.posted_after == datetime(2026, 9, 19, 12)
