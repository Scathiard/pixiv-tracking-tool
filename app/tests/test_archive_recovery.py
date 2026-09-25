import io

import pytest
from PIL import Image

from src.core.database import ArchiveDatabase
from src.core.library import Library
from src.core.media import Publisher, verify_media
from src.core.models import ArchiveIntent, Artist, Asset, Work
from src.core.ownership import LibraryInUse, LibraryLease


def test_only_one_new_runtime_can_own_same_library(tmp_path):
    first, second = LibraryLease(tmp_path), LibraryLease(tmp_path)
    first.acquire()
    try:
        with pytest.raises(LibraryInUse):
            second.acquire()
    finally:
        first.close()
    with second:
        assert second._stream is not None


def test_delete_and_restart_after_file_move_preserve_suppression(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    root = tmp_path / "media"
    root.mkdir()
    Image.new("RGB", (4, 4)).save(root / "10_p0.png")
    with database.write() as session:
        artist = Artist(id=1, pixiv_user_id="1", name="artist")
        work = Work(id=1, pixiv_illust_id="10", title="delete", artist=artist)
        work.assets = [Asset(role="image", relative_path="10_p0.png")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="10")])
    publisher = Publisher(database, root)
    ids = publisher.suppress(1)
    assert Library(database).query()["total"] == 0

    def crash():
        raise SystemExit("process death")

    with pytest.raises(SystemExit):
        publisher.finish_deletion(ids[0], after_move=crash)
    assert not (root / "10_p0.png").exists()
    database.close()
    reopened = ArchiveDatabase(tmp_path / "archive.db")
    try:
        restored = Publisher(reopened, root)
        assert restored.recover() == [{"id": ids[0], "state": "committed"}]
        assert not list(root.rglob("*.deleted"))
        assert restored.suppress(1) == ids
        with reopened.read() as session:
            assert session.get(ArchiveIntent, "10").mode == "suppressed"
        assert Library(reopened).query()["total"] == 0
    finally:
        reopened.close()


def test_delete_catches_file_published_before_database_commit(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    try:
        with database.write() as session:
            work = Work(id=1, pixiv_illust_id="10", title="delete", artist=Artist(id=1, pixiv_user_id="1", name="artist"))
            asset = Asset(role="image")
            work.assets = [asset]
            session.add_all([work, ArchiveIntent(pixiv_illust_id="10")])
            session.flush()
            asset_id = asset.id
        publisher = Publisher(database, tmp_path / "media")
        relative, staged = publisher.staging()
        Image.new("RGB", (4, 4)).save(staged, format="PNG")
        journal_id = publisher.prepare(asset_id, relative, "10_p0.png", verify_media(staged, "image"), expected_revision=1)

        def crash():
            raise SystemExit()

        with pytest.raises(SystemExit):
            publisher.publish(journal_id, after_move=crash)
        publisher.suppress(1)
        results = publisher.recover()
        assert {entry["state"] for entry in results} == {"obsolete", "committed"}
        assert not (publisher.root / "10_p0.png").exists()
        assert Library(database).query()["items"] == []
    finally:
        database.close()
