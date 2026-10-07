import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.database import ArchiveDatabase, UpgradeRequired
from src.core.models import (
    ArchiveIntent, Artist, Asset, LocalPreference, Operation, TrackingPolicy, Work,
)


def test_open_legacy_does_not_migrate_or_change_data(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE illustration(id INTEGER PRIMARY KEY, title TEXT)")
        connection.execute("INSERT INTO illustration VALUES (7, 'original')")
    before = path.read_bytes()
    with pytest.raises(UpgradeRequired):
        ArchiveDatabase(path)
    assert path.read_bytes() == before


def test_create_never_overwrites_existing_database(tmp_path):
    path = tmp_path / "library.db"
    path.write_bytes(b"preserve")
    with pytest.raises(FileExistsError):
        ArchiveDatabase.create(path)
    assert path.read_bytes() == b"preserve"


def test_failed_initialization_does_not_leave_partial_schema(tmp_path, monkeypatch):
    from src.core.database import Base

    path = tmp_path / "failed.db"

    def fail_during_creation(connection):
        connection.exec_driver_sql("CREATE TABLE initialization_probe(id INTEGER)")
        raise RuntimeError("simulated initialization failure")

    monkeypatch.setattr(Base.metadata, "create_all", fail_during_creation)
    with pytest.raises(RuntimeError, match="initialization failure"):
        ArchiveDatabase.create(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == []


def test_stopping_tracking_preserves_identity_work_and_preferences(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "library.db")
    try:
        with db.write() as session:
            artist = Artist(id=4, pixiv_user_id="41", name="Artist")
            artist.policy = TrackingPolicy(mode="active")
            work = Work(id=8, pixiv_illust_id="81", title="Work", artist=artist)
            work.preference = LocalPreference(is_hidden=True, rating=3)
            session.add(work)
        with db.write() as session:
            session.get(TrackingPolicy, 4).mode = "stopped"
        with db.read() as session:
            assert session.get(Work, 8).artist.name == "Artist"
            assert session.get(LocalPreference, 8).is_hidden
            assert session.get(LocalPreference, 8).rating == 3
            assert session.get(TrackingPolicy, 4).mode == "stopped"
    finally:
        db.close()


def test_asset_identity_is_unique_and_suppression_survives_missing_work(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "library.db")
    try:
        with db.write() as session:
            artist = Artist(id=1, pixiv_user_id="1", name="Artist")
            session.add(Work(id=1, pixiv_illust_id="10", title="Work", artist=artist))
            session.add(ArchiveIntent(pixiv_illust_id="deleted-work", mode="suppressed"))
        with db.write() as session:
            session.add(Asset(work_id=1, role="image", page_index=0))
        with pytest.raises(IntegrityError), db.write() as session:
            session.add(Asset(work_id=1, role="image", page_index=0))
        with db.read() as session:
            assert session.get(ArchiveIntent, "deleted-work").mode == "suppressed"
    finally:
        db.close()


def test_checkpoint_recycling_preserves_pinned_snapshot_and_committed_rows(tmp_path):
    path = tmp_path / "checkpoint.db"
    db = ArchiveDatabase.create(path)
    observer = sqlite3.connect(path)
    expected = 0
    try:
        page_size = observer.execute("PRAGMA page_size").fetchone()[0]
        with db.writer_engine.connect() as connection:
            threshold = connection.exec_driver_sql("PRAGMA wal_autocheckpoint").scalar_one()
            assert 0 < threshold <= 1000
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 2
        observer.execute("BEGIN")
        assert observer.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        wal = path.with_name(path.name + "-wal")
        # A long external snapshot can pin the WAL. The writer must still
        # commit, and recycle it once that snapshot ends; no forced truncation.
        for batch in range(128):
            with db.write() as session:
                for index in range(16):
                    ident = f"checkpoint-{batch}-{index}"
                    session.add(Operation(id=ident, kind="verify", label=ident,
                        dedupe_key=ident, parameters={"payload": "x" * 2048}))
            expected += 16
            if wal.stat().st_size > (threshold + 40) * (page_size + 24) + 32:
                break
        else:
            pytest.fail("Synthetic writes did not cross the checkpoint window")
        assert observer.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        with db.read() as session:
            assert session.query(Operation).count() == expected
        with wal.open("rb") as stream:
            initial_salt = stream.read(24)[16:24]
        observer.rollback()
        for number in range(2):
            with db.write() as session:
                session.get(Operation, "checkpoint-0-0").label = f"committed-{number}"
        with wal.open("rb") as stream:
            assert stream.read(24)[16:24] != initial_salt
        with pytest.raises(RuntimeError, match="rollback marker"), db.write() as session:
            session.get(Operation, "checkpoint-0-0").label = "must roll back"
            raise RuntimeError("rollback marker")
    finally:
        observer.close()
        db.close()
    reopened = ArchiveDatabase(path)
    try:
        with reopened.read() as session:
            assert session.query(Operation).count() == expected
            assert session.get(Operation, "checkpoint-0-0").label == "committed-1"
        with reopened.writer_engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 2
            assert connection.exec_driver_sql("PRAGMA quick_check").scalar_one() == "ok"
    finally:
        reopened.close()
