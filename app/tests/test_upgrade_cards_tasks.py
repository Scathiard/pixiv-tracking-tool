"""The approved 0004 -> 0005 path preserves source facts; synthetic data only."""
import sqlite3
from contextlib import closing

import pytest

from src.core import gallery_card_projection as cards, normalized_task_projection as tasks, upgrade
from src.core.database import ArchiveDatabase, SCHEMA_REVISION, UpgradeRequired
from src.core.library import Library
from src.core.models import Artist, Asset, Operation, OperationItem, RemoteBookmark, Work


def source_0004(path):
    database = ArchiveDatabase.create(path)
    with database.write() as session:
        artist = Artist(id=1, pixiv_user_id="101", name="synthetic", directory="kept")
        work = Work(id=7, pixiv_illust_id="107", artist=artist, title="unchanged", type="illust", page_count=1)
        work.bookmark = RemoteBookmark(value=True, source="legacy_cache", version=3)
        session.add(work)
        session.flush()
        session.add(Asset(id="a-kept", work_id=7, role="image", page_index=0,
            state="verified", relative_path="kept/original.png", byte_size=10))
        operation = Operation(id="op-kept", kind="repair", dedupe_key="kept", label="kept",
            state="ready", parameters={"work_ids": [7]})
        operation.items = [OperationItem(id="i-kept", item_key="kept", kind="download",
            phase="downloading", lane="download", target={"work_id": 7}, state="pending")]
        session.add(operation)
    database.close()
    # Remove only 0005 additions from this independent synthetic fixture.
    # The source tables and pre-existing 0004 projection definitions are exact.
    with closing(sqlite3.connect(path)) as connection:
        for name in (*cards.TRIGGERS, "read_task_scope_definition_update"):
            connection.execute(f'DROP TRIGGER "{name}"')
        for name in (*tasks.TABLES, cards.TABLE):
            connection.execute(f'DROP TABLE "{name}"')
        connection.execute("CREATE TABLE user_extra(id INTEGER PRIMARY KEY, content BLOB)")
        connection.execute("INSERT INTO user_extra VALUES(1,?)", (b"untouched\x00value",))
        connection.execute("UPDATE alembic_version SET version_num='0004_read_projections'")
        connection.commit()
    return path


def fingerprint(path):
    with upgrade._readonly(path) as connection:
        return upgrade._fingerprint(connection)


def test_0004_candidate_keeps_sources_and_can_activate_then_rollback(tmp_path):
    source = source_0004(tmp_path / "source.db")
    before = fingerprint(source)
    with pytest.raises(UpgradeRequired):
        ArchiveDatabase(source)
    result = upgrade.upgrade_0005(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert fingerprint(source) == fingerprint(result["backup"]) == before
    assert result["validation"]["schema_revision"] == SCHEMA_REVISION == "0005_read_cards_tasks"
    assert result["validation"]["projection"]["cards"] == {"cards": 1}
    candidate = ArchiveDatabase(result["candidate"])
    card = Library(candidate).query(compact=True)["items"][0]
    assert (card["id"], card["title"], card["preview"], card["is_bookmarked"]) == (
        7, "unchanged", "/images/kept/original.png", True)
    assert card["processing"]["operation_id"] == "op-kept"
    candidate.close()
    receipt = upgrade.activate_upgrade(result, writers_stopped=True)
    assert fingerprint(receipt["active"]) == receipt["active_fingerprint"]
    upgrade.rollback_upgrade(receipt, writers_stopped=True)
    assert fingerprint(source) == before


def test_0004_failed_new_backfill_does_not_change_source_or_publish_candidate(tmp_path, monkeypatch):
    source = source_0004(tmp_path / "source.db")
    before = fingerprint(source)
    def fail(owner, **kwargs):
        raise RuntimeError("synthetic backfill failure")
    monkeypatch.setattr(cards, "backfill", fail)
    candidate, backup = tmp_path / "candidate.db", tmp_path / "backup.db"
    with pytest.raises(RuntimeError, match="synthetic backfill failure"):
        upgrade.upgrade_0005(source, candidate, backup)
    assert fingerprint(source) == fingerprint(backup) == before
    with upgrade._readonly(candidate) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0004_read_projections"
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (cards.TABLE,)).fetchone()
