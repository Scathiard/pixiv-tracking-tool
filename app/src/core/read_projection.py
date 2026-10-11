"""Transactional storage primitives for the explicitly installed read model.

This module does not open a database, migrate a schema, rebuild on reads, or
commit. Callers must resolve legacy/current resource and task identity semantics
before supplying facts, and maintain them in the same transaction as source DML.
Explicit installation, source-change capture and reconciliation are coordinated
by projection_maintenance and the database owner.
"""
from __future__ import annotations

from dataclasses import dataclass
import json


class ProjectionUnavailable(RuntimeError):
    pass


class CursorExpired(ValueError):
    pass


_DDL = (
    "CREATE TABLE read_model_meta (id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL, ready INTEGER NOT NULL CHECK(ready IN (0,1)))",
    "CREATE TABLE read_model_dirty (entity TEXT NOT NULL, identity TEXT NOT NULL, PRIMARY KEY(entity,identity)) WITHOUT ROWID",
    "CREATE TABLE read_resource_totals (scope TEXT PRIMARY KEY, recorded INTEGER NOT NULL DEFAULT 0, verified INTEGER NOT NULL DEFAULT 0, missing INTEGER NOT NULL DEFAULT 0, thumbnail INTEGER NOT NULL DEFAULT 0, gif INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID",
    "CREATE TABLE read_resource_facts (scope TEXT NOT NULL, slot TEXT NOT NULL, physical_id TEXT NOT NULL, recorded INTEGER NOT NULL, verified INTEGER NOT NULL, missing INTEGER NOT NULL, thumbnail INTEGER NOT NULL, gif INTEGER NOT NULL, PRIMARY KEY(scope,slot)) WITHOUT ROWID",
    "CREATE INDEX ix_read_resource_physical ON read_resource_facts(physical_id,scope,slot)",
    "CREATE INDEX ix_read_resource_current_physical ON read_resource_facts(physical_id,scope,slot) WHERE substr(scope,1,5)='work:'",
    "CREATE TABLE read_revision_lifecycle_totals (revision_id TEXT PRIMARY KEY, verified INTEGER NOT NULL DEFAULT 0, readable INTEGER NOT NULL DEFAULT 0, gif INTEGER NOT NULL DEFAULT 0) WITHOUT ROWID",
    "CREATE TABLE read_revision_lifecycle_facts (revision_id TEXT NOT NULL, slot TEXT NOT NULL, physical_id TEXT NOT NULL, verified INTEGER NOT NULL, readable INTEGER NOT NULL, gif INTEGER NOT NULL, PRIMARY KEY(revision_id,slot)) WITHOUT ROWID",
    "CREATE INDEX ix_read_revision_lifecycle_physical ON read_revision_lifecycle_facts(physical_id,revision_id,slot)",
    "CREATE TABLE read_work_order (work_id INTEGER PRIMARY KEY, order_key TEXT NOT NULL, visible INTEGER NOT NULL CHECK(visible IN (0,1)), artist_id INTEGER NOT NULL, work_type TEXT NOT NULL, favorite INTEGER NOT NULL CHECK(favorite IN (0,1)), title_key TEXT NOT NULL DEFAULT '', added_key TEXT NOT NULL DEFAULT '')",
    "CREATE INDEX ix_read_work_order ON read_work_order(visible,order_key DESC,work_id DESC)",
    "CREATE INDEX ix_read_work_artist ON read_work_order(visible,artist_id,order_key DESC,work_id DESC)",
    "CREATE INDEX ix_read_work_type ON read_work_order(visible,work_type,order_key DESC,work_id DESC)",
    "CREATE INDEX ix_read_work_favorite ON read_work_order(visible,favorite,order_key DESC,work_id DESC)",
    "CREATE INDEX ix_read_work_title ON read_work_order(visible,title_key ASC,work_id ASC)",
    "CREATE INDEX ix_read_work_title_artist ON read_work_order(visible,artist_id,title_key ASC,work_id ASC)",
    "CREATE INDEX ix_read_work_title_type ON read_work_order(visible,work_type,title_key ASC,work_id ASC)",
    "CREATE INDEX ix_read_work_title_favorite ON read_work_order(visible,favorite,title_key ASC,work_id ASC)",
    "CREATE INDEX ix_read_work_added ON read_work_order(visible,added_key DESC,work_id DESC)",
    "CREATE INDEX ix_read_work_added_artist ON read_work_order(visible,artist_id,added_key DESC,work_id DESC)",
    "CREATE INDEX ix_read_work_added_type ON read_work_order(visible,work_type,added_key DESC,work_id DESC)",
    "CREATE INDEX ix_read_work_added_favorite ON read_work_order(visible,favorite,added_key DESC,work_id DESC)",
    "CREATE TABLE read_common_counts (scope TEXT PRIMARY KEY, value INTEGER NOT NULL CHECK(value>=0)) WITHOUT ROWID",
    "CREATE TABLE read_task_candidates (work_id INTEGER NOT NULL, candidate_id TEXT NOT NULL, priority INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, failure_priority INTEGER NOT NULL, position INTEGER NOT NULL, operation_id TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(work_id,candidate_id)) WITHOUT ROWID",
    "CREATE INDEX ix_read_task_winner ON read_task_candidates(work_id,priority,created_at DESC,updated_at DESC,failure_priority,position,operation_id DESC,(CASE WHEN substr(candidate_id,1,6)='scope:' THEN 1 ELSE 0 END),item_id)",
    "CREATE INDEX ix_read_task_operation ON read_task_candidates(operation_id,work_id,candidate_id)",
    "CREATE INDEX ix_read_task_item ON read_task_candidates(item_id,work_id,candidate_id)",
    "CREATE TABLE read_operation_scope (operation_id TEXT NOT NULL, work_id INTEGER NOT NULL, PRIMARY KEY(operation_id,work_id)) WITHOUT ROWID",
    "CREATE INDEX ix_read_scope_work ON read_operation_scope(work_id,operation_id)",
)
_FIELDS = ("recorded", "verified", "missing", "thumbnail", "gif")


