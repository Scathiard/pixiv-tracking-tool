"""Transaction-live task counters and bounded status/history read indexes.

Source triggers maintain counts in the same SQLite transaction as source DML.
There is no startup/read-time recount. Explicit candidate rebuild and validation
are the only source-wide operations. Unknown states and NULL phase/unit groups
are retained rather than silently folded into a known-state bucket. Reading a
phase list costs its distinct output groups, not its historical item count.
"""
from __future__ import annotations

from sqlalchemy import JSON, column, select, table
from sqlalchemy.orm import defer

from .models import Operation
from .read_projection import ProjectionUnavailable
from .read_control import track_read_stage

PRIORITY_SQL = "CASE WHEN source='manual' THEN 0 WHEN source='maintenance' THEN 2 ELSE 1 END"
RUNNING_STATES_SQL = "state IN ('planning','running')"
WAITING_STATES_SQL = "state IN ('queued','ready','interrupted')"
PROBLEM_STATES_SQL = "state IN ('failed','not_executed')"
REMOTE_INVENTORY_SQL = "kind IN ('discover_page','bookmark_page','bookmark_artist') AND state IN ('pending','running') AND phase IN ('checking','bookmarks') AND unit IN ('pages','artists')"

_DDL = (
    "CREATE TABLE read_task_status (id INTEGER PRIMARY KEY CHECK(id=1), active INTEGER NOT NULL CHECK(active>=0), queued INTEGER NOT NULL CHECK(queued>=0), waiting INTEGER NOT NULL CHECK(waiting>=0), attention INTEGER NOT NULL CHECK(attention>=0))",
    "CREATE TABLE read_task_operation_view (operation_id TEXT PRIMARY KEY, status_result TEXT NOT NULL, history_result TEXT NOT NULL) WITHOUT ROWID",
    "CREATE TABLE read_task_operation_counts (operation_id TEXT NOT NULL, state_key TEXT NOT NULL, state TEXT, value INTEGER NOT NULL CHECK(value>=0), PRIMARY KEY(operation_id,state_key)) WITHOUT ROWID",
    "CREATE TABLE read_task_phase_counts (operation_id TEXT NOT NULL, phase_key TEXT NOT NULL, phase TEXT, unit TEXT, state TEXT, value INTEGER NOT NULL CHECK(value>=0), PRIMARY KEY(operation_id,phase_key)) WITHOUT ROWID",
    "CREATE INDEX ix_read_task_phase_counts ON read_task_phase_counts(operation_id,phase,unit,state)",
    "CREATE TABLE read_task_round_counts (round_id TEXT NOT NULL, state_key TEXT NOT NULL, state TEXT, value INTEGER NOT NULL CHECK(value>=0), PRIMARY KEY(round_id,state_key)) WITHOUT ROWID",
    "CREATE INDEX ix_task_status_history ON operations(state,finished_at DESC,id DESC)",
    "CREATE INDEX ix_task_status_attention_history ON operations(state,finished_at DESC,id DESC) WHERE needs_attention IS 1",
    f"CREATE INDEX ix_task_status_running ON operations(({PRIORITY_SQL}),created_at) WHERE {RUNNING_STATES_SQL}",
    f"CREATE INDEX ix_task_status_waiting ON operations(({PRIORITY_SQL}),created_at) WHERE {WAITING_STATES_SQL}",
    "CREATE INDEX ix_task_round_latest ON task_rounds(created_at DESC)",
    "CREATE INDEX ix_task_item_detail ON operation_items(operation_id,position)",
    f"CREATE INDEX ix_task_item_problems ON operation_items(operation_id,position) WHERE {PROBLEM_STATES_SQL}",
    "CREATE INDEX ix_task_item_running ON operation_items(operation_id,state,position)",
    f"CREATE INDEX ix_task_item_remote_inventory ON operation_items(operation_id) WHERE {REMOTE_INVENTORY_SQL}",
)
STATUS_RESULT_SQL = "json_remove({result},'$.current_item','$.current_items','$.child_operation_ids','$.parent_operation_ids')"
HISTORY_RESULT_SQL = "json_remove({result},'$.current_item','$.current_items')"
_STATUS_EXPRESSIONS = (
    ("active", "coalesce({side}.state IN ('queued','planning','ready','running','interrupted'),0)"),
    ("queued", "coalesce({side}.state IN ('queued','ready'),0)"),
    ("waiting", "coalesce({side}.round_id IS NULL AND {side}.state='queued',0)"),
    ("attention", "({side}.needs_attention IS 1)"),
)


