"""Isolated old-library conversion, pending journals, activation and recovery."""
import json
import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import select

from src.core.database import ArchiveDatabase, UpgradeRequired
from src.core.models import (ArchiveIntent, Artist, Asset, Derivative, LocalPreference,
    Operation, OperationItem, PublicationJournal, RemoteBookmark, Work, WorkRevision)
from src.core.revisions import revision_assets, revision_derivatives
from src.core.upgrade import upgrade_0003, activate_upgrade, rollback_upgrade, validate_candidate


def client_database(path):
    db = ArchiveDatabase.create(path)
    with db.write() as session:
        artist = Artist(id=3, pixiv_user_id="30", name="old artist", directory="原路径")
        work = Work(id=7, artist=artist, pixiv_illust_id="70", title="old", type="manga", page_count=3,
            time_basis="legacy_unknown", legacy_posted_at="2020-01-01 09:00:00")
        work.preference = LocalPreference(is_hidden=True, rating=2)
        work.bookmark = RemoteBookmark(value=None, source="legacy_cache")
        session.add_all([work, ArchiveIntent(pixiv_illust_id="70"), ArchiveIntent(pixiv_illust_id="80", mode="suppressed", revision=4)])
        session.flush()
        session.add_all([Asset(id="keep-original", work_id=7, role="image", page_index=0, state="verified",
            relative_path="原路径/70_p0.png", sha256="a" * 64, byte_size=12),
            Asset(id="keep-gap", work_id=7, role="image", page_index=1, state="failed", error="synthetic"),
            Derivative(id="keep-preview", work_id=7, kind="thumbnail", relative_path=".thumbnails/70.webp", state="unknown")])
        operation = Operation(id="keep-operation", kind="repair", dedupe_key="keep-dedupe", label="pending")
        operation.items = [OperationItem(id="keep-item", item_key="keep-key", kind="download", phase="downloading",
            target={"work_id": 7, "asset_id": "keep-gap", "revision": 1}, depends_on=[])]
        session.add(operation); session.flush()
        session.add(PublicationJournal(id="keep-journal", operation_item_id="keep-item", asset_id="keep-gap",
            pixiv_illust_id="70", intent_revision=1, action="publish", staging_path=".staging/keep", target_path="原路径/70_p1.png",
            verification={"record_id": "keep-gap", "derivative": False, "sha256": "b" * 64}))
        session.add(PublicationJournal(id="keep-delete", pixiv_illust_id="80", intent_revision=4, action="delete",
            staging_path="old/80.png", target_path=".recovery/80.deleted"))
    db.close()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("UPDATE alembic_version SET version_num='0002_client'"); connection.commit()
    return path


def test_upgrade_keeps_ids_original_paths_dates_preferences_pending_tasks_and_journals(tmp_path):
    source = client_database(tmp_path / "source.db")
    before = source.read_bytes()
    result = upgrade_0003(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert source.read_bytes() == before
    db = ArchiveDatabase(result["candidate"])
    with db.read() as session:
        work = session.get(Work, 7)
        assert work.pixiv_illust_id == "70" and work.time_basis == "legacy_unknown"
        assert work.legacy_posted_at == "2020-01-01 09:00:00"
        assert work.preference.is_hidden and work.preference.rating == 2 and work.bookmark.value is None
        revision = session.get(WorkRevision, work.default_revision_id)
        assert revision.page_count == 3 and revision.state == "partial"
        assert {asset.id: asset.relative_path for asset in revision_assets(session, revision)} == {
            "keep-original": "原路径/70_p0.png", "keep-gap": None}
        assert revision_derivatives(session, revision)[0].id == "keep-preview"
        target = session.get(OperationItem, "keep-item").target
        assert target["asset_id"] == "keep-gap" and target["revision"] == 1 and target["work_revision_id"] == revision.id
        assert session.get(PublicationJournal, "keep-journal").step == "prepared"
        assert session.get(PublicationJournal, "keep-delete").intent_revision == 4
        assert session.get(ArchiveIntent, "80").mode == "suppressed"
    db.close()
    assert result["validation"]["totals"]["works"] == 1


def test_activation_and_rollback_preserve_both_candidates(tmp_path):
    source = client_database(tmp_path / "source.db")
    result = upgrade_0003(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    with pytest.raises(RuntimeError): activate_upgrade(result)
    receipt = activate_upgrade(result, writers_stopped=True)
    db = ArchiveDatabase(source); db.close()
    assert receipt["retained"].exists() and result["candidate"].exists() and result["backup"].exists()
    recovered = rollback_upgrade(receipt, writers_stopped=True)
    assert recovered["retained_candidate"].exists()
    with closing(sqlite3.connect(source)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0002_client"
        assert connection.execute("SELECT relative_path FROM assets WHERE id='keep-original'").fetchone()[0] == "原路径/70_p0.png"
    with pytest.raises(UpgradeRequired): ArchiveDatabase(source)


def test_source_changes_or_candidate_corruption_block_activation(tmp_path):
    source = client_database(tmp_path / "source.db")
    result = upgrade_0003(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("UPDATE works SET title='new user write'"); connection.commit()
    with pytest.raises(UpgradeRequired, match="改变"): activate_upgrade(result, writers_stopped=True)
    with closing(sqlite3.connect(result["candidate"])) as connection:
        connection.execute("UPDATE assets SET relative_path='tampered'"); connection.commit()
    with pytest.raises(UpgradeRequired, match="核验失败"): validate_candidate(result["candidate"], result["backup"])
    with closing(sqlite3.connect(source)) as connection:
        assert connection.execute("SELECT title FROM works").fetchone()[0] == "new user write"


def test_new_write_after_activation_blocks_automatic_rollback(tmp_path):
    source = client_database(tmp_path / "source.db")
    result = upgrade_0003(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    receipt = activate_upgrade(result, writers_stopped=True)
    db = ArchiveDatabase(source)
    with db.write() as session:
        session.get(Work, 7).preference.rating = 5
    db.close()
    with pytest.raises(UpgradeRequired, match="已有写入"):
        rollback_upgrade(receipt, writers_stopped=True)
    db = ArchiveDatabase(source)
    with db.read() as session:
        assert session.get(Work, 7).preference.rating == 5
    db.close()
    assert receipt["retained"].exists() and result["backup"].exists()


def test_invalid_schema_or_existing_output_fails_before_copy(tmp_path):
    source = client_database(tmp_path / "source.db")
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("DROP TABLE operation_items"); connection.commit()
    with pytest.raises(UpgradeRequired): upgrade_0003(source, tmp_path / "candidate.db", tmp_path / "backup.db")
    assert not (tmp_path / "candidate.db").exists() and not (tmp_path / "backup.db").exists()
