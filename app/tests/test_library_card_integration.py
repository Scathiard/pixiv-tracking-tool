"""One real writer-to-gallery-to-detail flow, with synthetic source facts only."""
from sqlalchemy import event

from src.core.database import ArchiveDatabase
from src.core.library import Library
from src.core.models import Artist, BookmarkOperation, RemoteBookmark, Work, WorkRevision


def test_compact_gallery_updates_cards_and_bookmarks_without_wide_hydration(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "synthetic.db")
    try:
        with database.write() as session:
            artist = Artist(id=1, pixiv_user_id="101", name="old artist", directory="kept")
            work = Work(id=7, pixiv_illust_id="107", artist=artist, title="latest", type="manga",
                page_count=2, default_revision_id="old", latest_revision_id="old", x_restrict=1)
            work.bookmark = RemoteBookmark(value=True, source="metadata", account_id="synthetic", version=1)
            session.add(work); session.flush()
            session.add(WorkRevision(id="old", work_id=7, ordinal=1, title="pinned", type="illust",
                page_count=1, caption="complete caption", source_signature="old"))
        library = Library(database)
        library.account_id = "synthetic"
        before = library.query_cursor(compact=True)["items"][0]
        assert (before["title"], before["revision_id"], before["x_restrict"], before["is_bookmarked"]) == ("pinned", "old", 1, True)
        with database.write() as session:
            session.get(Artist, 1).name = "new artist"
            session.get(WorkRevision, "old").title = "renamed pinned"
            bookmark = session.get(RemoteBookmark, 7)
            bookmark.value = False; bookmark.version = 2
            session.add(BookmarkOperation(id="bookmark-result", request_id="synthetic-result", work_id=7,
                desired=False, kind="set", account_id="synthetic", status="confirmed", version=2))
        statements = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement.lower())
        event.listen(database.engine, "before_cursor_execute", capture)
        try:
            after = library.query_cursor(compact=True)["items"][0]
        finally:
            event.remove(database.engine, "before_cursor_execute", capture)
        assert (after["title"], after["artist_name"], after["is_bookmarked"], after["bookmark"]["status"]) == (
            "renamed pinned", "new artist", False, "confirmed")
        assert not any("source_metadata" in sql or "from work_revisions" in sql or "from assets" in sql
            or "from derivatives" in sql for sql in statements)
        detail = library.query(compact=False)["items"][0]
        assert detail["title"] == after["title"] and detail["caption"] == "complete caption"
        assert detail["bookmark"] == after["bookmark"]
    finally:
        database.close()