def _trigger(connection, name, table, action, body, when=None):
    connection.exec_driver_sql(f"CREATE TRIGGER {name} AFTER {action} ON {table} "
        + (f"WHEN {when} " if when else "") + f"BEGIN {body} END")


def _operation_result(side):
    status = STATUS_RESULT_SQL.format(result=f"{side}.result")
    history = HISTORY_RESULT_SQL.format(result=f"{side}.result")
    return f"INSERT INTO read_task_operation_view VALUES({side}.id,{status},{history}) ON CONFLICT(operation_id) DO UPDATE SET status_result=excluded.status_result,history_result=excluded.history_result;"


def _status_delta(side, sign):
    return "UPDATE read_task_status SET " + ",".join(
        f"{name}={name}{sign}({expression.format(side=side)})"
        for name, expression in _STATUS_EXPRESSIONS) + " WHERE id=1;"


def _item_delta(side, *, add):
    operation_id, state = f"{side}.operation_id", f"{side}.state"
    state_key, phase_key = f"json_array({state})", f"json_array({side}.phase,{side}.unit,{state})"
    round_id = f"(SELECT round_id FROM operations WHERE id={operation_id})"
    if add:
        return (
            f"INSERT INTO read_task_operation_counts VALUES({operation_id},{state_key},{state},1) ON CONFLICT(operation_id,state_key) DO UPDATE SET value=value+1;"
            f"INSERT INTO read_task_phase_counts VALUES({operation_id},{phase_key},{side}.phase,{side}.unit,{state},1) ON CONFLICT(operation_id,phase_key) DO UPDATE SET value=value+1;"
            f"INSERT INTO read_task_round_counts SELECT {round_id},{state_key},{state},1 WHERE {round_id} IS NOT NULL ON CONFLICT(round_id,state_key) DO UPDATE SET value=value+1;"
        )
    return (
        f"UPDATE read_task_operation_counts SET value=value-1 WHERE operation_id={operation_id} AND state_key={state_key};"
        f"DELETE FROM read_task_operation_counts WHERE operation_id={operation_id} AND state_key={state_key} AND value=0;"
        f"UPDATE read_task_phase_counts SET value=value-1 WHERE operation_id={operation_id} AND phase_key={phase_key};"
        f"DELETE FROM read_task_phase_counts WHERE operation_id={operation_id} AND phase_key={phase_key} AND value=0;"
        f"UPDATE read_task_round_counts SET value=value-1 WHERE round_id={round_id} AND state_key={state_key};"
        f"DELETE FROM read_task_round_counts WHERE round_id={round_id} AND state_key={state_key} AND value=0;"
    )


def _round_transfer(*, remove):
    # This is an explicit grouped fan-out when the parent changes rounds. Its
    # cost is the operation's distinct states, never a hidden read-time scan.
    if remove:
        return """UPDATE read_task_round_counts SET value=value-coalesce((SELECT c.value
            FROM read_task_operation_counts c WHERE c.operation_id=OLD.id
            AND c.state_key=read_task_round_counts.state_key),0)
            WHERE round_id=OLD.round_id AND state_key IN
            (SELECT state_key FROM read_task_operation_counts WHERE operation_id=OLD.id);
            DELETE FROM read_task_round_counts WHERE round_id=OLD.round_id AND value=0;"""
    return """INSERT INTO read_task_round_counts SELECT NEW.round_id,state_key,state,value
        FROM read_task_operation_counts WHERE operation_id=OLD.id AND NEW.round_id IS NOT NULL
        ON CONFLICT(round_id,state_key) DO UPDATE SET value=value+excluded.value;"""


