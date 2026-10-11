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

from sqlalchemy.orm import Session, selectinload

from .database import ArchiveDatabase, SCHEMA_REVISION, UpgradeRequired
from .models import Base, Work, Asset, Derivative, OperationItem
from .revisions import ensure_legacy_revision

OLD_REVISION = "0001_archive"
CLIENT_REVISION = "0002_client"
REVISIONS_REVISION = "0003_revisions"
READ_PROJECTIONS_REVISION = "0004_read_projections"
_SUPPORTED_SOURCES = (OLD_REVISION, CLIENT_REVISION, REVISIONS_REVISION, READ_PROJECTIONS_REVISION)
_REVISION_TABLES = {"work_revisions", "revision_assets", "revision_derivatives"}
_NEW_ARTIST = {"biography", "avatar_path", "profile_refreshed_at"}
_NEW_BOOKMARK = {"source", "account_id", "version"}
_NEW_TABLES = {"application_state", "bookmark_operations"}
_NEW_COLUMNS = {"works": {"default_revision_id", "latest_revision_id"},
    "assets": {"revision_id"}, "derivatives": {"revision_id"},
    "operations": {"pause_requested", "last_claim_seq"}, "operation_items": {"lane", "not_before"}}


class UpgradeRecoveryRequired(UpgradeRequired):
    """A file switch failed and automatic restoration could not be completed."""

    def __init__(self, active, retained=None, *, intent=None):
        self.active = Path(active)
        self.retained = Path(retained) if retained else None
        self.intent = intent
        super().__init__("图库切换记录需要恢复核验，请关闭程序后检查恢复记录与保留的文件")


def _journal_path(source):
    source = Path(source).resolve()
    return source.with_name(source.name + ".upgrade-journal.jsonl")


def _journal_state(source):
    """Inspect the durable switch record for this exact resolved database path."""
    source = Path(source).resolve()
    journal = _journal_path(source)
    if journal.is_symlink():
        raise UpgradeRecoveryRequired(source)
    if not journal.exists():
        return None
    current = None
    try:
        with journal.open("r", encoding="utf-8") as records:
            for line in records:
                record = json.loads(line)
                if (not isinstance(record, dict) or record.get("format") != 1
                        or record.get("source") != str(source)
                        or record.get("action") not in ("activate", "rollback")
                        or not isinstance(record.get("attempt"), str)):
                    raise ValueError("invalid upgrade journal ownership")
                for key in ("staged", "retained", "backup"):
                    if not isinstance(record.get(key), str) or not Path(record[key]).is_absolute():
                        raise ValueError("invalid upgrade recovery path")
                if "candidate" in record and (not isinstance(record["candidate"], str) or not Path(record["candidate"]).is_absolute()):
                    raise ValueError("invalid upgrade candidate path")
                state = record.get("state")
                if state == "pending":
                    if current is not None and current.get("state") == "pending":
                        raise ValueError("unresolved preceding upgrade intent")
                elif state in ("activated", "restored", "rolled_back"):
                    if (current is None or current.get("state") != "pending"
                            or current.get("attempt") != record.get("attempt")
                            or current.get("action") != record.get("action")):
                        raise ValueError("upgrade completion has no matching intent")
                    if state == "activated" and record["action"] != "activate" or state == "rolled_back" and record["action"] != "rollback":
                        raise ValueError("upgrade completion action mismatch")
                else:
                    raise ValueError("invalid upgrade journal state")
                current = record
        if current is None:
            raise ValueError("empty upgrade journal")
        return current
    except (OSError, UnicodeError, ValueError) as error:
        raise UpgradeRecoveryRequired(source) from error


def guard_upgrade_recovery(source):
    """Fail closed before open/create when an earlier file switch is unresolved."""
    source = Path(source).resolve()
    record = _journal_state(source)
    if record is not None and (record.get("state") == "pending" or not source.is_file()):
        raise UpgradeRecoveryRequired(source, record.get("retained"), intent=record)
    if record is None and not source.exists() and source.parent.is_dir():
        # Older builds did not have a durable journal. Their retained switch
        # files still prevent a missing library from becoming an empty install.
        prefixes = tuple(source.name + suffix for suffix in (".previous-", ".failed-", ".activation-", ".rollback-"))
        for sibling in source.parent.iterdir():
            if sibling.name.startswith(prefixes):
                raise UpgradeRecoveryRequired(source, sibling)