def install(connection):
    """Install empty, unready projections inside an explicit candidate transaction."""
    if not connection.in_transaction():
        raise RuntimeError("projection installation requires an explicit transaction")
    # Python sqlite legacy mode does not begin a transaction for DDL.
    # Establish the driver transaction so failed install rolls back every table.
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    for statement in _DDL:
        connection.exec_driver_sql(statement)
    connection.exec_driver_sql("INSERT INTO read_model_meta VALUES(1,0,0)")


def require_ready(connection):
    row = connection.exec_driver_sql("SELECT generation,ready,EXISTS(SELECT 1 FROM read_model_dirty LIMIT 1) FROM read_model_meta WHERE id=1").first()
    if row is None or not row[1] or row[2]:
        raise ProjectionUnavailable("读取投影尚未就绪，需要显式完成核验或修复")
    return row[0]


def session_generation(session):
    """Reuse readiness only within ArchiveDatabase's owned read snapshot."""
    if 'read_projection_generation' in session.info:
        return session.info['read_projection_generation']
    return require_ready(session.connection())


def mark_dirty(connection, entity, identity):
    connection.exec_driver_sql("INSERT OR IGNORE INTO read_model_dirty VALUES(?,?)", (entity, str(identity)))


def finish_verified_batch(connection, *, advance_generation=True):
    """Caller has reconciled and verified every queued identity before this call."""
    if connection.exec_driver_sql("SELECT 1 FROM read_model_dirty LIMIT 1").first():
        raise ProjectionUnavailable("仍有未同步的原始记录")
    connection.exec_driver_sql("UPDATE read_model_meta SET ready=1,generation=generation+? WHERE id=1", (int(advance_generation),))


def resource_fact(connection, scope, slot, physical_id=None, *, recorded=0, verified=0,
                  missing=0, thumbnail=0, gif=0):
    """Replace one logical fact; change its aggregate by delta, without scanning P.

    A scope is a caller-defined current work or explicit revision identity. A
    physical file reused by multiple slots retains the source query's multiplicity.
    Derivative readiness must use deduplicated physical-file slots supplied by the
    reconciler, rather than counting multiple logical bindings as multiple files.
    """
    values = (recorded, verified, missing, thumbnail, gif)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("resource facts must be nonnegative integers")
    previous = connection.exec_driver_sql(
        "SELECT recorded,verified,missing,thumbnail,gif FROM read_resource_facts WHERE scope=? AND slot=?",
        (scope, slot)).first()
    old = tuple(previous) if previous else (0,) * 5
    new = values if physical_id is not None else (0,) * 5
    delta = tuple(after - before for after, before in zip(new, old))
    connection.exec_driver_sql("INSERT OR IGNORE INTO read_resource_totals(scope) VALUES(?)", (scope,))
    connection.exec_driver_sql("UPDATE read_resource_totals SET " + ",".join(f"{field}={field}+?" for field in _FIELDS) + " WHERE scope=?", (*delta, scope))
    if physical_id is None:
        connection.exec_driver_sql("DELETE FROM read_resource_facts WHERE scope=? AND slot=?", (scope, slot))
    else:
        connection.exec_driver_sql("INSERT INTO read_resource_facts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(scope,slot) DO UPDATE SET physical_id=excluded.physical_id,recorded=excluded.recorded,verified=excluded.verified,missing=excluded.missing,thumbnail=excluded.thumbnail,gif=excluded.gif", (scope, slot, physical_id, *values))


def resource_totals(connection, scope):
    require_ready(connection)
    row = connection.exec_driver_sql("SELECT recorded,verified,missing,thumbnail,gif FROM read_resource_totals WHERE scope=?", (scope,)).first()
    return dict(zip(_FIELDS, row or (0,) * 5))


def _count_keys(row):
    if row is None or not row[1]:
        return set()
    keys = {"visible", f"artist:{row[2]}", f"type:{row[3]}"}
    if row[4]:
        keys.add("favorite")
    return keys