def install_task_read_projection(connection):
    """Install empty aggregates and source triggers in an explicit candidate."""
    if not connection.in_transaction():
        raise RuntimeError("task projection installation requires an explicit transaction")
    for statement in _DDL:
        connection.exec_driver_sql(statement)
    connection.exec_driver_sql("INSERT INTO read_task_status VALUES(1,0,0,0,0)")
    _trigger(connection, "read_task_counter_operation_insert", "operations", "INSERT", _status_delta("NEW", "+") + _operation_result("NEW"))
    _trigger(connection, "read_task_counter_operation_delete", "operations", "DELETE",
        _status_delta("OLD", "-") + _round_transfer(remove=True)
        + "DELETE FROM read_task_operation_counts WHERE operation_id=OLD.id;"
        + "DELETE FROM read_task_phase_counts WHERE operation_id=OLD.id;"
        + "DELETE FROM read_task_operation_view WHERE operation_id=OLD.id;")
    _trigger(connection, "read_task_result_operation_update", "operations", "UPDATE",
        "DELETE FROM read_task_operation_view WHERE operation_id=OLD.id AND OLD.id IS NOT NEW.id;" + _operation_result("NEW"),
        "OLD.id IS NOT NEW.id OR OLD.result IS NOT NEW.result")
    _trigger(connection, "read_task_counter_operation_update", "operations", "UPDATE",
        _status_delta("OLD", "-") + _status_delta("NEW", "+"),
        "OLD.state IS NOT NEW.state OR OLD.round_id IS NOT NEW.round_id OR OLD.needs_attention IS NOT NEW.needs_attention")
    _trigger(connection, "read_task_counter_operation_round", "operations", "UPDATE",
        _round_transfer(remove=True) + _round_transfer(remove=False)
        + "UPDATE read_task_operation_counts SET operation_id=NEW.id WHERE operation_id=OLD.id;"
        + "UPDATE read_task_phase_counts SET operation_id=NEW.id WHERE operation_id=OLD.id;",
        "OLD.round_id IS NOT NEW.round_id OR OLD.id IS NOT NEW.id")
    _trigger(connection, "read_task_counter_item_insert", "operation_items", "INSERT", _item_delta("NEW", add=True))
    _trigger(connection, "read_task_counter_item_delete", "operation_items", "DELETE", _item_delta("OLD", add=False))
    _trigger(connection, "read_task_counter_item_update", "operation_items", "UPDATE",
        _item_delta("OLD", add=False) + _item_delta("NEW", add=True),
        "OLD.operation_id IS NOT NEW.operation_id OR OLD.state IS NOT NEW.state OR OLD.phase IS NOT NEW.phase OR OLD.unit IS NOT NEW.unit")
    _trigger(connection, "read_task_counter_round_delete", "task_rounds", "DELETE",
        "DELETE FROM read_task_round_counts WHERE round_id=OLD.id;")
    for table in ("operations", "operation_items", "task_rounds"):
        for action in ("INSERT", "UPDATE", "DELETE"):
            _trigger(connection, f"read_task_dirty_{table}_{action.lower()}", table, action,
                "INSERT OR IGNORE INTO read_model_dirty VALUES('task_status','snapshot');")


def rebuild_task_read_projection(connection):
    """Explicit source-wide backfill; caller owns candidate transaction/receipt."""
    for table in ("read_task_operation_view", "read_task_operation_counts", "read_task_phase_counts", "read_task_round_counts"):
        connection.exec_driver_sql(f"DELETE FROM {table}")
    connection.exec_driver_sql("INSERT INTO read_task_operation_view SELECT id," + STATUS_RESULT_SQL.format(result="result") + "," + HISTORY_RESULT_SQL.format(result="result") + " FROM operations")
    expressions = ",".join(f"coalesce(sum({expression.format(side='operations')}),0)" for _, expression in _STATUS_EXPRESSIONS)
    connection.exec_driver_sql("UPDATE read_task_status SET (active,queued,waiting,attention)=(SELECT " + expressions + " FROM operations) WHERE id=1")
    connection.exec_driver_sql("INSERT INTO read_task_operation_counts SELECT operation_id,json_array(state),state,count(*) FROM operation_items GROUP BY operation_id,state")
    connection.exec_driver_sql("INSERT INTO read_task_phase_counts SELECT operation_id,json_array(phase,unit,state),phase,unit,state,count(*) FROM operation_items GROUP BY operation_id,phase,unit,state")
    connection.exec_driver_sql("INSERT INTO read_task_round_counts SELECT o.round_id,json_array(i.state),i.state,count(*) FROM operation_items i JOIN operations o ON o.id=i.operation_id WHERE o.round_id IS NOT NULL GROUP BY o.round_id,i.state")


