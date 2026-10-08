"""Startup-only query indexes: validate, snapshot, then create atomically under a lease."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from .database import SCHEMA_REVISION
from .ownership import LibraryLease
from .task_indexes import TASK_INDEX_SQL, create_task_indexes

SETTINGS_INDEX_SQL = {
    'ix_works_settings_shape': 'CREATE INDEX ix_works_settings_shape ON works '
        '(default_revision_id, pixiv_illust_id, type, page_count)',
    'ix_revisions_settings_shape': 'CREATE INDEX ix_revisions_settings_shape ON work_revisions '
        '(id, type, page_count)',
}


class TaskIndexPreparationError(RuntimeError):
    user_message = "本地查询索引准备未完成。原记录未被覆盖，已有备份保留；请检查可用空间和文件占用后重试。"


class TaskIndexDefinitionMismatch(TaskIndexPreparationError):
    user_message = "已有本地查询索引与本版定义不同。程序未删除或重建它，请关闭程序后检查索引定义。"


def _normalized_sql(statement):
    # SQL keywords/identifiers are case-insensitive; JSON paths/string literals
    # are not. Keep literal contents rather than lowercasing the whole SQL.
    tokens = re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^]]*\]|/\*.*?\*/|--[^\n]*|[A-Za-z_][A-Za-z_0-9]*|[^\s]", statement, re.S)
    normalized = []
    for token in tokens:
        if token.startswith(('/*', '--')):
            continue
        if token.startswith("'"):
            normalized.append(('literal', token))
        elif token.startswith(('"', '`', '[')):
            normalized.append(('word', token[1:-1].replace(token[0] * 2, token[0]).casefold()))
        else:
            normalized.append(('word', token.casefold()))
    if_not_exists = [('word', word) for word in ('if', 'not', 'exists')]
    for offset in range(min(5, len(normalized) - 2)):
        if normalized[offset:offset + 3] == if_not_exists:
            del normalized[offset:offset + 3]
            break
    while normalized and normalized[-1] == ('word', ';'):
        normalized.pop()
    return normalized


def _missing_indexes(connection, definitions):
    """Inspect only; same names with different definitions are never replaced."""
    existing = {name.casefold(): sql for name, sql in connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='index'")}
    missing = []
    for name, sql in definitions.items():
        actual = existing.get(name.casefold())
        if name.casefold() not in existing:
            missing.append(name)
        elif actual is None or _normalized_sql(actual) != _normalized_sql(sql):
            raise TaskIndexDefinitionMismatch(TaskIndexDefinitionMismatch.user_message)
    return missing


def missing_task_indexes(connection):
    return _missing_indexes(connection, TASK_INDEX_SQL)


def missing_settings_indexes(connection):
    return _missing_indexes(connection, SETTINGS_INDEX_SQL)


def create_settings_indexes(connection):
    """Create only validated missing indexes; caller owns the transaction."""
    missing = missing_settings_indexes(connection)
    for name in missing:
        connection.exec_driver_sql(SETTINGS_INDEX_SQL[name])
    return tuple(missing)


def _missing_query_indexes(connection):
    return missing_task_indexes(connection) + missing_settings_indexes(connection)


def _save_receipt(directory, receipt):
    with (directory / 'receipt.json').open('w', encoding='utf-8') as output:
        json.dump(receipt, output, ensure_ascii=False, indent=2)


def _backup(source, target):
    # SQLite's backup API includes committed WAL frames in one consistent
    # snapshot. Copying the bare main file would not preserve those records.
    with target.open('xb'):
        pass
    source_connection = destination = None
    try:
        source_connection = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        destination = sqlite3.connect(target)
        source_connection.backup(destination, pages=256)
        if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('Backup validation failed')
        revision = destination.execute('SELECT version_num FROM alembic_version').fetchone()
        if not revision or revision[0] != SCHEMA_REVISION:
            raise RuntimeError('Backup schema did not match')
    finally:
        if destination is not None:
            destination.close()
        if source_connection is not None:
            source_connection.close()


def prepare_task_indexes(database, lease: LibraryLease, *, progress=None):
    """Prepare additive task/settings indexes before workers; retain the existing API."""
    if not isinstance(lease, LibraryLease) or lease._stream is None or lease._stream.closed:
        raise TaskIndexPreparationError('查询索引准备需要持有图库租约')
    with database._write_lock:
        with database.engine.connect() as connection:
            missing = _missing_query_indexes(connection)
        if not missing:
            return None
        directory = database.path.parent / 'upgrade' / (datetime.now().strftime('%Y%m%d-') + uuid4().hex)
        backup = directory / 'backup.db'
        receipt = {'kind': 'task_indexes', 'schema_revision': SCHEMA_REVISION,
                   'created_at': datetime.now(timezone.utc).isoformat(),
                   'source': str(database.path), 'backup': str(backup),
                   'missing_indexes': missing, 'state': 'preparing'}
        try:
            directory.mkdir(parents=True, exist_ok=False)
            _save_receipt(directory, receipt)
            if progress:
                progress('正在备份本地图库，准备本地查询索引')
            _backup(database.path, backup)
            receipt['state'], receipt['backup_bytes'] = 'backed_up', backup.stat().st_size
            _save_receipt(directory, receipt)
            if progress:
                progress('正在准备本地查询索引，原图片与任务保留')
            with database.engine.begin() as connection:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                # Recheck every definition under the write transaction before
                # issuing any DDL, including indexes created since inspection.
                _missing_query_indexes(connection)
                create_task_indexes(connection)
                create_settings_indexes(connection)
                if _missing_query_indexes(connection):
                    raise RuntimeError('Query indexes were not completely prepared')
                # A failed final receipt must still roll back the DDL; after
                # committing there is no safe automatic restoration of data.
                receipt['state'] = 'complete'
                _save_receipt(directory, receipt)
            return receipt
        except BaseException as error:
            receipt.update(state='failed', error_code=type(error).__name__)
            try:
                _save_receipt(directory, receipt)
            except OSError:
                pass
            if not isinstance(error, Exception):
                raise
            raise TaskIndexPreparationError(TaskIndexPreparationError.user_message) from error