def _append_journal(source, record):
    """Keep intent/history; fsync before any rename rather than deleting a marker."""
    journal = _journal_path(source)
    if journal.is_symlink():
        raise UpgradeRecoveryRequired(source)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(journal, flags, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        output.flush(); os.fsync(output.fileno())
    # Windows does not expose a portable directory fsync. File fsync is still
    # mandatory there; POSIX also persists the newly created directory entry.
    if os.name != "nt":
        directory = os.open(journal.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _begin_switch(source, *, action, staged, retained, backup, candidate=None):
    guard_upgrade_recovery(source)
    record = {"format": 1, "attempt": uuid4().hex, "source": str(Path(source).resolve()),
        "action": action, "state": "pending", "staged": str(Path(staged).resolve()),
        "retained": str(Path(retained).resolve()), "backup": str(Path(backup).resolve())}
    if candidate is not None:
        record["candidate"] = str(Path(candidate).resolve())
    _append_journal(source, record)
    return record


def _finish_switch(source, intent, state):
    _append_journal(source, {**intent, "state": state})


@contextmanager
def _readonly(path):
    connection = sqlite3.connect("file:" + quote(Path(path).resolve().as_posix(), safe="/:") + "?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
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
    return [row[1] for row in connection.execute(f'PRAGMA table_info({_identifier(name)})')]


def _identifier(name):
    return '"' + name.replace('"', '""') + '"'


def _inspect_old(connection, expected=None):
    tables = _tables(connection)
    if "alembic_version" not in tables:
        raise UpgradeRequired("源数据库没有结构版本")
    versions = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    allowed = (expected,) if expected in _SUPPORTED_SOURCES else _SUPPORTED_SOURCES if expected is None else ()
    if len(versions) != 1 or versions[0][0] not in allowed:
        raise UpgradeRequired("源数据库不是支持的旧结构版本")
    version = versions[0][0]
    # 0004 upgrades copy source data/indexes unchanged and only add read tables.
    # Index-to-row integrity walks cause random HDD reads for every old index;
    # the copied source fingerprint and new projection mapping provide the
    # relevant invariants here. Keep full checks for actual domain conversions.
    check = "quick_check" if version == READ_PROJECTIONS_REVISION else "integrity_check"
    if connection.execute("PRAGMA " + check).fetchone()[0] != "ok":
        raise UpgradeRequired("源数据库完整性检查失败")
    for table in Base.metadata.sorted_tables:
        if version not in (REVISIONS_REVISION, READ_PROJECTIONS_REVISION) and table.name in _REVISION_TABLES or version == OLD_REVISION and table.name in _NEW_TABLES:
            continue
        if table.name not in tables:
            raise UpgradeRequired("源数据库缺少必要表")
        required = {column.name for column in table.columns}
        if version not in (REVISIONS_REVISION, READ_PROJECTIONS_REVISION):
            required -= _NEW_COLUMNS.get(table.name, set())
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
    for row in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"):
        digest.update(repr(tuple(row)).encode("utf-8"))
    for name in sorted(_preserved_tables(connection)):
        digest.update(name.encode())
        digest.update(_table_fingerprint(connection, name).encode())
    return digest.hexdigest()


def _preserved_tables(connection):
    # AUTOINCREMENT high-water marks are source facts too, even if deleted IDs
    # leave no corresponding row. Snapshot upgrades keep SQLite's own tables.
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _table_fingerprint(connection, name):
    """Hash exact stored values without retaining a whole source table in Python."""
    digest = hashlib.sha256()
    columns = _columns(connection, name)
    rowid = _rowid_alias(connection, name, columns)
    if rowid is not None:
        columns = [rowid, *columns]
    clause = ",".join(_identifier(column) for column in columns)
    for row in connection.execute(f'SELECT {clause} FROM {_identifier(name)} ORDER BY {clause}'):
        value = repr(tuple(row)).encode("utf-8")
        digest.update(len(value).to_bytes(8, "big")); digest.update(value)
    return digest.hexdigest()


def _rowid_alias(connection, name, columns):
    details = next(row for row in connection.execute("PRAGMA table_list") if row[0] == "main" and row[1] == name)
    if details[4]:  # WITHOUT ROWID: declared primary-key values are already hashed.
        return None
    # Generated/hidden columns can shadow an alias too, although table_info
    # omits them from the stored-value selection above.
    names = {row[1].casefold() for row in connection.execute(f"PRAGMA table_xinfo({_identifier(name)})")}
    for alias in ("_rowid_", "rowid", "oid"):
        if alias not in names:
            return alias
    # An INTEGER PRIMARY KEY aliases the hidden identity even if the customary
    # names are all shadowed. A separate PK index distinguishes DESC/non-alias
    # keys. Otherwise SQLite cannot expose this rowid through its SQL API.
    info = list(connection.execute(f"PRAGMA table_info({_identifier(name)})"))
    primary = [row for row in info if row[5]]
    separate_primary = any(row[3] == "pk" for row in connection.execute(f"PRAGMA index_list({_identifier(name)})"))
    if len(primary) == 1 and primary[0][2].strip().upper() == "INTEGER" and not separate_primary:
        return None
    raise UpgradeRequired("源表隐藏行身份无法安全核验，原库未转换")


def _validate_revision_links(connection, *, require_complete=False):
    # Tolerated 0003 pointers and cross-work/logical resource reuse are source
    # facts, not retroactive migration invariants. Surface counts; never repair.
    warnings = {}
    predicate = "" if require_complete else "w.latest_revision_id IS NOT NULL AND "
    default_predicate = "" if require_complete else "w.default_revision_id IS NOT NULL AND "
    pointers = connection.execute(f"""SELECT count(*) FROM works w WHERE
            ({predicate}NOT EXISTS (SELECT 1 FROM work_revisions r WHERE r.id=w.latest_revision_id AND r.work_id=w.id))
            OR ({default_predicate}NOT EXISTS (SELECT 1 FROM work_revisions r WHERE r.id=w.default_revision_id AND r.work_id=w.id))""").fetchone()[0]
    if pointers and require_complete:
        raise UpgradeRequired("候选库作品版本映射不完整")
    if pointers:
        warnings["non_owned_revision_pointers"] = pointers
    physical_owners = 0
    binding_owners = 0
    binding_remaps = 0
    for table in ("assets", "derivatives"):
        physical_owners += connection.execute(f"""SELECT count(*) FROM {table} a JOIN work_revisions r ON r.id=a.revision_id
                WHERE a.work_id != r.work_id""").fetchone()[0]
    if physical_owners and require_complete:
        raise UpgradeRequired("候选库物理资源版本归属不一致")
    if physical_owners:
        warnings["non_owned_physical_revision_pointers"] = physical_owners
    for bindings, physical, field in (("revision_assets", "assets", "asset_id"),
                                      ("revision_derivatives", "derivatives", "derivative_id")):
        base = f"FROM {bindings} b JOIN {physical} a ON a.id=b.{field} JOIN work_revisions r ON r.id=b.revision_id"
        owners = connection.execute(f"SELECT count(*) {base} WHERE a.work_id != r.work_id").fetchone()[0]
        remapped = "a.role != b.role OR a.page_index != b.page_index" if physical == "assets" else "a.kind != b.kind OR a.page_index != b.page_index OR a.spec_key != b.spec_key"
        remaps = connection.execute(f"SELECT count(*) {base} WHERE {remapped}").fetchone()[0]
        if require_complete and (owners or remaps):
            raise UpgradeRequired("候选库资源版本归属不一致")
        binding_owners += owners; binding_remaps += remaps
        if require_complete and connection.execute(f"SELECT count(*) FROM {physical} a WHERE NOT EXISTS (SELECT 1 FROM {bindings} b WHERE b.{field}=a.id)").fetchone()[0]:
            raise UpgradeRequired("候选库资源版本映射不完整")
    if binding_owners:
        warnings["cross_work_resource_bindings"] = binding_owners
    if binding_remaps:
        warnings["logical_resource_remaps"] = binding_remaps
    return warnings


def _verify_preserved_revisions(check, old):
    """Copied upgrades preserve source facts and pre-existing schema objects."""
    source_revision = old.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    actual = {(row[0], row[1]): tuple(row) for row in check.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master")}
    for row in old.execute("SELECT type,name,tbl_name,sql FROM sqlite_master"):
        if actual.get((row[0], row[1])) != tuple(row):
            raise UpgradeRequired(f"候选库保留结构核验失败：{row[1]}")
    for name in sorted(_preserved_tables(old)):
        if name == "alembic_version" or (source_revision == READ_PROJECTIONS_REVISION
                and name == "read_operation_scope"):
            # Membership is a derived task lookup. 0005 reconstructs it from
            # source parameters once; operation/item records remain identical.
            continue
        if _columns(old, name) != _columns(check, name) or _table_fingerprint(old, name) != _table_fingerprint(check, name):
            raise UpgradeRequired(f"候选库保留数据核验失败：{name}")


def validate_candidate(candidate, backup=None):
    """Read-only verification. Validation never installs or repairs a projection."""
    source_version = None
    if backup:
        with _readonly(backup) as old:
            source_version = old.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    check_kind = "quick_check" if source_version == READ_PROJECTIONS_REVISION else "integrity_check"
    with _readonly(candidate) as check:
        if check.execute("PRAGMA " + check_kind).fetchone()[0] != "ok" or check.execute("PRAGMA foreign_key_check").fetchone():
            raise UpgradeRequired("候选库完整性或外键检查失败")
        versions = check.execute("SELECT version_num FROM alembic_version").fetchall()
        if versions != [(SCHEMA_REVISION,)]:
            raise UpgradeRequired("候选库结构版本不符，未启用")
        for table in Base.metadata.sorted_tables:
            if not {column.name for column in table.columns}.issubset(_columns(check, table.name)):
                raise UpgradeRequired("候选库结构或字段不完整，未启用")
        if backup:
            with _readonly(backup) as old:
                source_version = _inspect_old(old)
                if source_version in (REVISIONS_REVISION, READ_PROJECTIONS_REVISION):
                    _verify_preserved_revisions(check, old)
                else:
                    for table in Base.metadata.sorted_tables:
                        name = table.name
                        if name in _REVISION_TABLES or name not in _tables(old): continue
                        columns = [column for column in _columns(old, name) if column in _columns(check, name)
                            and column not in _NEW_COLUMNS.get(name, set())]
                        if name == "remote_bookmarks" and source_version == OLD_REVISION:
                            columns = [column for column in columns if column not in ("confirmed_at", "source", "account_id", "version")]
                        if not columns: continue
                        clause = ",".join(_identifier(column) for column in columns)
                        before = sorted(old.execute(f'SELECT {clause} FROM {_identifier(name)}').fetchall(), key=repr)
                        after = sorted(check.execute(f'SELECT {clause} FROM {_identifier(name)}').fetchall(), key=repr)
                        # Only pre-revisions tasks gain the stable revision pointer.
                        if name == "operation_items" and "target" in columns:
                            pos = columns.index("target")
                            def normalized(rows):
                                values = []
                                for row in rows:
                                    row = list(row); payload = json.loads(row[pos]); payload.pop("work_revision_id", None)
                                    row[pos] = json.dumps(payload, sort_keys=True); values.append(tuple(row))
                                return sorted(values, key=repr)
                            before, after = normalized(before), normalized(after)
                        if before != after:
                            raise UpgradeRequired(f"候选库保留数据核验失败：{name}")
        warnings = _validate_revision_links(check, require_complete=source_version in (OLD_REVISION, CLIENT_REVISION))
        totals = {name: check.execute(f'SELECT count(*) FROM {_identifier(name)}').fetchone()[0] for name in sorted(_tables(check))}
    # A verification bug must not silently repair a candidate. query_only fences
    # this standalone session as well as the SQLite mode=ro source checks above.
    from .projection_maintenance import verify_projections, validate_projection_schema
    engine = ArchiveDatabase._engine(Path(candidate).resolve())
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=ON")
            connection.exec_driver_sql("BEGIN")
            with Session(bind=connection) as session:
                try:
                    if source_version == READ_PROJECTIONS_REVISION:
                        # Existing resource/order/count projections are copied
                        # unchanged. Validate only the newly derived structures;
                        # do not replay every historical task for every Work.
                        from . import gallery_card_projection, normalized_task_projection
                        from .read_projection import require_ready
                        validate_projection_schema(connection)
                        generation = require_ready(connection)
                        projection = {"generation": generation,
                            "cards": gallery_card_projection.verify(connection),
                            "tasks": normalized_task_projection.verify(connection)}
                    else:
                        projection = verify_projections(session)
                except Exception as error:
                    raise UpgradeRequired("候选库读取投影核验失败，未启用") from error
    finally:
        engine.dispose()
    return {"schema_revision": SCHEMA_REVISION, "totals": totals, "projection": projection,
        "warnings": warnings, "database_check": check_kind}


def _finish_projection_candidate(candidate, *, install=False, convert_legacy=False):
    """Explicit candidate-only DDL/backfill; source rows and readiness commit together."""
    from .projection_maintenance import install_projections, rebuild_projections, verify_projections
    engine = ArchiveDatabase._engine(Path(candidate).resolve())
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            if install:
                install_projections(connection)
            with Session(bind=connection) as session:
                if convert_legacy:
                    revisions = {}
                    for work in session.query(Work).options(selectinload(Work.assets), selectinload(Work.derivatives),
                            selectinload(Work.tags)).order_by(Work.id).yield_per(100):
                        revision = ensure_legacy_revision(session, work, raw=True)
                        revisions[work.id] = revision.id
                    for item in session.query(OperationItem).yield_per(200):
                        payload = item.target or {}
                        work_id = payload.get("work_id")
                        if work_id in revisions:
                            item.target = {**payload, "work_revision_id": revisions[work_id]}
                    # Keep lane unset: queue derives old kinds exactly as before.
                    session.flush()
                rebuild_projections(session)
                verify_projections(session)
                connection.exec_driver_sql("UPDATE alembic_version SET version_num=?", (SCHEMA_REVISION,))
    finally:
        engine.dispose()


def _finish_read_candidate(candidate):
    """0004 -> 0005 adds narrow reads without rebuilding old source projections."""
    from . import gallery_card_projection, normalized_task_projection
    from .read_projection import require_ready
    engine = ArchiveDatabase._engine(Path(candidate).resolve())
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            require_ready(connection)
            gallery_card_projection.install(connection)
            normalized_task_projection.install(connection)
            with Session(bind=connection) as session:
                gallery_card_projection.backfill(session)
                normalized_task_projection.backfill(connection)
            gallery_card_projection.verify(connection)
            normalized_task_projection.verify(connection)
            connection.exec_driver_sql("UPDATE alembic_version SET version_num=?", (SCHEMA_REVISION,))
    finally:
        engine.dispose()


def prepare_upgrade(source, candidate, backup, *, expected_revision=None):
    source, candidate, backup = (Path(path).resolve() for path in (source, candidate, backup))
    if len({source, candidate, backup}) != 3: raise ValueError("源库、候选库和备份路径必须互不相同")
    guard_upgrade_recovery(source)
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
    if version in (REVISIONS_REVISION, READ_PROJECTIONS_REVISION):
        # Snapshot-copy, rather than reconstruct, preserves unknown source tables,
        # logical bindings, existing indexes, and the exact task target JSON.
        _snapshot(backup, candidate)
        if version == READ_PROJECTIONS_REVISION:
            _finish_read_candidate(candidate)
        else:
            _finish_projection_candidate(candidate, install=True)
        validation = validate_candidate(candidate, backup)
        return {"source": source, "candidate": candidate, "backup": backup,
            "source_revision": version, "source_fingerprint": source_fingerprint, "validation": validation}
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
    _finish_projection_candidate(candidate, convert_legacy=True)
    validation = validate_candidate(candidate, backup)
    return {"source": source, "candidate": candidate, "backup": backup,
        "source_revision": version, "source_fingerprint": source_fingerprint, "validation": validation}


def upgrade_0002(source, candidate, backup):
    return prepare_upgrade(source, candidate, backup, expected_revision=OLD_REVISION)


def upgrade_0003(source, candidate, backup):
    return prepare_upgrade(source, candidate, backup, expected_revision=CLIENT_REVISION)


def upgrade_0004(source, candidate, backup):
    return prepare_upgrade(source, candidate, backup, expected_revision=REVISIONS_REVISION)


def upgrade_0005(source, candidate, backup):
    return prepare_upgrade(source, candidate, backup, expected_revision=READ_PROJECTIONS_REVISION)


def activate_upgrade(result, *, writers_stopped=False):
    if not writers_stopped: raise RuntimeError("启用前必须关闭图库写入者")
    source, candidate, backup = (Path(result[key]).resolve() for key in ("source", "candidate", "backup"))
    guard_upgrade_recovery(source)
    validate_candidate(candidate, backup)
    with _readonly(backup) as saved:
        if _fingerprint(saved) != result["source_fingerprint"]:
            raise UpgradeRequired("源库备份在准备后已改变，候选不能启用")
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
    validate_candidate(staged, backup)
    intent = _begin_switch(source, action="activate", staged=staged, retained=retained,
        backup=backup, candidate=candidate)
    moved_source = False
    try:
        os.replace(source, retained)
        moved_source = True
        os.replace(staged, source)
        db = ArchiveDatabase(source); db.close()
        with _readonly(source) as active:
            active_fingerprint = _fingerprint(active)
        _finish_switch(source, intent, "activated")
    except BaseException:
        try:
            if moved_source:
                if source.exists(): os.replace(source, source.with_name(source.name + ".failed-" + uuid4().hex))
                os.replace(retained, source)
            with _readonly(source) as restored:
                if _fingerprint(restored) != result["source_fingerprint"]:
                    raise UpgradeRequired("恢复后的源库与准备快照不符")
            _finish_switch(source, intent, "restored")
        except BaseException as restore_error:
            raise UpgradeRecoveryRequired(source, retained, intent=intent) from restore_error
        raise
    return {**result, "active": source, "retained": retained, "active_fingerprint": active_fingerprint}


def rollback_upgrade(receipt, *, writers_stopped=False):
    if not writers_stopped: raise RuntimeError("回退前必须关闭图库写入者")
    active, backup = Path(receipt["active"]).resolve(), Path(receipt["backup"]).resolve()
    guard_upgrade_recovery(active)
    with _readonly(backup) as old:
        _inspect_old(old)
        if not receipt.get("source_fingerprint") or _fingerprint(old) != receipt["source_fingerprint"]:
            raise UpgradeRequired("旧库备份已改变，保留当前库，不能自动回退")
    with _readonly(active) as current:
        if not receipt.get("active_fingerprint") or _fingerprint(current) != receipt["active_fingerprint"]:
            raise UpgradeRequired("新版库启用后已有写入，保留当前库与旧备份，不能自动回退")
    staged = active.with_name(active.name + ".rollback-" + uuid4().hex)
    failed = active.with_name(active.name + ".failed-" + uuid4().hex)
    _snapshot(backup, staged)
    with _writable(active) as current:
        if current.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            raise UpgradeRequired("新版库仍被使用，未执行回退")
    intent = _begin_switch(active, action="rollback", staged=staged, retained=failed, backup=backup,
        candidate=receipt.get("candidate"))
    moved_active = False
    try:
        os.replace(active, failed)
        moved_active = True
        os.replace(staged, active)
        with _readonly(active) as restored:
            if _fingerprint(restored) != receipt["source_fingerprint"]:
                raise UpgradeRequired("回退后的源库与旧备份不符")
        _finish_switch(active, intent, "rolled_back")
    except BaseException:
        try:
            if moved_active:
                if active.exists(): os.replace(active, active.with_name(active.name + ".failed-" + uuid4().hex))
                os.replace(failed, active)
            with _readonly(active) as restored:
                if _fingerprint(restored) != receipt["active_fingerprint"]:
                    raise UpgradeRequired("恢复后的新版库与启用记录不符")
            _finish_switch(active, intent, "restored")
        except BaseException as restore_error:
            raise UpgradeRecoveryRequired(active, failed, intent=intent) from restore_error
        raise
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
