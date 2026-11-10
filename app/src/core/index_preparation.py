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


def compact_bloated_task_history(
    database,
    *,
    keep_finished: int = 24,
    item_threshold: int = 5000,
    vacuum: bool = True,
    progress=None,
) -> dict | None:
    """Fast O(1)-table-truncate compaction for bloated historical operation_items on startup."""
    from . import normalized_task_projection, task_read_projection

    with database._write_lock:
        with database.engine.connect() as connection:
            has_pj_idx = (
                connection.exec_driver_sql(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='index' AND name='ix_publication_journal_operation_item_id'"
                ).first()
                is not None
            )
            item_count = connection.exec_driver_sql("SELECT count(*) FROM operation_items").scalar_one()
            has_unfinished = (
                connection.exec_driver_sql(
                    "SELECT 1 FROM operations WHERE state != 'finished' OR finished_at IS NULL LIMIT 1"
                ).first()
                is not None
            )
            if item_count > item_threshold and not has_unfinished:
                inc_ids = [
                    row[0]
                    for row in connection.exec_driver_sql(
                        "SELECT work_id FROM read_work_state "
                        "WHERE json_extract(value, '$.archive.state') != 'complete' "
                        "   OR json_extract(value, '$.source_update.pending') = 1 "
                        "UNION "
                        "SELECT CAST(substr(key, 18) AS INTEGER) "
                        "FROM application_state "
                        "WHERE key >= 'maintenance_work:' AND key < 'maintenance_work;'"
                    ).fetchall()
                ]
                winners = (
                    normalized_task_projection.winners(connection, inc_ids, history_ids=inc_ids)
                    if inc_ids
                    else {}
                )
                winner_ops = {
                    w["payload"]["operation_id"]
                    for w in winners.values()
                    if w and w.get("payload", {}).get("operation_id")
                }
                winner_items = {
                    w["candidate_id"][5:]
                    for w in winners.values()
                    if w and str(w.get("candidate_id", "")).startswith("item:")
                }
                scope_ops = {
                    w["payload"]["operation_id"]
                    for w in winners.values()
                    if w
                    and str(w.get("candidate_id", "")).startswith("scope:")
                    and w.get("payload", {}).get("operation_id")
                }
                if scope_ops:
                    placeholders = ",".join("?" for _ in scope_ops)
                    winner_items.update(
                        row[0]
                        for row in connection.exec_driver_sql(
                            f"SELECT history_id FROM read_task_scope_best "
                            f"WHERE operation_id IN ({placeholders}) AND history_id IS NOT NULL",
                            tuple(scope_ops),
                        ).fetchall()
                    )
            else:
                winner_ops, winner_items = set(), set()

        if not has_pj_idx:
            with database.engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_publication_journal_operation_item_id "
                    "ON publication_journal (operation_item_id)"
                )
        if item_count <= item_threshold or has_unfinished:
            return None

        if progress:
            progress("正在清理历史冗余任务记录并收缩本地数据库")

        database.engine.dispose()
        database.writer_engine.dispose()

        try:
            with database.engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                triggers = connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='trigger' AND tbl_name IN ('operation_items', 'operations', 'task_rounds') "
                    "ORDER BY name"
                ).fetchall()
                indexes = connection.exec_driver_sql(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type='index' AND tbl_name IN ('operation_items', 'operations', 'task_rounds') "
                    "AND sql IS NOT NULL ORDER BY name"
                ).fetchall()

                connection.exec_driver_sql(
                    """
                    CREATE TEMP TABLE _recent_ops AS
                    SELECT id FROM (
                        SELECT id FROM operations
                        WHERE state = 'finished'
                        ORDER BY finished_at DESC, id DESC
                        LIMIT ?
                    )
                    UNION
                    SELECT id FROM operations
                    WHERE round_id IN (
                        SELECT id FROM (
                            SELECT id FROM task_rounds ORDER BY created_at DESC LIMIT 2
                        )
                    )
                    """,
                    (keep_finished,),
                )
                connection.exec_driver_sql("CREATE TEMP TABLE _keep_ops (id TEXT PRIMARY KEY)")
                connection.exec_driver_sql("INSERT OR IGNORE INTO _keep_ops SELECT id FROM _recent_ops")
                if winner_ops:
                    connection.exec_driver_sql(
                        "INSERT OR IGNORE INTO _keep_ops(id) VALUES (?)",
                        [(op_id,) for op_id in winner_ops],
                    )

                connection.exec_driver_sql("CREATE TEMP TABLE _win_items (id TEXT PRIMARY KEY)")
                if winner_items:
                    connection.exec_driver_sql(
                        "INSERT OR IGNORE INTO _win_items(id) VALUES (?)",
                        [(it_id,) for it_id in winner_items],
                    )

                connection.exec_driver_sql(
                    """
                    CREATE TEMP TABLE _kept_items AS
                    SELECT * FROM operation_items
                    WHERE operation_id IN (SELECT id FROM _recent_ops)
                       OR id IN (SELECT id FROM _win_items)
                    """
                )
                connection.exec_driver_sql(
                    "UPDATE _kept_items SET target = json_remove(target, '$.data') "
                    "WHERE json_type(target, '$.data') IS NOT NULL"
                )
                connection.exec_driver_sql(
                    "CREATE TEMP TABLE _kept_ops_rows AS "
                    "SELECT * FROM operations WHERE id IN (SELECT id FROM _keep_ops)"
                )
                connection.exec_driver_sql(
                    """
                    CREATE TEMP TABLE _kept_rounds_rows AS
                    SELECT * FROM task_rounds
                    WHERE id IN (
                        SELECT id FROM (SELECT id FROM task_rounds ORDER BY created_at DESC LIMIT 2)
                        UNION
                        SELECT DISTINCT round_id FROM _kept_ops_rows WHERE round_id IS NOT NULL
                    )
                    """
                )
                connection.exec_driver_sql(
                    "UPDATE publication_journal SET operation_item_id = NULL "
                    "WHERE operation_item_id IS NOT NULL "
                    "AND operation_item_id NOT IN (SELECT id FROM _kept_items)"
                )
                for name, _ in triggers:
                    connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
                for name, _ in indexes:
                    connection.exec_driver_sql(f'DROP INDEX IF EXISTS "{name}"')

                connection.exec_driver_sql("DELETE FROM operation_items")
                connection.exec_driver_sql("DELETE FROM operations")
                connection.exec_driver_sql("DELETE FROM task_rounds")

                connection.exec_driver_sql("INSERT INTO task_rounds SELECT * FROM _kept_rounds_rows")
                connection.exec_driver_sql("INSERT INTO operations SELECT * FROM _kept_ops_rows")
                connection.exec_driver_sql("INSERT INTO operation_items SELECT * FROM _kept_items")

                connection.exec_driver_sql("DROP TABLE _kept_items")
                connection.exec_driver_sql("DROP TABLE _kept_ops_rows")
                connection.exec_driver_sql("DROP TABLE _kept_rounds_rows")
                connection.exec_driver_sql("DROP TABLE _win_items")
                connection.exec_driver_sql("DROP TABLE _keep_ops")
                connection.exec_driver_sql("DROP TABLE _recent_ops")

                for _, sql in indexes:
                    connection.exec_driver_sql(sql)
                for _, sql in triggers:
                    connection.exec_driver_sql(sql)

                items_after = connection.exec_driver_sql("SELECT count(*) FROM operation_items").scalar_one()
                ops_after = connection.exec_driver_sql("SELECT count(*) FROM operations").scalar_one()

                task_read_projection.rebuild_task_read_projection(connection)
                normalized_task_projection.backfill(connection)
                connection.exec_driver_sql(
                    "DELETE FROM read_model_dirty "
                    "WHERE entity IN ('operation', 'item', 'scope', 'scope_definition')"
                )
                database._schema_version = connection.exec_driver_sql("PRAGMA schema_version").scalar()
        finally:
            database.engine.dispose()
            database.writer_engine.dispose()

        if vacuum:
            database.engine.dispose()
            database.writer_engine.dispose()
            raw_vac = sqlite3.connect(str(database.path), isolation_level=None, timeout=60.0)
            try:
                raw_vac.execute("VACUUM")
                raw_vac.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                raw_vac.close()

        return {
            "items_before": item_count,
            "items_after": items_after,
            "operations_after": ops_after,
            "vacuumed": vacuum,
        }

