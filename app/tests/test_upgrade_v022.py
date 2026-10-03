import sqlite3
from contextlib import closing

import pytest

from src.core.database import ArchiveDatabase, UpgradeRequired
from src.core.models import Artist, RemoteBookmark, Work
from src.core.upgrade import upgrade_0002


def old_database(path):
    db = ArchiveDatabase.create(path)
    with db.write() as session:
        artist = Artist(id=1, pixiv_user_id="10", name="Artist")
        work = Work(id=2, pixiv_illust_id="20", artist=artist, title="Work")
        work.bookmark = RemoteBookmark(value=True)
        session.add(work)
    db.close()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DROP TABLE bookmark_operations")
        connection.execute("DROP TABLE application_state")
        for column in ("biography", "avatar_path", "profile_refreshed_at"):
            connection.execute(f"ALTER TABLE artists DROP COLUMN {column}")
        for column in ("source", "account_id", "version"):
            connection.execute(f"ALTER TABLE remote_bookmarks DROP COLUMN {column}")
        connection.execute("UPDATE alembic_version SET version_num='0001_archive'")
        connection.commit()
    return path


def test_explicit_upgrade_preserves_source_and_legacy_bookmark_is_cache(tmp_path):
    source = old_database(tmp_path / "source.db")
    before = source.read_bytes()
    candidate, backup = tmp_path / "candidate.db", tmp_path / "backup.db"
    with pytest.raises(UpgradeRequired):
        ArchiveDatabase(source)
    upgrade_0002(source, candidate, backup)
    assert source.read_bytes() == before
    assert backup.exists()
    db = ArchiveDatabase(candidate)
    try:
        with db.read() as session:
            bookmark = session.get(RemoteBookmark, 2)
            assert bookmark.value is True and bookmark.source == "legacy_cache"
            assert bookmark.confirmed_at is None and bookmark.version == 0
            assert session.get(Artist, 1).biography is None
        with db.write() as session:
            session.add(Work(id=3, pixiv_illust_id="30", artist_id=1, title="New"))
            session.add(RemoteBookmark(work_id=3, value=None, source="legacy_cache"))
        with db.read() as session:
            assert session.get(RemoteBookmark, 3).value is None
    finally:
        db.close()


def test_invalid_or_corrupt_source_never_claims_a_candidate(tmp_path):
    invalid = tmp_path / "invalid.db"
    invalid.write_bytes(b"not sqlite")
    with pytest.raises((sqlite3.DatabaseError, UpgradeRequired)):
        upgrade_0002(invalid, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert not (tmp_path / "candidate.db").exists()
    assert not (tmp_path / "backup.db").exists()


def test_existing_backup_is_preserved(tmp_path):
    source = old_database(tmp_path / "source.db")
    backup = tmp_path / "backup.db"
    backup.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        upgrade_0002(source, tmp_path / "candidate.db", backup)
    assert backup.read_bytes() == b"keep"


def test_candidate_ddl_failure_rolls_back_and_keeps_source_and_backup(tmp_path):
    source = old_database(tmp_path / "source.db")
    # Valid 0001 data with a colliding extra table forces failure mid-upgrade.
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE remote_bookmarks_next (sentinel INTEGER)")
    before = source.read_bytes()
    candidate, backup = tmp_path / "candidate.db", tmp_path / "backup.db"
    with pytest.raises(sqlite3.OperationalError):
        upgrade_0002(source, candidate, backup)
    assert source.read_bytes() == before
    assert backup.exists() and candidate.exists()
    with sqlite3.connect(candidate) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0001_archive",)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(artists)")}
        assert "biography" not in columns