def work_fact(connection, work_id, *, order_key=None, visible=True, artist_id=0,
              work_type="illust", favorite=False, title="", added_key=""):
    """Maintain a current ordering fact and common counts by old/new membership."""
    old = connection.exec_driver_sql("SELECT order_key,visible,artist_id,work_type,favorite FROM read_work_order WHERE work_id=?", (work_id,)).first()
    new = None if order_key is None else (order_key, int(visible), artist_id, work_type, int(favorite))
    before, after = _count_keys(old), _count_keys(new)
    for key in sorted(before ^ after):
        change = 1 if key in after else -1
        connection.exec_driver_sql("INSERT OR IGNORE INTO read_common_counts VALUES(?,0)", (key,))
        connection.exec_driver_sql("UPDATE read_common_counts SET value=value+? WHERE scope=?", (change, key))
    if new is None:
        connection.exec_driver_sql("DELETE FROM read_work_order WHERE work_id=?", (work_id,))
    else:
        connection.exec_driver_sql("INSERT INTO read_work_order VALUES(?,?,?,?,?,?,lower(?),?) ON CONFLICT(work_id) DO UPDATE SET order_key=excluded.order_key,visible=excluded.visible,artist_id=excluded.artist_id,work_type=excluded.work_type,favorite=excluded.favorite,title_key=excluded.title_key,added_key=excluded.added_key", (work_id, *new, title, added_key))


def common_count(connection, scope="visible"):
    require_ready(connection)
    return connection.exec_driver_sql("SELECT value FROM read_common_counts WHERE scope=?", (scope,)).scalar() or 0


@dataclass(frozen=True)
class Cursor:
    generation: int
    scope: str
    order_key: str
    work_id: int


def work_page(connection, *, limit=20, cursor=None, scope="visible", reverse=False):
    """Seek one supported indexed scope; arbitrary filters belong to on-demand APIs.

    The caller normalizes ordering keys (including NULL dates) before storage.
    Read generation, rows and counts in one database snapshot. Generation changes
    expire cursors explicitly, rather than silently skipping or repeating rows.
    """
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("page size must be between 1 and 500")
    generation = require_ready(connection)
    predicates, args = ["visible=1"], []
    index = "ix_read_work_order"
    if scope.startswith("artist:"):
        predicates.append("artist_id=?"); args.append(int(scope[7:]))
        index = "ix_read_work_artist"
    elif scope.startswith("type:"):
        predicates.append("work_type=?"); args.append(scope[5:])
        index = "ix_read_work_type"
    elif scope == "favorite":
        predicates.append("favorite=1")
        index = "ix_read_work_favorite"
    elif scope != "visible":
        raise ValueError("unsupported fast scope; use explicit on-demand query")
    if cursor:
        if cursor.generation != generation or cursor.scope != scope:
            raise CursorExpired("图库已变化或筛选不同，请重新读取这一窗口")
    direction = "ASC" if reverse else "DESC"
    sql = f"SELECT work_id,order_key FROM read_work_order INDEXED BY {index} WHERE " + " AND ".join(predicates)
    suffix = f" ORDER BY order_key {direction},work_id {direction} LIMIT ?"
    if cursor:
        comparison = ">" if reverse else "<"
        rows = connection.exec_driver_sql(sql + f" AND order_key=? AND work_id {comparison} ?" + suffix,
            (*args, cursor.order_key, cursor.work_id, limit + 1)).all()
        if len(rows) <= limit:
            rows += connection.exec_driver_sql(sql + f" AND order_key {comparison} ?" + suffix,
                (*args, cursor.order_key, limit + 1 - len(rows))).all()
    else:
        rows = connection.exec_driver_sql(sql + suffix, (*args, limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    if reverse:
        rows.reverse()
    head = Cursor(generation, scope, rows[0][1], rows[0][0]) if rows else None
    tail = Cursor(generation, scope, rows[-1][1], rows[-1][0]) if rows else None
    return {"ids": [row[0] for row in rows], "before": head, "after": tail, "has_more": more, "generation": generation}


def task_fact(connection, work_id, candidate_id, *, priority=None, created_at="",
              updated_at="", failure_priority=1, position=0, operation_id="",
              item_id="", payload=None):
    """Store a resolved direct/scope candidate, without expanding history at read."""
    if priority is None:
        connection.exec_driver_sql("DELETE FROM read_task_candidates WHERE work_id=? AND candidate_id=?", (work_id, candidate_id))
        return
    connection.exec_driver_sql("INSERT INTO read_task_candidates VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(work_id,candidate_id) DO UPDATE SET priority=excluded.priority,created_at=excluded.created_at,updated_at=excluded.updated_at,failure_priority=excluded.failure_priority,position=excluded.position,operation_id=excluded.operation_id,item_id=excluded.item_id,payload=excluded.payload", (work_id, candidate_id, priority, created_at, updated_at, failure_priority, position, operation_id, item_id, json.dumps(payload or {}, ensure_ascii=False)))


def task_winner(connection, work_id, *, check_ready=True):
    if check_ready:
        require_ready(connection)
    from .normalized_task_projection import task_winner as normalized_winner
    return normalized_winner(connection, work_id)
