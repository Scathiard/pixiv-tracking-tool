"""Explicit, non-destructive 0001_archive -> 0002_client SQLite upgrade.

Usage: python -m src.core.upgrade SOURCE --candidate NEW_DB --backup NEW_BACKUP
The source is never modified; activation of the candidate is a separate step.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from .database import ArchiveDatabase, SCHEMA_REVISION, UpgradeRequired
from .models import Base


OLD_REVISION = "0001_archive"
_NEW_ARTIST = {"biography", "avatar_path", "profile_refreshed_at"}
_NEW_BOOKMARK = {"source", "account_id", "version"}
_NEW_TABLES = {"application_state", "bookmark_operations"}


def _inspect_old(connection):
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise UpgradeRequired("源数据库完整性检查失败")
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "alembic_version" not in tables:
        raise UpgradeRequired("源数据库没有结构版本")
    revisions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    if revisions != [(OLD_REVISION,)]:
        raise UpgradeRequired("仅支持从 0001_archive 显式升级")
    for table in Base.metadata.sorted_tables:
        if table.name in _NEW_TABLES:
            continue
        if table.name not in tables:
            raise UpgradeRequired("源数据库缺少必要表")
        expected = {column.name for column in table.columns}
        if table.name == "artists":
            expected -= _NEW_ARTIST
        if table.name == "remote_bookmarks":
            expected -= _NEW_BOOKMARK
        actual = {row[1] for row in connection.execute(
            f'PRAGMA table_info("{table.name}")')}
        if not expected.issubset(actual):
            raise UpgradeRequired("源数据库缺少必要字段")


def _snapshot(source, target):
    # Exclusive creation protects a pre-existing backup/candidate, even if empty.
    with target.open("xb"):
        pass
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst)


def upgrade_0002(source, candidate, backup):
    source, candidate, backup = (Path(path).resolve() for path in
                                 (source, candidate, backup))
    if len({source, candidate, backup}) != 3:
        raise ValueError("源库、候选库和备份路径必须互不相同")
    if not source.is_file():
        raise FileNotFoundError(source)
    if candidate.exists() or backup.exists():
        raise FileExistsError("候选库或备份路径已存在")
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src:
        _inspect_old(src)
    backup.parent.mkdir(parents=True, exist_ok=True)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    _snapshot(source, backup)
    with sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True) as saved:
        _inspect_old(saved)
    _snapshot(backup, candidate)
    with sqlite3.connect(candidate) as connection:
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            for column, kind in (("biography", "TEXT"), ("avatar_path", "TEXT"),
                                 ("profile_refreshed_at", "DATETIME")):
                connection.execute(f"ALTER TABLE artists ADD COLUMN {column} {kind}")
            # Rebuild because 0001.value was NOT NULL; 0002 needs NULL for unknown.
            connection.execute("CREATE TABLE remote_bookmarks_next ("
                               "work_id INTEGER NOT NULL PRIMARY KEY REFERENCES works(id), "
                               "value BOOLEAN, confirmed_at DATETIME, error TEXT, "
                               "source VARCHAR NOT NULL DEFAULT 'legacy_cache', "
                               "account_id VARCHAR, version INTEGER NOT NULL DEFAULT 0)")
            connection.execute("INSERT INTO remote_bookmarks_next "
                               "(work_id,value,confirmed_at,error,source,account_id,version) "
                               "SELECT work_id,value,NULL,error,'legacy_cache',NULL,0 "
                               "FROM remote_bookmarks")
            connection.execute("DROP TABLE remote_bookmarks")
            connection.execute("ALTER TABLE remote_bookmarks_next RENAME TO remote_bookmarks")
            for name in _NEW_TABLES:
                table = Base.metadata.tables[name]
                from sqlalchemy.schema import CreateTable, CreateIndex
                from sqlalchemy.dialects import sqlite
                connection.execute(str(CreateTable(table).compile(dialect=sqlite.dialect())))
                for index in table.indexes:
                    connection.execute(str(CreateIndex(index).compile(dialect=sqlite.dialect())))
            connection.execute("UPDATE alembic_version SET version_num=?", (SCHEMA_REVISION,))
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise UpgradeRequired("候选库外键检查失败")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    db = ArchiveDatabase(candidate)
    db.close()
    with sqlite3.connect(f"file:{candidate.as_posix()}?mode=ro", uri=True) as check:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise UpgradeRequired("候选库完整性检查失败")
    return {"source": source, "candidate": candidate, "backup": backup}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    result = upgrade_0002(args.source, args.candidate, args.backup)
    print(f"候选库已核验：{result['candidate']}")
    print(f"源库快照已保留：{result['backup']}")


if __name__ == "__main__":
    main()
