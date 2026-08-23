import sqlite3
from pathlib import Path

import pytest

from src.data_migration import (
    MigrationError,
    execute_migration,
    persist_data_root,
    prepare_migration,
)


def _build_source(root: Path):
    data_dir = root / "data"
    images_dir = root / "images" / "Artist 1"
    data_dir.mkdir(parents=True)
    images_dir.mkdir(parents=True)

    db_path = data_dir / "pixiv_tracker.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO sample(value) VALUES ('kept')")
    (data_dir / "pixiv_token.json").write_text('{"token":"kept"}', encoding="utf-8")
    (images_dir / "100_p0.jpg").write_bytes(b"image-data")
    return db_path


def test_prepare_migration_requires_empty_non_nested_target(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _build_source(source)

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "existing.txt").write_text("x", encoding="utf-8")

    with pytest.raises(MigrationError, match="空目录"):
        prepare_migration(source, nonempty)
    with pytest.raises(MigrationError, match="内部"):
        prepare_migration(source, source / "nested")


def test_execute_migration_uses_sqlite_snapshot_and_keeps_source(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    source_db = _build_source(source)
    target = tmp_path / "target"

    plan = prepare_migration(source, target)
    report = execute_migration(plan)

    target_db = target / "data" / "pixiv_tracker.db"
    with sqlite3.connect(target_db) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT value FROM sample").fetchone()[0] == "kept"

    assert source_db.exists()
    assert (source / "images" / "Artist 1" / "100_p0.jpg").exists()
    assert (target / "data" / "pixiv_token.json").exists()
    assert (target / "images" / "Artist 1" / "100_p0.jpg").read_bytes() == b"image-data"
    assert report.database_ok
    assert report.image_files == 1
    assert report.image_bytes == len(b"image-data")


def test_persist_data_root_replaces_only_data_root_atomically(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("PORT=1919\nDATA_ROOT=old\nCHECK_INTERVAL_HOURS=6\n", encoding="utf-8")
    new_root = tmp_path / "new data"

    persist_data_root(env_file, new_root)

    assert env_file.read_text(encoding="utf-8") == (
        f"PORT=1919\nDATA_ROOT={new_root.resolve()}\nCHECK_INTERVAL_HOURS=6\n"
    )
    assert not (tmp_path / ".env.tmp").exists()


def test_execute_migration_detects_corrupt_copied_file(monkeypatch, tmp_path: Path):
    from src import data_migration

    source = tmp_path / "source"
    source.mkdir()
    _build_source(source)
    target = tmp_path / "target"
    real_copy2 = data_migration.shutil.copy2

    def corrupt_images(src, dst, *args, **kwargs):
        result = real_copy2(src, dst, *args, **kwargs)
        if str(dst).endswith("100_p0.jpg"):
            Path(dst).write_bytes(b"bad")
        return result

    monkeypatch.setattr(data_migration.shutil, "copy2", corrupt_images)

    with pytest.raises(MigrationError, match="图片文件清单"):
        execute_migration(prepare_migration(source, target))
