"""Explicit 0003 projection upgrades use synthetic source libraries only."""
from contextlib import closing
import json
import sqlite3

import pytest
from sqlalchemy.orm import Session

from src.core import projection_maintenance, upgrade
from src.core.database import ArchiveDatabase, SCHEMA_REVISION, UpgradeRequired
from src.core.models import (Base, Artist, Asset, Derivative, LocalPreference,
    Operation, OperationItem, RevisionAsset, RevisionDerivative, Work, WorkRevision)
from src.core.startup_upgrade import StartupUpgrade


def assert_restart_blocked(source, images, monkeypatch):
    """A normal new app must not create or open a library with unresolved intent."""
    from fastapi.testclient import TestClient
    from src import web
    from src.core.runtime import Runtime
    before = snapshot(source) if source.exists() else None
    monkeypatch.setattr(web._cfg, "DATA_DIR", source.parent)
    monkeypatch.setattr(web._cfg, "IMAGES_DIR", images)
    app = web.create_app(authenticate=False, schedule=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        state = client.get("/api/startup").json()
        assert state["state"] == "failed" and state["recovery_required"] and not state["can_start"]
        assert app.state.runtime is None
        assert client.get("/api/library").status_code == 503
        client.headers.update({"Origin": "http://127.0.0.1", "X-CSRF-Token": client.cookies.get("pixiv_csrf")})
        assert client.post("/api/startup/upgrade", json={"confirmed": True}).status_code == 409
    with pytest.raises(upgrade.UpgradeRecoveryRequired):
        Runtime(source, images, create=not source.exists())
    if before is None:
        assert not source.exists()
    else:
        assert snapshot(source) == before


def revisions_database(path):
    """Create the exact pre-projection model, with modern and legacy identities."""
    engine = ArchiveDatabase._engine(path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            Base.metadata.create_all(connection)
            connection.exec_driver_sql("CREATE TABLE alembic_version(version_num VARCHAR(32) PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO alembic_version VALUES('0003_revisions')")
            with Session(bind=connection) as session:
                artist = Artist(id=3, pixiv_user_id="30", name="synthetic", directory="original-location")
                modern = Work(id=7, artist=artist, pixiv_illust_id="70", title="current", type="manga", page_count=3,
                    default_revision_id="r-old", latest_revision_id="r-new")
                modern.preference = LocalPreference(is_hidden=True, rating=4)
                legacy = Work(id=8, artist=artist, pixiv_illust_id="80", title="legacy", type="illust", page_count=1)
                session.add_all([modern, legacy]); session.flush()
                session.add_all([
                    WorkRevision(id="r-old", work_id=7, ordinal=1, title="old", type="illust", page_count=1,
                        source_signature="synthetic-old", state="complete", viewable=True),
                    WorkRevision(id="r-new", work_id=7, ordinal=2, title="new", type="manga", page_count=3,
                        source_signature="synthetic-new", state="partial", viewable=True),
                ]); session.flush()
                session.add_all([
                    Asset(id="a-shared", work_id=7, revision_id="r-old", role="image", page_index=0,
                        state="verified", relative_path="original-location/70.png", byte_size=12),
                    Asset(id="a-legacy", work_id=8, role="image", page_index=0,
                        state="unknown", relative_path="original-location/80.png"),
                    Derivative(id="d-shared", work_id=7, revision_id="r-old", kind="thumbnail", page_index=0,
                        spec_key="synthetic", state="verified", relative_path="previews/70.webp"),
                    Derivative(id="d-legacy", work_id=8, kind="thumbnail", page_index=0,
                        state="unknown", relative_path="previews/80.webp"),
                ]); session.flush()
                session.add_all([
                    RevisionAsset(revision_id="r-old", role="image", page_index=0, asset_id="a-shared"),
                    RevisionAsset(revision_id="r-new", role="image", page_index=0, asset_id="a-shared"),
                    RevisionDerivative(revision_id="r-old", kind="thumbnail", page_index=0,
                        spec_key="synthetic", derivative_id="d-shared"),
                    RevisionDerivative(revision_id="r-new", kind="thumbnail", page_index=0,
                        spec_key="synthetic", derivative_id="d-shared"),
                ])
                operation = Operation(id="op-preserve", kind="repair", dedupe_key="synthetic", label="keep task",
                    parameters={"scope": "works", "work_ids": [7, 8]})
                operation.items = [OperationItem(id="item-preserve", item_key="synthetic", kind="download",
                    phase="downloading", lane="download", target={"work_id": 7, "work_revision_id": "r-old",
                        "asset_id": "a-shared", "preserve": True})]
                session.add(operation); session.flush()
            # Unknown user tables, indexes, triggers, BLOB values and deleted
            # AUTOINCREMENT IDs must survive the copied-source upgrade too.
            connection.exec_driver_sql('CREATE TABLE "extra source"(id INTEGER PRIMARY KEY AUTOINCREMENT, value BLOB)')
            connection.exec_driver_sql('INSERT INTO "extra source"(id,value) VALUES(15,?)', (b"synthetic\x00bytes",))
            connection.exec_driver_sql('INSERT INTO "extra source"(id,value) VALUES(99,NULL)')
            connection.exec_driver_sql('DELETE FROM "extra source" WHERE id=99')
            connection.exec_driver_sql('CREATE INDEX extra_source_value ON "extra source"(value)')
            connection.exec_driver_sql('CREATE TRIGGER extra_source_noop AFTER UPDATE ON "extra source" BEGIN SELECT 1; END')
            connection.exec_driver_sql("UPDATE operation_items SET target=? WHERE id='item-preserve'", (
                '{ "work_id": "7", "work_revision_id": "r-old", "asset_id": "a-shared", "preserve": true }',))
    finally:
        engine.dispose()
    return path


def revision(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def snapshot(path):
    with upgrade._readonly(path) as connection:
        return upgrade._fingerprint(connection)


def prepare(tmp_path):
    source = revisions_database(tmp_path / "source.db")
    return upgrade.upgrade_0004(source, tmp_path / "candidate.db", tmp_path / "backup.db")


def test_revisions_upgrade_copies_all_source_facts_and_only_adds_verified_projections(tmp_path):
    source = revisions_database(tmp_path / "source.db"); before = snapshot(source)
    with pytest.raises(UpgradeRequired):
        ArchiveDatabase(source)
    result = upgrade.prepare_upgrade(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert result["source_revision"] == "0003_revisions"
    assert snapshot(source) == snapshot(result["backup"]) == before
    assert revision(result["candidate"]) == SCHEMA_REVISION == "0005_read_cards_tasks"
    assert result["validation"]["projection"]
    with upgrade._readonly(result["candidate"]) as candidate, upgrade._readonly(source) as old:
        upgrade._verify_preserved_revisions(candidate, old)
        assert candidate.execute("SELECT default_revision_id,latest_revision_id FROM works WHERE id=8").fetchone() == (None, None)
        assert candidate.execute("SELECT count(*) FROM work_revisions").fetchone()[0] == 2
        assert candidate.execute("SELECT count(*) FROM revision_assets WHERE asset_id='a-shared'").fetchone()[0] == 2
        assert candidate.execute("SELECT target FROM operation_items").fetchone() == old.execute("SELECT target FROM operation_items").fetchone()
        assert candidate.execute("SELECT seq FROM sqlite_sequence WHERE name='extra source'").fetchone()[0] == 99
        assert candidate.execute("SELECT ready FROM read_model_meta").fetchone()[0] == 1
        assert candidate.execute("SELECT count(*) FROM read_model_dirty").fetchone()[0] == 0


def test_wal_snapshot_includes_committed_source_frames_without_checkpointing_source(tmp_path):
    source = revisions_database(tmp_path / "source.db")
    with closing(sqlite3.connect(source)) as owner:
        owner.execute("PRAGMA journal_mode=WAL"); owner.execute("PRAGMA wal_autocheckpoint=0")
        owner.execute("UPDATE works SET title='committed in WAL' WHERE id=8"); owner.commit()
        before = snapshot(source)
        result = upgrade.upgrade_0004(source, tmp_path / "candidate.db", tmp_path / "backup.db")
        assert snapshot(source) == snapshot(result["backup"]) == before
        with upgrade._readonly(result["candidate"]) as candidate:
            assert candidate.execute("SELECT title FROM works WHERE id=8").fetchone()[0] == "committed in WAL"


def test_tolerated_modern_pointer_and_cross_work_binding_facts_are_preserved_with_warnings(tmp_path):
    source = revisions_database(tmp_path / "source.db")
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("UPDATE works SET default_revision_id='dangling',latest_revision_id='r-new' WHERE id=8")
        connection.execute("INSERT INTO revision_assets VALUES('r-new','image',2,'a-legacy')")
        connection.execute("INSERT INTO revision_derivatives VALUES('r-new','thumbnail',1,'remapped','d-legacy')")
        connection.commit()
    before = snapshot(source)
    result = upgrade.upgrade_0004(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert result["validation"]["warnings"] == {"non_owned_revision_pointers": 1,
        "cross_work_resource_bindings": 2, "logical_resource_remaps": 2}
    assert snapshot(source) == snapshot(result["backup"]) == before
    with upgrade._readonly(result["candidate"]) as candidate, upgrade._readonly(source) as old:
        upgrade._verify_preserved_revisions(candidate, old)
        assert candidate.execute("SELECT default_revision_id FROM works WHERE id=8").fetchone()[0] == "dangling"


@pytest.mark.parametrize("failure_stage", ["install", "backfill", "verify", "commit"])
def test_candidate_failure_retains_source_backup_and_unactivated_candidate(tmp_path, monkeypatch, failure_stage):
    source = revisions_database(tmp_path / "source.db"); before = snapshot(source)
    if failure_stage == "install":
        def fail_install(connection):
            connection.exec_driver_sql("CREATE TABLE partial_projection(value TEXT)")
            raise RuntimeError("synthetic install failure")
        monkeypatch.setattr(projection_maintenance, "install_projections", fail_install)
    elif failure_stage == "backfill":
        def fail_backfill(session):
            session.connection().exec_driver_sql("INSERT INTO read_work_order(work_id,order_key,visible,artist_id,work_type,favorite) VALUES(99,'',1,3,'illust',0)")
            raise RuntimeError("synthetic backfill failure")
        monkeypatch.setattr(projection_maintenance, "rebuild_projections", fail_backfill)
    elif failure_stage == "verify":
        monkeypatch.setattr(projection_maintenance, "verify_projections", lambda session: (_ for _ in ()).throw(RuntimeError("synthetic mismatch")))
    else:
        from sqlalchemy import event
        original_engine = ArchiveDatabase._engine
        def failing_engine(path, **kwargs):
            engine = original_engine(path, **kwargs)
            if path.name == "candidate.db":
                event.listen(engine, "commit", lambda connection: (_ for _ in ()).throw(RuntimeError("synthetic commit failure")))
            return engine
        monkeypatch.setattr(ArchiveDatabase, "_engine", staticmethod(failing_engine))
    with pytest.raises(RuntimeError):
        upgrade.upgrade_0004(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert snapshot(source) == snapshot(tmp_path / "backup.db") == before
    assert snapshot(tmp_path / "candidate.db") == before
    assert revision(source) == revision(tmp_path / "candidate.db") == "0003_revisions"


@pytest.mark.parametrize("tamper", ["target", "binding", "unknown_table", "schema", "projection", "dirty"])
def test_revalidation_blocks_source_or_projection_tampering_without_activation(tmp_path, tamper):
    result = prepare(tmp_path); before = snapshot(result["source"])
    sql = {
        "target": "UPDATE operation_items SET target='{\"work_id\":7}'",
        "binding": "DELETE FROM revision_assets WHERE revision_id='r-new'",
        "unknown_table": 'UPDATE "extra source" SET value=NULL',
        "schema": "DROP INDEX extra_source_value",
        "projection": "UPDATE read_work_order SET work_type='tampered' WHERE work_id=7",
        "dirty": "INSERT INTO read_model_dirty VALUES('work','7')",
    }[tamper]
    with closing(sqlite3.connect(result["candidate"])) as connection:
        connection.execute(sql); connection.commit()
    with pytest.raises(UpgradeRequired):
        upgrade.activate_upgrade(result, writers_stopped=True)
    assert snapshot(result["source"]) == before
    assert result["candidate"].exists() and result["backup"].exists()


def test_candidate_validation_is_read_only_and_never_repairs(tmp_path, monkeypatch):
    result = prepare(tmp_path); before = snapshot(result["candidate"])
    monkeypatch.setattr(projection_maintenance, "rebuild_projections", lambda *args: pytest.fail("validation rebuilt"))
    upgrade.validate_candidate(result["candidate"], result["backup"])
    db = ArchiveDatabase(result["candidate"]); db.close()
    assert snapshot(result["candidate"]) == before
    def mutating_verifier(session):
        session.connection().exec_driver_sql("UPDATE read_model_meta SET generation=generation+1")
    monkeypatch.setattr(projection_maintenance, "verify_projections", mutating_verifier)
    with pytest.raises(UpgradeRequired):
        upgrade.validate_candidate(result["candidate"], result["backup"])
    assert snapshot(result["candidate"]) == before


def test_explicit_upgrade_retries_preserve_existing_attempts_and_reject_repeat_activation(tmp_path):
    result = prepare(tmp_path)
    before = {key: snapshot(result[key]) for key in ("source", "candidate", "backup")}
    with pytest.raises(FileExistsError):
        upgrade.upgrade_0004(result["source"], result["candidate"], result["backup"])
    assert before == {key: snapshot(result[key]) for key in before}
    receipt = upgrade.activate_upgrade(result, writers_stopped=True)
    active_before = snapshot(result["source"])
    with pytest.raises(UpgradeRequired):
        upgrade.activate_upgrade(result, writers_stopped=True)
    assert snapshot(result["source"]) == active_before
    restored = upgrade.rollback_upgrade(receipt, writers_stopped=True)
    assert snapshot(result["source"]) == before["source"]
    assert receipt["retained"].exists() and restored["retained_candidate"].exists()
    assert result["candidate"].exists() and result["backup"].exists()


def test_backup_changes_block_rollback_instead_of_overwriting_active_source(tmp_path):
    result = prepare(tmp_path); receipt = upgrade.activate_upgrade(result, writers_stopped=True)
    before = snapshot(result["source"])
    with closing(sqlite3.connect(result["backup"])) as connection:
        connection.execute("UPDATE works SET title='tampered backup' WHERE id=8"); connection.commit()
    with pytest.raises(UpgradeRequired, match="备份已改变"):
        upgrade.rollback_upgrade(receipt, writers_stopped=True)
    assert snapshot(result["source"]) == before


def test_projection_name_collision_keeps_source_and_candidate_at_0003(tmp_path):
    source = revisions_database(tmp_path / "source.db")
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE read_work_order(unrelated TEXT)"); connection.commit()
    before = snapshot(source)
    with pytest.raises(Exception):
        upgrade.upgrade_0004(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert snapshot(source) == snapshot(tmp_path / "candidate.db") == snapshot(tmp_path / "backup.db") == before


@pytest.mark.parametrize("invalid", ["missing_table", "missing_column", "duplicate_revision", "unsupported_revision"])
def test_incomplete_or_unsupported_source_fails_before_candidate_or_backup_creation(tmp_path, invalid):
    source = revisions_database(tmp_path / "source.db")
    with closing(sqlite3.connect(source)) as connection:
        if invalid == "missing_table":
            connection.execute("DROP TABLE revision_derivatives")
        elif invalid == "missing_column":
            connection.execute("ALTER TABLE work_revisions DROP COLUMN caption_state")
        elif invalid == "duplicate_revision":
            connection.execute("INSERT INTO alembic_version VALUES('0002_client')")
        else:
            connection.execute("UPDATE alembic_version SET version_num='unknown_revision'")
        connection.commit()
    before = snapshot(source)
    with pytest.raises(UpgradeRequired):
        upgrade.prepare_upgrade(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert snapshot(source) == before
    assert not (tmp_path / "candidate.db").exists() and not (tmp_path / "backup.db").exists()


def test_source_schema_change_after_prepare_blocks_activation(tmp_path):
    result = prepare(tmp_path)
    with closing(sqlite3.connect(result["source"])) as connection:
        connection.execute("CREATE INDEX source_changed ON works(title)"); connection.commit()
    before = snapshot(result["source"])
    with pytest.raises(UpgradeRequired, match="已改变"):
        upgrade.activate_upgrade(result, writers_stopped=True)
    assert snapshot(result["source"]) == before


def test_write_after_0003_activation_blocks_rollback_and_preserves_new_data(tmp_path):
    result = prepare(tmp_path); receipt = upgrade.activate_upgrade(result, writers_stopped=True)
    db = ArchiveDatabase(result["source"])
    with db.write() as session:
        session.get(Work, 8).title = "user edit after activation"
    db.close(); before = snapshot(result["source"])
    with pytest.raises(UpgradeRequired, match="已有写入"):
        upgrade.rollback_upgrade(receipt, writers_stopped=True)
    assert snapshot(result["source"]) == before
    assert result["backup"].exists() and receipt["retained"].exists()


def test_fingerprint_failure_after_file_switch_restores_old_source(tmp_path, monkeypatch):
    result = prepare(tmp_path); before = snapshot(result["source"])
    original_fingerprint = upgrade._fingerprint
    def fail_after_switch(connection):
        if connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == SCHEMA_REVISION:
            raise RuntimeError("synthetic post-activation fingerprint failure")
        return original_fingerprint(connection)
    monkeypatch.setattr(upgrade, "_fingerprint", fail_after_switch)
    with pytest.raises(RuntimeError):
        upgrade.activate_upgrade(result, writers_stopped=True)
    assert snapshot(result["source"]) == before
    assert revision(result["source"]) == "0003_revisions"
    assert list(tmp_path.glob("source.db.failed-*"))
    assert result["candidate"].exists() and result["backup"].exists()


def test_startup_constructor_failure_restores_0003_without_losing_revision_bindings(tmp_path):
    import asyncio
    source = revisions_database(tmp_path / "source.db"); before = snapshot(source)
    def failing_factory():
        raise RuntimeError("synthetic private error")
    controller = StartupUpgrade(source, tmp_path / "images", failing_factory); controller.require()
    async def run():
        await controller.start(lambda current: None); await controller.task
    asyncio.run(run())
    assert controller.snapshot()["state"] == "failed" and not controller.snapshot()["activated"]
    assert snapshot(source) == before
    assert list((tmp_path / "upgrade").glob("*/candidate.db"))
    assert list((tmp_path / "upgrade").glob("*/backup.db"))
    assert "private" not in json.dumps(controller.snapshot())


@pytest.mark.parametrize("phase", ["activation", "rollback"])
def test_failed_file_switch_and_failed_compensation_require_manual_recovery(tmp_path, monkeypatch, phase):
    import asyncio
    from pathlib import Path
    source = revisions_database(tmp_path / "pixiv_tracker.db"); before = snapshot(source)
    replace = upgrade.os.replace
    def fail_switch_and_restore(src, destination):
        name = Path(src).name
        blocked = (".activation-" in name or ".previous-" in name) if phase == "activation" else (".rollback-" in name or ".failed-" in name)
        if blocked:
            raise OSError("synthetic private replacement failure")
        return replace(src, destination)
    monkeypatch.setattr(upgrade.os, "replace", fail_switch_and_restore)
    def failing_factory():
        if phase == "activation":
            pytest.fail("runtime must not be constructed after failed activation")
        raise RuntimeError("synthetic constructor failure")
    controller = StartupUpgrade(source, tmp_path / "images", failing_factory); controller.require()
    async def run():
        await controller.start(lambda current: None); await controller.task
    asyncio.run(run())
    state = controller.snapshot()
    assert state["state"] == "failed" and state["recovery_required"] and not state["can_start"]
    assert not source.exists()
    retained = list(tmp_path.glob(source.name + ".previous-*"))
    assert len(retained) == 1 and snapshot(retained[0]) == before
    assert list((tmp_path / "upgrade").glob("*/candidate.db"))
    assert list((tmp_path / "upgrade").glob("*/backup.db"))
    assert "private" not in json.dumps(state)
    monkeypatch.undo()
    assert_restart_blocked(source, tmp_path / "images", monkeypatch)


@pytest.mark.parametrize("phase", ["activate", "rollback"])
@pytest.mark.parametrize("stage", [0, 1, 2])
def test_process_exit_at_each_switch_boundary_blocks_fresh_normal_startup(tmp_path, monkeypatch, phase, stage):
    import subprocess
    import sys
    from pathlib import Path
    source = revisions_database(tmp_path / "pixiv_tracker.db")
    before = snapshot(source)
    code = '''
import os,sys
from pathlib import Path
from src.core import upgrade
source=Path(sys.argv[1]); phase=sys.argv[2]; stage=int(sys.argv[3])
prepared=upgrade.prepare_upgrade(source,source.parent/'candidate.db',source.parent/'backup.db')
if phase=='rollback':
    receipt=upgrade.activate_upgrade(prepared,writers_stopped=True)
replace=os.replace
calls=0
def interrupted(src,destination):
    global calls
    calls+=1
    if stage==0 and calls==1:
        os._exit(91)
    replace(src,destination)
    if calls==stage:
        os._exit(91)
os.replace=interrupted
if phase=='activate':
    upgrade.activate_upgrade(prepared,writers_stopped=True)
else:
    upgrade.rollback_upgrade(receipt,writers_stopped=True)
raise AssertionError('switch did not reach interruption')
'''
    result = subprocess.run([sys.executable, "-c", code, str(source), phase, str(stage)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 91, result.stderr
    with pytest.raises(upgrade.UpgradeRecoveryRequired):
        upgrade.guard_upgrade_recovery(source)
    assert_restart_blocked(source, tmp_path / "images", monkeypatch)
    assert snapshot(tmp_path / "backup.db") == before
    assert (tmp_path / "candidate.db").exists()


def test_durable_terminal_switches_allow_normal_upgrade_restart_and_legacy_retry(tmp_path, monkeypatch):
    source = revisions_database(tmp_path / "pixiv_tracker.db")
    result = upgrade.prepare_upgrade(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    receipt = upgrade.activate_upgrade(result, writers_stopped=True)
    assert upgrade._journal_state(source)["state"] == "activated"
    upgrade.guard_upgrade_recovery(source)
    current = ArchiveDatabase(source); current.close()
    upgrade.rollback_upgrade(receipt, writers_stopped=True)
    assert upgrade._journal_state(source)["state"] == "rolled_back"
    upgrade.guard_upgrade_recovery(source)
    next_result = upgrade.prepare_upgrade(source, tmp_path / "candidate-next.db", tmp_path / "backup-next.db")
    assert next_result["source_revision"] == "0003_revisions"
    upgrade.activate_upgrade(next_result, writers_stopped=True)
    from src.core.runtime import Runtime
    current = Runtime(source, tmp_path / "images")
    assert current.close()


def test_genuinely_new_install_with_no_switch_history_still_creates_library(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from src import web
    data = tmp_path / "new-data"
    monkeypatch.setattr(web._cfg, "DATA_DIR", data)
    monkeypatch.setattr(web._cfg, "IMAGES_DIR", tmp_path / "new-images")
    app = web.create_app(authenticate=False, schedule=False)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/startup").json()["state"] == "ready"
        assert client.get("/api/library").status_code == 200
    assert revision(data / "pixiv_tracker.db") == SCHEMA_REVISION


def test_resolved_path_ownership_and_corrupt_journal_fail_closed(tmp_path):
    source = revisions_database(tmp_path / "pixiv_tracker.db")
    intent = upgrade._begin_switch(source, action="activate", staged=tmp_path / "staged.db",
        retained=tmp_path / "retained.db", backup=tmp_path / "backup.db")
    alias = tmp_path / "path-alias"
    import os
    if os.name == "nt":
        # Directory junctions test the same resolved ownership without requiring
        # SeCreateSymbolicLinkPrivilege or enabling Windows Developer Mode.
        import subprocess
        subprocess.run(["powershell.exe", "-NoProfile", "-Command",
            "New-Item -ItemType Junction -Path $env:PIXIV_TEST_ALIAS -Value $env:PIXIV_TEST_TARGET | Out-Null"],
            env={**os.environ, "PIXIV_TEST_ALIAS": str(alias), "PIXIV_TEST_TARGET": str(tmp_path)},
            check=True, capture_output=True)
    else:
        alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(upgrade.UpgradeRecoveryRequired):
        upgrade.guard_upgrade_recovery(alias / source.name)
    # A completion copied from another database cannot resolve this intent.
    upgrade._append_journal(source, {**intent, "source": str(tmp_path / "another.db"), "state": "activated"})
    with pytest.raises(upgrade.UpgradeRecoveryRequired):
        upgrade.guard_upgrade_recovery(source)


def test_missing_source_with_pre_journal_retained_files_is_not_a_new_install(tmp_path, monkeypatch):
    source = tmp_path / "pixiv_tracker.db"
    retained = revisions_database(tmp_path / "pixiv_tracker.db.previous-synthetic")
    assert_restart_blocked(source, tmp_path / "images", monkeypatch)
    assert retained.exists()


@pytest.mark.parametrize("schema", [
    "CREATE TABLE unknown_extension(payload BLOB)",
    "CREATE TABLE unknown_extension(rowid TEXT, payload BLOB)",
    "CREATE TABLE unknown_extension(rowid TEXT, _rowid_ TEXT, payload BLOB)",
    "CREATE TABLE unknown_extension(payload BLOB, _rowid_ INT GENERATED ALWAYS AS(length(payload)) VIRTUAL)",
])
def test_hidden_rowid_is_preserved_and_mutation_changes_fingerprint(tmp_path, schema):
    source = revisions_database(tmp_path / "source.db")
    with closing(sqlite3.connect(source)) as connection:
        connection.execute(schema)
        shadowed = {row[1] for row in connection.execute("PRAGMA table_xinfo(unknown_extension)")}
        alias = next(value for value in ("_rowid_", "rowid", "oid")
            if value not in shadowed)
        connection.execute(f"INSERT INTO unknown_extension({alias},payload) VALUES(42,?)", (b"synthetic",))
        connection.commit()
    result = upgrade.prepare_upgrade(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    with closing(sqlite3.connect(result["candidate"])) as connection:
        connection.execute(f"UPDATE unknown_extension SET {alias}=99"); connection.commit()
    with pytest.raises(UpgradeRequired, match="保留数据核验失败"):
        upgrade.validate_candidate(result["candidate"], result["backup"])
    before = snapshot(source)
    with closing(sqlite3.connect(source)) as connection:
        connection.execute(f"UPDATE unknown_extension SET {alias}=99"); connection.commit()
    assert snapshot(source) != before


def test_without_rowid_and_fully_shadowed_integer_primary_key_are_fingerprinted(tmp_path):
    source = revisions_database(tmp_path / "source.db")
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE without_hidden(id TEXT PRIMARY KEY,payload BLOB) WITHOUT ROWID")
        connection.execute("INSERT INTO without_hidden VALUES('id',?)", (b"synthetic",))
        connection.execute("CREATE TABLE shadowed(id INTEGER PRIMARY KEY,rowid TEXT,_rowid_ TEXT,oid TEXT)")
        connection.execute("INSERT INTO shadowed VALUES(42,'rowid','_rowid_','oid')"); connection.commit()
    result = upgrade.prepare_upgrade(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert snapshot(source) == snapshot(result["backup"])


def test_all_inaccessible_hidden_rowid_aliases_fail_closed_without_activation(tmp_path):
    source = revisions_database(tmp_path / "source.db")
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE inaccessible(rowid TEXT,_rowid_ TEXT,oid TEXT)"); connection.commit()
    with pytest.raises(UpgradeRequired, match="隐藏行身份"):
        upgrade.prepare_upgrade(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert revision(source) == "0003_revisions" and not (tmp_path / "candidate.db").exists()


def upgraded_root(tmp_path):
    root = tmp_path / "old-root"
    data = root / "data"; data.mkdir(parents=True)
    source = revisions_database(data / "pixiv_tracker.db")
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("UPDATE operations SET state='completed'")
        connection.execute("UPDATE operation_items SET state='done'"); connection.commit()
    result = upgrade.prepare_upgrade(source, data / "candidate.db", data / "backup.db")
    upgrade.activate_upgrade(result, writers_stopped=True)
    return root, source


def test_upgraded_library_relocation_keeps_path_owned_journal_at_original_source(tmp_path):
    from src.core.runtime import Runtime
    from src.core.relocation import prepare_relocation
    root, source = upgraded_root(tmp_path)
    journal = upgrade._journal_path(source); original = journal.read_bytes()
    current = Runtime(source, root / "images")
    candidate = None
    try:
        candidate, report = prepare_relocation(current, root, tmp_path / "new-root")
        assert report.database_ok
        assert candidate.library.query(limit=1)["items"][0]["id"] == 8
        assert journal.read_bytes() == original
        assert not upgrade._journal_path(candidate.database.path).exists()
        upgrade.guard_upgrade_recovery(candidate.database.path)
    finally:
        if candidate is not None:
            assert candidate.close()
        assert current.close()


@pytest.mark.parametrize("invalid", ["pending", "corrupt", "foreign"])
def test_unresolved_or_foreign_journal_blocks_relocation_before_copy(tmp_path, invalid):
    from src.data_migration import MigrationError, prepare_migration
    root, source = upgraded_root(tmp_path)
    if invalid == "pending":
        upgrade._begin_switch(source, action="activate", staged=source.parent / "staged.db",
            retained=source.parent / "retained.db", backup=source.parent / "backup.db")
    elif invalid == "corrupt":
        with upgrade._journal_path(source).open("a", encoding="utf-8") as output:
            output.write("unfinished journal record")
    else:
        records = upgrade._journal_path(source).read_text(encoding="utf-8")
        import json
        # JSON escapes Windows backslashes; replace the decoded ownership field
        # so the foreign-journal fault is actually injected on every platform.
        altered = [{**json.loads(line), "source": str(source.parent / "foreign.db")}
            for line in records.splitlines() if line.strip()]
        assert all(record["source"] != str(source) for record in altered)
        upgrade._journal_path(source).write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in altered) + "\n", encoding="utf-8")
    target = tmp_path / "new-root"
    with pytest.raises(MigrationError, match="恢复核验"):
        prepare_migration(root, target)
    assert not target.exists()


@pytest.mark.parametrize("when", ["before_execute", "before_success"])
def test_new_unresolved_intent_blocks_relocation_execution_and_final_success(tmp_path, monkeypatch, when):
    from src import data_migration
    root, source = upgraded_root(tmp_path)
    target = tmp_path / "new-root"
    plan = data_migration.prepare_migration(root, target)
    def begin_pending():
        upgrade._begin_switch(source, action="activate", staged=source.parent / "staged.db",
            retained=source.parent / "retained.db", backup=source.parent / "backup.db")
    if when == "before_execute":
        begin_pending()
    else:
        copied = data_migration._copy_tree
        def introduce_intent(src, dst, **kwargs):
            copied(src, dst, **kwargs)
            if src == root / "images":
                begin_pending()
        monkeypatch.setattr(data_migration, "_copy_tree", introduce_intent)
    with pytest.raises(data_migration.MigrationError, match="恢复核验"):
        data_migration.execute_migration(plan)
    if when == "before_execute":
        assert not target.exists()
    else:
        assert (target / "data" / "pixiv_tracker.db").exists()
    assert upgrade._journal_state(source)["state"] == "pending"