def verify_task_read_projection(connection):
    """Explicit independent full reconciliation, never used by ordinary reads."""
    expressions = ",".join(f"coalesce(sum({expression.format(side='operations')}),0)" for _, expression in _STATUS_EXPRESSIONS)
    expected_status = tuple(connection.exec_driver_sql("SELECT " + expressions + " FROM operations").one())
    stored_status = connection.exec_driver_sql("SELECT active,queued,waiting,attention FROM read_task_status WHERE id=1").first()
    if stored_status is None or tuple(stored_status) != expected_status:
        raise ProjectionUnavailable("task status aggregate mismatch")
    expected_views = "SELECT id," + STATUS_RESULT_SQL.format(result="result") + "," + HISTORY_RESULT_SQL.format(result="result") + " FROM operations"
    stored_views = "SELECT operation_id,status_result,history_result FROM read_task_operation_view"
    if (connection.exec_driver_sql(f"SELECT * FROM ({stored_views} EXCEPT {expected_views}) LIMIT 1").first()
            or connection.exec_driver_sql(f"SELECT * FROM ({expected_views} EXCEPT {stored_views}) LIMIT 1").first()):
        raise ProjectionUnavailable("task operation result projection mismatch")
    comparisons = (
        ("read_task_operation_counts", "operation_id,state,value", "SELECT operation_id,state,count(*) FROM operation_items GROUP BY operation_id,state"),
        ("read_task_phase_counts", "operation_id,phase,unit,state,value", "SELECT operation_id,phase,unit,state,count(*) FROM operation_items GROUP BY operation_id,phase,unit,state"),
        ("read_task_round_counts", "round_id,state,value", "SELECT o.round_id,i.state,count(*) FROM operation_items i JOIN operations o ON o.id=i.operation_id WHERE o.round_id IS NOT NULL GROUP BY o.round_id,i.state"),
    )
    for table, columns, expected in comparisons:
        actual = f"SELECT {columns} FROM {table}"
        if (connection.exec_driver_sql(f"SELECT * FROM ({actual} EXCEPT {expected}) LIMIT 1").first()
                or connection.exec_driver_sql(f"SELECT * FROM ({expected} EXCEPT {actual}) LIMIT 1").first()):
            raise ProjectionUnavailable(f"task count aggregate mismatch: {table}")
        key_expression = "json_array(phase,unit,state)" if table == "read_task_phase_counts" else "json_array(state)"
        key_column = "phase_key" if table == "read_task_phase_counts" else "state_key"
        if connection.exec_driver_sql(f"SELECT 1 FROM {table} WHERE {key_column}!={key_expression} OR value<=0 LIMIT 1").first():
            raise ProjectionUnavailable(f"task count identity mismatch: {table}")
    return {"task_status": True}


def _connection(session):
    # A raw connection SELECT does not trigger ORM autoflush. Live writer
    # callers must first publish pending item states so SQL triggers can run.
    session.flush()
    return session.connection()


def status_counts(session):
    row = _connection(session).exec_driver_sql("SELECT active,queued,waiting,attention FROM read_task_status WHERE id=1").first()
    if row is None:
        raise ProjectionUnavailable("task status projection is missing")
    return dict(zip(("active", "queued", "waiting", "attention"), row))


def operation_counts(session, operation_id):
    return dict(_connection(session).exec_driver_sql("SELECT state,value FROM read_task_operation_counts WHERE operation_id=?", (operation_id,)).all())


def round_counts(session, round_id):
    return dict(_connection(session).exec_driver_sql("SELECT state,value FROM read_task_round_counts WHERE round_id=?", (round_id,)).all())


def phase_counts(session, operation_id, phase, unit):
    return dict(_connection(session).exec_driver_sql("SELECT state,value FROM read_task_phase_counts WHERE operation_id=? AND phase IS ? AND unit IS ? AND state IS NOT NULL ORDER BY state", (operation_id, phase, unit)).all())


def operation_phases(session, operation_id):
    return _connection(session).exec_driver_sql("SELECT phase,unit,state,value FROM read_task_phase_counts WHERE operation_id=? ORDER BY phase,unit,state", (operation_id,)).all()


