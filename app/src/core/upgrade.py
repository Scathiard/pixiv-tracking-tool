"""Explicit snapshot/candidate/validation/activation/rollback for archive SQLite.

Source and original media are never edited during preparation. Activation and
rollback require the caller to stop every library writer first.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4
from urllib.parse import quote

from sqlalchemy.orm import selectinload

from .database import ArchiveDatabase, SCHEMA_REVISION, UpgradeRequired
from .models import Base, Work, Asset, Derivative, OperationItem
from .revisions import ensure_legacy_revision

OLD_REVISION = "0001_archive"
CLIENT_REVISION = "0002_client"
_REVISION_TABLES = {"work_revisions", "revision_assets", "revision_derivatives"}
_NEW_ARTIST = {"biography", "avatar_path", "profile_refreshed_at"}
_NEW_BOOKMARK = {"source", "account_id", "version"}
_NEW_TABLES = {"application_state", "bookmark_operations"}
_NEW_COLUMNS = {"works": {"default_revision_id", "latest_revision_id"},
    "assets": {"revision_id"}, "derivatives": {"revision_id"},
    "operations": {"pause_requested", "last_claim_seq"}, "operation_items": {"lane", "not_before"}}


@contextmanager
def _readonly(path):
    connection = sqlite3.connect("file:" + quote(Path(path).resolve().as_posix(), safe="/:") + "?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def _writable(path):
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _tables(connection):
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'") if not row[0].startswith("sqlite_")}


def _columns(connection, name):
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]


def _inspect_old(connection, expected=None):
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise UpgradeRequired("源数据库完整性检查失败")
    tables = _tables(connection)
    if "alembic_version" not in tables:
        raise UpgradeRequired("源数据库没有结构版本")
    versions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    allowed = (expected,) if expected else (OLD_REVISION, CLIENT_REVISION)
    if len(versions) != 1 or versions[0][0] not in allowed:
        raise UpgradeRequired("源数据库不是支持的旧结构版本")
    version = versions[0][0]
    for table in Base.metadata.sorted_tables:
        if table.name in _REVISION_TABLES or version == OLD_REVISION and table.name in _NEW_TABLES:
            continue
        if table.name not in tables:
            raise UpgradeRequired("源数据库缺少必要表")
        required = {column.name for column in table.columns} - _NEW_COLUMNS.get(table.name, set())
        if version == OLD_REVISION and table.name == "artists": required -= _NEW_ARTIST
        if version == OLD_REVISION and table.name == "remote_bookmarks": required -= _NEW_BOOKMARK
        if not required.issubset(_columns(connection, table.name)):
            raise UpgradeRequired("源数据库缺少必要字段")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise UpgradeRequired("源数据库外键检查失败")
    return version


def _snapshot(source, target):
    with Path(target).open("xb"):
        pass
    with _readonly(source) as src, _writable(target) as dst:
        src.backup(dst)


def _fingerprint(connection):
    digest = hashlib.sha256()
    for name in sorted(_tables(connection)):
        digest.update(name.encode())
        rows = connection.execute(f'SELECT * FROM "{name}"').fetchall()
        for row in sorted(rows, key=repr):
            digest.update(repr(row).encode("utf-8"))
    return digest.hexdigest()


def validate_candidate(candidate, backup=None):
    db = ArchiveDatabase(candidate); db.close()
    with _readonly(candidate) as check:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or check.execute("PRAGMA foreign_key_check").fetchone():
            raise UpgradeRequired("候选库完整性或外键检查失败")
        if check.execute("""SELECT count(*) FROM works w WHERE
                NOT EXISTS (SELECT 1 FROM work_revisions r WHERE r.id=w.latest_revision_id AND r.work_id=w.id)
                OR NOT EXISTS (SELECT 1 FROM work_revisions r WHERE r.id=w.default_revision_id AND r.work_id=w.id)""").fetchone()[0]:
            raise UpgradeRequired("候选库作品版本映射不完整")
        if check.execute("""SELECT count(*) FROM revision_assets b JOIN assets a ON a.id=b.asset_id
                JOIN work_revisions r ON r.id=b.revision_id
                WHERE a.work_id != r.work_id OR a.role != b.role OR a.page_index != b.page_index""").fetchone()[0]:
            raise UpgradeRequired("候选库原件版本归属不一致")
        if check.execute("""SELECT count(*) FROM revision_derivatives b JOIN derivatives d ON d.id=b.derivative_id
                JOIN work_revisions r ON r.id=b.revision_id WHERE d.work_id != r.work_id
                OR d.kind != b.kind OR d.page_index != b.page_index OR d.spec_key != b.spec_key""").fetchone()[0]:
            raise UpgradeRequired("候选库预览版本归属不一致")
        if check.execute("SELECT count(*) FROM assets a WHERE NOT EXISTS (SELECT 1 FROM revision_assets r WHERE r.asset_id=a.id)").fetchone()[0]:
            raise UpgradeRequired("候选库原件版本映射不完整")
        if check.execute("SELECT count(*) FROM derivatives d WHERE NOT EXISTS (SELECT 1 FROM revision_derivatives r WHERE r.derivative_id=d.id)").fetchone()[0]:
            raise UpgradeRequired("候选库预览版本映射不完整")
        totals = {name: check.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in sorted(_tables(check))}
        if backup:
            with _readonly(backup) as old:
                version = _inspect_old(old)
                for table in Base.metadata.sorted_tables:
                    name = table.name
                    if name in _REVISION_TABLES or name not in _tables(old): continue
                    columns = [column for column in _columns(old, name) if column in _columns(check, name)
                        and column not in _NEW_COLUMNS.get(name, set())]
                    if name == "remote_bookmarks" and version == OLD_REVISION:
                        columns = [column for column in columns if column not in ("confirmed_at", "source", "account_id", "version")]
                    if not columns: continue
                    clause = ",".join('"' + column + '"' for column in columns)
                    before = sorted(old.execute(f'SELECT {clause} FROM "{name}"').fetchall(), key=repr)
                    after = sorted(check.execute(f'SELECT {clause} FROM "{name}"').fetchall(), key=repr)
                    # Tasks preserve their payload and gain the stable revision pointer.
                    if name == "operation_items" and "target" in columns:
                        pos = columns.index("target")
                        clean = []
                        for row in after:
                            row = list(row); payload = json.loads(row[pos]); payload.pop("work_revision_id", None)
                            row[pos] = json.dumps(payload, sort_keys=True); clean.append(tuple(row))
                        after = sorted(clean, key=repr)
                        clean = []
                        for row in before:
                            row = list(row); payload = json.loads(row[pos]); payload.pop("work_revision_id", None)
                            row[pos] = json.dumps(payload, sort_keys=True); clean.append(tuple(row))
                        before = sorted(clean, key=repr)
                    if before != after:
                        raise UpgradeRequired(f"候选库保留数据核验失败：{name}")
        return {"schema_revision": SCHEMA_REVISION, "totals": totals}


def prepare_upgrade(source, candidate, backup, *, expected_revision=None):
    source, candidate, backup = (Path(path).resolve() for path in (source, candidate, backup))
    if len({source, candidate, backup}) != 3: raise ValueError("源库、候选库和备份路径必须互不相同")
    if not source.is_file(): raise FileNotFoundError(source)
    if candidate.exists() or backup.exists(): raise FileExistsError("候选库或备份路径已存在")
    with _readonly(source) as src:
        version = _inspect_old(src, expected_revision)
        # Preserve a historical fail-closed fixture/foreign table collision.
        collision = "remote_bookmarks_next" in _tables(src) and version == OLD_REVISION
    backup.parent.mkdir(parents=True, exist_ok=True); candidate.parent.mkdir(parents=True, exist_ok=True)
    _snapshot(source, backup)
    with _readonly(backup) as saved:
        _inspect_old(saved, version); source_fingerprint = _fingerprint(saved)
    if collision:
        _snapshot(backup, candidate)
        raise sqlite3.OperationalError("candidate table collision")
    db = ArchiveDatabase.create(candidate); db.close()
    with _readonly(backup) as saved, _writable(candidate) as destination:
        try:
            destination.execute("PRAGMA foreign_keys=OFF")
            destination.execute("BEGIN IMMEDIATE")
            for table in Base.metadata.sorted_tables:
                name = table.name
                if name in _REVISION_TABLES or name not in _tables(saved): continue
                columns = [column.name for column in table.columns if column.name in _columns(saved, name)
                    and column.name not in _NEW_COLUMNS.get(name, set())]
                clause = ",".join('"'+column+'"' for column in columns)
                rows = saved.execute(f'SELECT {clause} FROM "{name}"').fetchall()
                if name == "remote_bookmarks" and version == OLD_REVISION:
                    rows = [tuple(None if column == "confirmed_at" else "legacy_cache" if column == "source" else 0 if column == "version" else value
                        for column, value in zip(columns, row)) for row in rows]
                defaults = {column.name: column.default.arg for column in table.columns if column.name not in columns
                    and not column.nullable and column.default is not None and not callable(column.default.arg)}
                insert_columns = columns + list(defaults)
                insert_clause = ",".join('"'+column+'"' for column in insert_columns)
                placeholders = ",".join("?" for column in insert_columns)
                destination.executemany(f'INSERT INTO "{name}" ({insert_clause}) VALUES ({placeholders})', [(*row, *defaults.values()) for row in rows])
            destination.commit()
        except BaseException:
            destination.rollback(); raise
    db = ArchiveDatabase(candidate)
    try:
        with db.write() as session:
            revisions = {}
            for work in session.query(Work).options(selectinload(Work.assets), selectinload(Work.derivatives),
                    selectinload(Work.tags)).order_by(Work.id).yield_per(100):
                revision = ensure_legacy_revision(session, work)
                revisions[work.id] = revision.id
            for item in session.query(OperationItem).yield_per(200):
                payload = item.target or {}
                work_id = payload.get("work_id")
                if work_id in revisions:
                    item.target = {**payload, "work_revision_id": revisions[work_id]}
            # Keep lane unset: queue derives old kinds exactly as it does new work.
    finally:
        db.close()
    validation = validate_candidate(candidate, backup)
    return {"source": source, "candidate": candidate, "backup": backup,
        "source_revision": version, "source_fingerprint": source_fingerprint, "validation": validation}


def upgrade_0002(source, candidate, backup):
    return prepare_upgrade(source, candidate, backup, expected_revision=OLD_REVISION)


def upgrade_0003(source, candidate, backup):
    return prepare_upgrade(source, candidate, backup, expected_revision=CLIENT_REVISION)


def activate_upgrade(result, *, writers_stopped=False):
    if not writers_stopped: raise RuntimeError("启用前必须关闭图库写入者")
    source, candidate, backup = (Path(result[key]).resolve() for key in ("source", "candidate", "backup"))
    validate_candidate(candidate, backup)
    with _readonly(source) as old:
        if _fingerprint(old) != result["source_fingerprint"]:
            raise UpgradeRequired("源库在准备后已改变，候选不能启用")
    # Close and checkpoint the source only after the caller explicitly quiesces it.
    with _writable(source) as old:
        if old.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            raise UpgradeRequired("源库仍被使用，未启用候选")
    staged = source.with_name(source.name + ".activation-" + uuid4().hex)
    retained = source.with_name(source.name + ".previous-" + uuid4().hex)
    _snapshot(candidate, staged)
    os.replace(source, retained)
    try:
        os.replace(staged, source)
        db = ArchiveDatabase(source); db.close()
    except BaseException:
        if source.exists(): os.replace(source, source.with_name(source.name + ".failed-" + uuid4().hex))
        os.replace(retained, source)
        raise
    with _readonly(source) as active:
        active_fingerprint = _fingerprint(active)
    return {**result, "active": source, "retained": retained, "active_fingerprint": active_fingerprint}


def rollback_upgrade(receipt, *, writers_stopped=False):
    if not writers_stopped: raise RuntimeError("回退前必须关闭图库写入者")
    active, backup = Path(receipt["active"]).resolve(), Path(receipt["backup"]).resolve()
    with _readonly(backup) as old: _inspect_old(old)
    with _readonly(active) as current:
        if not receipt.get("active_fingerprint") or _fingerprint(current) != receipt["active_fingerprint"]:
            raise UpgradeRequired("新版库启用后已有写入，保留当前库与旧备份，不能自动回退")
    staged = active.with_name(active.name + ".rollback-" + uuid4().hex)
    failed = active.with_name(active.name + ".failed-" + uuid4().hex)
    _snapshot(backup, staged)
    with _writable(active) as current:
        if current.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            raise UpgradeRequired("新版库仍被使用，未执行回退")
    os.replace(active, failed)
    try: os.replace(staged, active)
    except BaseException:
        os.replace(failed, active); raise
    return {"active": active, "retained_candidate": failed, "backup": backup}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    result = prepare_upgrade(args.source, args.candidate, args.backup)
    print(f"候选库已核验：{result['candidate']}")
    print(f"源库快照已保留：{result['backup']}")


if __name__ == "__main__": main()
