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
    traced = []
    with upgrade._readonly(source) as connection:
        connection.set_trace_callback(traced.append)
        upgrade._inspect_old(connection)
    assert not any(statement in traced for statement in (
        "PRAGMA quick_check", "PRAGMA integrity_check", "PRAGMA foreign_key_check"))
    with pytest.raises(UpgradeRequired):
        ArchiveDatabase(source)
    result = upgrade.upgrade_0005(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert fingerprint(source) == fingerprint(result["backup"]) == before
    assert result["validation"]["schema_revision"] == SCHEMA_REVISION == "0005_read_cards_tasks"
    assert result["validation"]["database_check"] == "source_preservation_and_new_mappings"
    assert result["validation"]["projection"]["cards"] == {"cards": 1}
    candidate = ArchiveDatabase(result["candidate"])
    card = Library(candidate).query(compact=True)["items"][0]
    assert (card["id"], card["title"], card["preview"], card["is_bookmarked"]) == (
        7, "unchanged", "/images/kept/original.png", True)
    assert card["processing"]["operation_id"] == "op-kept"
    candidate.close()
    receipt = upgrade.activate_upgrade(result, writers_stopped=True)
    assert upgrade._file_fingerprint(receipt["active"]) == receipt["active_fingerprint"]
    upgrade.rollback_upgrade(receipt, writers_stopped=True)
    assert fingerprint(source) == before


def test_verified_candidate_change_is_rejected_before_switch(tmp_path):
    source = source_0004(tmp_path / "source.db")
    before = fingerprint(source)
    result = upgrade.upgrade_0005(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    with closing(sqlite3.connect(result["candidate"])) as connection:
        connection.execute("UPDATE user_extra SET content=? WHERE id=1", (b"changed",))
        connection.commit()
    with pytest.raises(UpgradeRequired, match="候选库在核验后已改变"):
        upgrade.activate_upgrade(result, writers_stopped=True)
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


@pytest.mark.parametrize("statement", [
    "UPDATE user_extra SET content=X'00'",
    "DELETE FROM operations",
    "INSERT INTO user_extra VALUES(2,X'00')",
    "DROP TABLE user_extra",
    "PRAGMA writable_schema=ON",
])
def test_copy_guard_blocks_original_data_and_schema_writes(tmp_path, monkeypatch, statement):
    source = source_0004(tmp_path / "source.db")
    before = fingerprint(source)
    attempted = []

    def forbidden(owner):
        attempted.append(statement)
        owner.connection().exec_driver_sql(statement)

    monkeypatch.setattr(cards, "backfill", forbidden)
    with pytest.raises(Exception, match="not authorized"):
        upgrade.upgrade_0005(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert attempted == [statement]
    assert fingerprint(source) == fingerprint(tmp_path / "backup.db") == before
    with upgrade._readonly(tmp_path / "candidate.db") as check:
        assert check.execute("SELECT version_num FROM alembic_version").fetchone()[0] == upgrade.READ_PROJECTIONS_REVISION
        assert check.execute("SELECT content FROM user_extra").fetchone()[0] == b"untouched\x00value"


def test_guarded_copy_skips_payload_audit_but_standalone_validation_is_strict(tmp_path, monkeypatch):
    source = source_0004(tmp_path / "source.db")
    def no_payload_scan(*args, **kwargs):
        raise AssertionError("internal copy must not fingerprint SQL payloads")
    with monkeypatch.context() as scoped:
        scoped.setattr(upgrade, "_fingerprint", no_payload_scan)
        scoped.setattr(upgrade, "_table_fingerprint", no_payload_scan)
        result = upgrade.upgrade_0005(source, tmp_path / "candidate.db", tmp_path / "backup.db")
        receipt = upgrade.activate_upgrade(result, writers_stopped=True)
        upgrade.rollback_upgrade(receipt, writers_stopped=True)
    with closing(sqlite3.connect(result["candidate"])) as connection:
        connection.execute("UPDATE user_extra SET content=X'01'")
        connection.commit()
    with pytest.raises(UpgradeRequired, match="保留数据核验失败：user_extra"):
        upgrade.validate_candidate(result["candidate"], result["backup"])


@pytest.mark.parametrize("statement", [
    "CREATE TABLE read_gallery_cards(user_content BLOB)",
    "CREATE INDEX ix_read_gallery_card_artist ON user_extra(id)",
    "CREATE TRIGGER read_task_scope_definition_update AFTER UPDATE ON user_extra BEGIN SELECT 1; END",
])
def test_copy_guard_rejects_existing_schema_name_collisions(tmp_path, statement):
    source = source_0004(tmp_path / "source.db")
    with closing(sqlite3.connect(source)) as connection:
        connection.execute(statement)
        connection.commit()
    before = fingerprint(source)
    with pytest.raises(UpgradeRequired, match="同名对象"):
        upgrade.upgrade_0005(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert fingerprint(source) == before
    assert not (tmp_path / "candidate.db").exists() and not (tmp_path / "backup.db").exists()


def wal_source(tmp_path):
    """Copy a committed live WAL pair to an isolated path, without checkpoint."""
    donor = source_0004(tmp_path / "donor.db")
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(donor)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE user_extra SET content=?", (b"committed in WAL",))
        connection.commit()
        source.write_bytes(donor.read_bytes())
        source.with_name(source.name + "-wal").write_bytes(donor.with_name(donor.name + "-wal").read_bytes())
    return source


def test_file_receipt_keeps_wal_snapshot_and_can_restore_after_failed_activation(tmp_path, monkeypatch):
    source = wal_source(tmp_path)
    before = fingerprint(source)
    result = upgrade.upgrade_0005(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert result["fingerprint_mode"] == upgrade._FILE_FINGERPRINT
    assert result["source_fingerprint"] != result["backup_fingerprint"]
    assert fingerprint(result["backup"]) == before
    original = upgrade.ArchiveDatabase
    def failed_open(path):
        raise RuntimeError("synthetic open failure")
    monkeypatch.setattr(upgrade, "ArchiveDatabase", failed_open)
    with pytest.raises(RuntimeError, match="synthetic open failure"):
        upgrade.activate_upgrade(result, writers_stopped=True)
    assert fingerprint(source) == before
    assert upgrade._journal_state(source)["state"] == "restored"
    monkeypatch.setattr(upgrade, "ArchiveDatabase", original)
    # A fresh receipt uses the restored physical layout; old result is retained.
    result = upgrade.upgrade_0005(source, tmp_path / "candidate2.db", tmp_path / "backup2.db")
    receipt = upgrade.activate_upgrade(result, writers_stopped=True)
    upgrade.rollback_upgrade(receipt, writers_stopped=True)
    assert fingerprint(source) == before


@pytest.mark.parametrize("target, message", [
    ("source", "源库在准备后已改变"),
    ("backup", "源库备份在准备后已改变"),
    ("candidate", "候选库在核验后已改变"),
    ("active", "新版库启用后已有写入"),
])
def test_file_receipt_rejects_committed_wal_changes(tmp_path, target, message):
    source = source_0004(tmp_path / "source.db")
    result = upgrade.upgrade_0005(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    if target == "active":
        result = upgrade.activate_upgrade(result, writers_stopped=True)
    path = result[target]
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("UPDATE user_extra SET content=?", (b"new WAL write",))
        connection.commit()
        with pytest.raises(UpgradeRequired, match=message):
            if target == "active":
                upgrade.rollback_upgrade(result, writers_stopped=True)
            else:
                upgrade.activate_upgrade(result, writers_stopped=True)


def test_old_receipt_defaults_to_semantic_fingerprint(tmp_path):
    source = source_0004(tmp_path / "source.db")
    before = fingerprint(source)
    result = upgrade.upgrade_0005(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    result.pop("fingerprint_mode")
    result.pop("backup_fingerprint")
    result["source_fingerprint"] = before
    result["candidate_fingerprint"] = fingerprint(result["candidate"])
    receipt = upgrade.activate_upgrade(result, writers_stopped=True)
    assert receipt["active_fingerprint"] == fingerprint(source)
    upgrade.rollback_upgrade(receipt, writers_stopped=True)
    assert fingerprint(source) == before