def remote_inventory_pending(session, operation_id):
    return _connection(session).exec_driver_sql(f"SELECT 1 FROM operation_items INDEXED BY ix_task_item_remote_inventory WHERE operation_id=? AND {REMOTE_INVENTORY_SQL} LIMIT 1", (operation_id,)).first() is not None


def operation_statement(*, status=False):
    """Avoid decoding large scopes and JSON fields omitted by the API contract.

    Sanitization happens on parent result writes. SQL json_remove during reads
    would still parse every discarded child/current-item entry before LIMIT.
    History and details retain their original, output-sensitive graph lists.
    """
    view = table("read_task_operation_view", column("operation_id"),
        column("status_result", JSON), column("history_result", JSON))
    return select(Operation, view.c.status_result if status else view.c.history_result).options(
        defer(Operation.parameters), defer(Operation.result)).outerjoin(view, view.c.operation_id == Operation.id)


def _operation_rows(session, ids, *, status, read_stage=None):
    if not ids:
        return []
    statement = operation_statement(status=status).where(Operation.id.in_(ids))
    if read_stage is not None:
        statement = statement.execution_options(read_stage=read_stage)
    with track_read_stage(read_stage):
        rows = session.execute(statement).all()
    by_id = {operation.id: (operation, result) for operation, result in rows}
    if set(by_id) != set(ids):
        raise ProjectionUnavailable("task operation head membership mismatch")
    if any(result is None for _, result in rows):
        raise ProjectionUnavailable("task operation result projection is missing")
    return [by_id[ident] for ident in ids]


def active_head(session, *, running, limit):
    index = "ix_task_status_running" if running else "ix_task_status_waiting"
    predicate = RUNNING_STATES_SQL if running else WAITING_STATES_SQL
    ids = _connection(session).exec_driver_sql(f"SELECT id FROM operations INDEXED BY {index} WHERE {predicate} ORDER BY {PRIORITY_SQL},created_at LIMIT ?", (limit,)).scalars().all()
    return _operation_rows(session, ids, status=True, read_stage="progress_active_hydrate")


def history_head(session, *, limit, before=None, attention_only=False, status=False):
    index = "ix_task_status_attention_history" if attention_only else "ix_task_status_history"
    predicates = ["state='finished'"]
    args = []
    if attention_only:
        predicates.append("needs_attention IS 1")
    if before is not None:
        predicates.append("(finished_at,id)<(?,?)")
        date, ident = before
        if hasattr(date, "isoformat"):
            # SQLite DateTime columns use six fractional digits even when
            # microseconds are zero; the sqlite3 default adapter omits them.
            date = date.isoformat(" ", timespec="microseconds")
        args.extend((date, ident))
    args.append(limit)
    ids = _connection(session).exec_driver_sql(f"SELECT id FROM operations INDEXED BY {index} WHERE " + " AND ".join(predicates) + " ORDER BY finished_at DESC,id DESC LIMIT ?", tuple(args)).scalars().all()
    return _operation_rows(session, ids, status=status,
        read_stage="progress_history_hydrate" if status else None)


def item_window(session, operation_id, *, offset, limit, problems_only):
    from .models import OperationItem
    index = "ix_task_item_problems" if problems_only else "ix_task_item_detail"
    predicate = " AND " + PROBLEM_STATES_SQL if problems_only else ""
    ids = _connection(session).exec_driver_sql(f"SELECT id FROM operation_items INDEXED BY {index} WHERE operation_id=?{predicate} ORDER BY position LIMIT ? OFFSET ?", (operation_id, limit, offset)).scalars().all()
    if not ids:
        return []
    items = list(session.scalars(select(OperationItem).options(defer(OperationItem.target), defer(OperationItem.depends_on)).where(OperationItem.id.in_(ids))))
    by_id = {item.id: item for item in items}
    return [by_id[ident] for ident in ids]


def running_window(session, operation_id):
    from .models import OperationItem
    ids = _connection(session).exec_driver_sql("SELECT id FROM operation_items INDEXED BY ix_task_item_running WHERE operation_id=? AND state='running' ORDER BY position LIMIT 4", (operation_id,)).scalars().all()
    if not ids:
        return []
    items = list(session.scalars(select(OperationItem).options(defer(OperationItem.target), defer(OperationItem.depends_on)).where(OperationItem.id.in_(ids))))
    by_id = {item.id: item for item in items}
    return [by_id[ident] for ident in ids]
