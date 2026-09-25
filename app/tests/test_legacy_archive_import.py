import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import select

from src.core.database import ArchiveDatabase
from src.core.legacy_import import import_legacy
from src.core.models import ArchiveIntent, Asset, TrackingPolicy, Work


def legacy_fixture(root: Path):
    source = root / "legacy.db"
    with sqlite3.connect(source) as connection:
        connection.executescript("""
            CREATE TABLE tracked_artist (
                id INTEGER PRIMARY KEY, pixiv_user_id TEXT, name TEXT,
                is_active INTEGER, added_at TEXT
            );
            CREATE TABLE illustration (
                id INTEGER PRIMARY KEY, pixiv_illust_id TEXT, artist_id INTEGER,
                title TEXT, type TEXT, page_count INTEGER, tags TEXT,
                caption TEXT, posted_at TEXT, file_paths TEXT,
                is_hidden INTEGER, rating INTEGER, is_bookmarked INTEGER,
                x_restrict INTEGER
            );
            CREATE TABLE illustration_tag (illustration_id INTEGER, tag TEXT);
            INSERT INTO tracked_artist VALUES (7, '70', 'Artist, A', 0, '2026-01-02 03:04:05');
        """)
        connection.execute(
            "INSERT INTO illustration VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (8, "80", 7, "Multiple pages", "manga", 3, '["blue","blue"]',
             '{"status":"empty"}', "2026-01-01 12:00:00",
             json.dumps(["/images/Artist, A 70/80_p0.jpg", "/images/Artist, A 70/80_p2.png"]),
             1, 4, 1, 0),
        )
        connection.execute(
            "INSERT INTO illustration VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (9, "90", 7, "Animation", "ugoira", 1, '[]', "", None,
             '["/images/Artist, A 70/90.gif"]', 0, 0, 0, 0),
        )
        connection.execute("INSERT INTO illustration_tag VALUES (8, 'green')")
    deleted = root / "deleted.json"
    deleted.write_text('["80","999"]', encoding="utf-8")
    return source, deleted


def test_import_preserves_identity_preferences_missing_pages_and_old_animation(tmp_path):
    source, deleted = legacy_fixture(tmp_path)
    before = source.read_bytes()
    target = tmp_path / "candidate.db"
    report = import_legacy(source, target, deleted_manifest=deleted)
    assert report.ready_for_review
    assert (report.artists, report.works, report.assets, report.suppressed) == (1, 2, 5, 2)
    assert source.read_bytes() == before
    db = ArchiveDatabase(target)
    try:
        with db.read() as session:
            work = session.get(Work, 8)
            assert work.pixiv_illust_id == "80" and work.artist_id == 7
            assert work.preference.is_hidden and work.preference.rating == 4
            assert work.bookmark.value and work.bookmark.confirmed_at is None
            assert {tag.tag for tag in work.tags} == {"blue", "green"}
            assert work.time_basis == "legacy_unknown"
            assert work.legacy_posted_at == "2026-01-01 12:00:00"
            assert work.caption_state == "empty"
            assert not work.restriction_verified
            pages = {asset.page_index: asset for asset in work.assets}
            assert pages[0].relative_path == "Artist, A 70/80_p0.jpg"
            assert pages[0].state == "unknown"
            assert pages[1].relative_path is None and pages[1].state == "missing"
            assert pages[2].relative_path == "Artist, A 70/80_p2.png"
            animation = session.get(Work, 9)
            assert animation.source_metadata["original_backfill_requires_selection"]
            assert animation.derivatives[0].relative_path.endswith("90.gif")
            assert session.get(TrackingPolicy, 7).mode == "paused"
            assert session.get(ArchiveIntent, "999").mode == "suppressed"
            assert session.get(ArchiveIntent, "80").mode == "suppressed"
    finally:
        db.close()


def test_existing_candidate_cannot_be_overwritten(tmp_path):
    source, _ = legacy_fixture(tmp_path)
    target = tmp_path / "candidate.db"
    import_legacy(source, target)
    before = target.read_bytes()
    with pytest.raises(FileExistsError):
        import_legacy(source, target)
    assert target.read_bytes() == before


def test_unsafe_or_ambiguous_paths_block_activation_review(tmp_path):
    source, _ = legacy_fixture(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE illustration SET file_paths=? WHERE id=8", ('["/images/../secret.jpg"]',))
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"outside")
    report = import_legacy(source, tmp_path / "candidate.db", media_root=tmp_path / "images")
    assert report.completed and not report.ready_for_review
    assert report.errors == [{"work": 8, "code": "unsafe_media_path"}]
    assert secret.read_bytes() == b"outside"


def test_bad_tombstones_fail_before_creating_candidate(tmp_path):
    source, deleted = legacy_fixture(tmp_path)
    deleted.write_text('{"unexpected":true}', encoding="utf-8")
    target = tmp_path / "candidate.db"
    with pytest.raises(ValueError, match="删除阻止"):
        import_legacy(source, target, deleted_manifest=deleted)
    assert not target.exists()


def test_failed_import_rolls_back_all_imported_entities(tmp_path):
    source, _ = legacy_fixture(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE illustration SET artist_id=999 WHERE id=9")
    target = tmp_path / "candidate.db"
    with pytest.raises(ValueError, match="缺少画师关系"):
        import_legacy(source, target)
    db = ArchiveDatabase(target)
    try:
        with db.read() as session:
            assert session.scalars(select(Work)).all() == []
            assert session.scalars(select(Asset)).all() == []
        report = json.loads(target.with_suffix('.db.import.json').read_text(encoding='utf-8'))
        assert not report['completed'] and not report['ready_for_review']
    finally:
        db.close()
