"""Shared parent facts and item-only task projections.

Parent lifecycle writes never visit item or work projections. Three item winners
per work/parent (and per scope parent) preserve active and settled ordering without
re-ranking all a parent's items when it starts, waits, resumes or ends.
"""
from __future__ import annotations

import math

from .read_projection import ProjectionUnavailable

ACTIVE = "('queued','planning','ready','running','interrupted')"
LOCAL = "('check','metadata','repair','verify','backfill','thumbnails','reconcile','compare')"
TABLES = ('read_task_parent', 'read_task_item_fact', 'read_task_work_parent', 'read_task_scope_best')
_DDL = (
    "CREATE TABLE read_task_parent (operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL) WITHOUT ROWID",
    f"CREATE INDEX ix_read_task_parent_active ON read_task_parent(created_at DESC,updated_at DESC,operation_id DESC) WHERE state IN {ACTIVE} AND kind IN {LOCAL}",
    "CREATE TABLE read_task_item_fact (item_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, work_id INTEGER, state TEXT NOT NULL, failure_priority INTEGER NOT NULL, position INTEGER NOT NULL, concern TEXT NOT NULL, reason TEXT) WITHOUT ROWID",
    "CREATE INDEX ix_read_task_item_modes ON read_task_item_fact(operation_id,work_id,state,failure_priority,position,item_id)",
    "CREATE INDEX ix_read_task_item_history ON read_task_item_fact(operation_id,work_id,failure_priority,position,item_id)",
    "CREATE TABLE read_task_work_parent (work_id INTEGER NOT NULL, operation_id TEXT NOT NULL, running_id TEXT, pending_id TEXT, history_id TEXT NOT NULL, PRIMARY KEY(work_id,operation_id)) WITHOUT ROWID",
    "CREATE INDEX ix_read_task_work_parent_operation ON read_task_work_parent(operation_id,work_id)",
    "CREATE TABLE read_task_scope_best (operation_id TEXT PRIMARY KEY, running_id TEXT, pending_id TEXT, history_id TEXT, unplanned INTEGER NOT NULL CHECK(unplanned IN (0,1))) WITHOUT ROWID",
)


def install(connection):
    for statement in _DDL:
        connection.exec_driver_sql(statement)
    connection.exec_driver_sql("""CREATE TRIGGER read_task_scope_definition_update AFTER UPDATE ON operations
        WHEN json_extract(OLD.parameters,'$.work_ids') IS NOT json_extract(NEW.parameters,'$.work_ids')
        BEGIN INSERT OR IGNORE INTO read_model_dirty VALUES('scope_definition',OLD.id);
        INSERT OR IGNORE INTO read_model_dirty VALUES('scope_definition',NEW.id); END""")


def _parent(connection, operation_id):
    return connection.exec_driver_sql("""SELECT id,kind,state,coalesce(created_at,''),
        coalesce(updated_at,'') FROM operations WHERE id=?""",(operation_id,)).first()


def _item_ids(connection, operation_id, work_id):
    keys = []
    for state in ('running', 'pending', None):
        predicate = ' AND state=?' if state else ''
        args = (operation_id, work_id, state) if state else (operation_id, work_id)
        keys.append(connection.exec_driver_sql(
            'SELECT item_id FROM read_task_item_fact WHERE operation_id=? AND work_id IS ?'
            + predicate + ' ORDER BY failure_priority,position,item_id LIMIT 1', args).scalar())
    return keys


def _refresh_group(connection, operation_id, work_id):
    running, pending, history = _item_ids(connection, operation_id, work_id)
    if history is None:
        connection.exec_driver_sql('DELETE FROM read_task_work_parent WHERE work_id=? AND operation_id=?', (work_id, operation_id))
    else:
        connection.exec_driver_sql('''INSERT INTO read_task_work_parent VALUES(?,?,?,?,?)
            ON CONFLICT(work_id,operation_id) DO UPDATE SET running_id=excluded.running_id,
            pending_id=excluded.pending_id,history_id=excluded.history_id''',
            (work_id, operation_id, running, pending, history))


def reconcile_item(connection, item_id):
    from .projection_maintenance import _DIRECT_SQL, _direct_candidate
    old = connection.exec_driver_sql('SELECT operation_id,work_id FROM read_task_item_fact WHERE item_id=?', (item_id,)).first()
    connection.exec_driver_sql('DELETE FROM read_task_item_fact WHERE item_id=?', (item_id,))
    row = connection.exec_driver_sql(_DIRECT_SQL.format(predicate='i.id=?'), (item_id,)).mappings().first()
    new = None
    if row is not None:
        parent = connection.exec_driver_sql('SELECT * FROM read_task_parent WHERE operation_id=?',(row['operation_id'],)).first()
        if parent is not None:
            operation=dict(zip(('id','kind','state','created_at','updated_at'),parent))
            direct = _direct_candidate(connection, row, operation, dry_run=True)
            free = row['explicit_work'] is None and row['asset_id'] is None and row['record_id'] is None
            if direct is not None or free:
                work_id = direct[0] if direct is not None else None
                concern = ('original' if row['asset_id'] is not None or row['kind'] == 'ugoira_pair'
                    else 'local' if row['record_id'] is not None or row['kind'] in ('gif','thumbnail','verify_derivative') else 'work')
                connection.exec_driver_sql('INSERT INTO read_task_item_fact VALUES(?,?,?,?,?,?,?,?)',
                    (item_id, row['operation_id'], work_id, row['state'],
                     int(row['state'] not in ('failed','not_executed')), row['position'], concern, row['reason']))
                new = (row['operation_id'], work_id)
    groups = {tuple(value) for value in (old, new) if value is not None}
    for operation_id, work_id in groups:
        if work_id is not None:
            _refresh_group(connection, operation_id, work_id)
    return set()


def reconcile_scope(connection, operation_id, *, membership_unchanged=False):
    parent = connection.exec_driver_sql('SELECT operation_id FROM read_task_parent WHERE operation_id=?',(operation_id,)).first()
    if parent is None:
        return set()
    if not membership_unchanged:
        old = set(connection.exec_driver_sql('SELECT work_id FROM read_operation_scope WHERE operation_id=?', (operation_id,)).scalars())
        members = set(connection.exec_driver_sql(
            "SELECT DISTINCT CAST(value AS INTEGER) FROM operations,json_each(operations.parameters,'$.work_ids') WHERE operations.id=? AND CAST(value AS INTEGER) IS NOT NULL",(operation_id,)).scalars())
        removed, added = old - members, members - old
        if removed:
            connection.exec_driver_sql('DELETE FROM read_operation_scope WHERE operation_id=? AND work_id=?', [(operation_id, item) for item in sorted(removed)])
        if added:
            connection.exec_driver_sql('INSERT INTO read_operation_scope VALUES(?,?)', [(operation_id, item) for item in sorted(added)])
    running, pending, history = _item_ids(connection, operation_id, None)
    # Existing source counter is transaction-live and avoids reading all items.
    any_item = connection.exec_driver_sql('SELECT 1 FROM read_task_operation_counts WHERE operation_id=? LIMIT 1', (operation_id,)).first()
    connection.exec_driver_sql('''INSERT INTO read_task_scope_best VALUES(?,?,?,?,?)
        ON CONFLICT(operation_id) DO UPDATE SET running_id=excluded.running_id,
        pending_id=excluded.pending_id,history_id=excluded.history_id,unplanned=excluded.unplanned''',
        (operation_id, running, pending, history, int(not any_item)))
    return set()


def reconcile_operation(connection, operation_id):
    parent = _parent(connection, operation_id)
    old = connection.exec_driver_sql('SELECT operation_id FROM read_task_parent WHERE operation_id=?',(operation_id,)).first()
    if parent is None:
        connection.exec_driver_sql('DELETE FROM read_task_parent WHERE operation_id=?', (operation_id,))
        for table in ('read_task_item_fact','read_task_work_parent','read_task_scope_best','read_operation_scope'):
            connection.exec_driver_sql(f'DELETE FROM {table} WHERE operation_id=?', (operation_id,))
        return set()
    connection.exec_driver_sql('''INSERT INTO read_task_parent VALUES(?,?,?,?,?)
        ON CONFLICT(operation_id) DO UPDATE SET kind=excluded.kind,state=excluded.state,
        created_at=excluded.created_at,updated_at=excluded.updated_at''', tuple(parent))
    # Membership is initialized once; changes use the dedicated capture trigger.
    # Lifecycle/time/kind changes stop after the one narrow parent upsert.
    if old is None:
        reconcile_scope(connection, operation_id)
    return set()


def backfill(connection):
    """Bulk candidate-only narrow source mapping; no per-item random queries."""
    from .projection_maintenance import _DIRECT_SQL
    for table in TABLES:
        connection.exec_driver_sql(f'DELETE FROM {table}')
    connection.exec_driver_sql('DELETE FROM read_operation_scope')
    connection.exec_driver_sql("""INSERT INTO read_task_parent SELECT id,kind,state,
        coalesce(created_at,''),coalesce(updated_at,'') FROM operations""")
    batch=[]
    for row in connection.exec_driver_sql(_DIRECT_SQL.format(predicate='1=1').replace('ORDER BY i.id','ORDER BY i.rowid')).mappings():
        free=row['explicit_work'] is None and row['asset_id'] is None and row['record_id'] is None
        identity=row['resolved_work']
        valid=(row['resolved_type'] in ('integer','real') and identity is not None
            and math.isfinite(identity) and int(identity)==identity and -(2**63)<=identity<2**63)
        if not valid and not free:
            continue
        concern=('original' if row['asset_id'] is not None or row['kind']=='ugoira_pair'
            else 'local' if row['record_id'] is not None or row['kind'] in ('gif','thumbnail','verify_derivative') else 'work')
        batch.append((row['id'],row['operation_id'],int(identity) if valid else None,row['state'],
            int(row['state'] not in ('failed','not_executed')),row['position'],concern,row['reason']))
        if len(batch)>=1000:
            connection.exec_driver_sql('INSERT INTO read_task_item_fact VALUES(?,?,?,?,?,?,?,?)',batch)
            batch=[]
    if batch:
        connection.exec_driver_sql('INSERT INTO read_task_item_fact VALUES(?,?,?,?,?,?,?,?)',batch)
    rank_sql='''WITH ranked AS (SELECT *,row_number() OVER (PARTITION BY operation_id,work_id,state
        ORDER BY failure_priority,position,item_id) state_rank,
        row_number() OVER (PARTITION BY operation_id,work_id ORDER BY failure_priority,position,item_id) history_rank
        FROM read_task_item_fact), grouped AS (SELECT operation_id,work_id,
        max(CASE WHEN state='running' AND state_rank=1 THEN item_id END) running_id,
        max(CASE WHEN state='pending' AND state_rank=1 THEN item_id END) pending_id,
        max(CASE WHEN history_rank=1 THEN item_id END) history_id FROM ranked GROUP BY operation_id,work_id) '''
    connection.exec_driver_sql(rank_sql+'''INSERT INTO read_task_work_parent
        SELECT work_id,operation_id,running_id,pending_id,history_id FROM grouped WHERE work_id IS NOT NULL''')
    connection.exec_driver_sql(rank_sql+'''INSERT INTO read_task_scope_best
        SELECT p.operation_id,g.running_id,g.pending_id,g.history_id,
        NOT EXISTS(SELECT 1 FROM operation_items i WHERE i.operation_id=p.operation_id)
        FROM read_task_parent p LEFT JOIN grouped g ON g.operation_id=p.operation_id AND g.work_id IS NULL''')
    connection.exec_driver_sql("""INSERT OR IGNORE INTO read_operation_scope
        SELECT o.id,CAST(j.value AS INTEGER) FROM operations o,json_each(o.parameters,'$.work_ids') j
        WHERE CAST(j.value AS INTEGER) IS NOT NULL""")


# First resolve active candidates from the partial parent index. Historical
# parent summaries are visited only for selected works without an active winner;
# no source payload or item history is read by this path.
def winner_sql(size):
    selected = ','.join('(?)' for _ in range(size))
    active_direct = f'''SELECT g.work_id,p.operation_id,p.state,p.kind,p.created_at,p.updated_at,
        coalesce(g.running_id,g.pending_id) item_id,0 scoped,0 unplanned
        FROM read_task_parent p INDEXED BY ix_read_task_parent_active
        JOIN read_task_work_parent g ON g.operation_id=p.operation_id
        WHERE p.state IN {ACTIVE} AND p.kind IN {LOCAL}
        AND g.work_id IN (SELECT work_id FROM selected)
        AND (g.running_id IS NOT NULL OR g.pending_id IS NOT NULL)'''
    active_scope = f'''SELECT s.work_id,p.operation_id,p.state,p.kind,p.created_at,p.updated_at,
        coalesce(b.running_id,b.pending_id) item_id,1 scoped,b.unplanned
        FROM read_task_parent p INDEXED BY ix_read_task_parent_active
        JOIN read_task_scope_best b ON b.operation_id=p.operation_id
        JOIN read_operation_scope s ON s.operation_id=p.operation_id
        WHERE p.state IN {ACTIVE} AND p.kind IN {LOCAL}
        AND s.work_id IN (SELECT work_id FROM selected)
        AND (b.running_id IS NOT NULL OR b.pending_id IS NOT NULL OR b.unplanned=1)'''
    historical_direct = f'''SELECT g.work_id,p.operation_id,p.state,p.kind,p.created_at,p.updated_at,
        g.history_id item_id,0 scoped,0 unplanned FROM wanted
        CROSS JOIN read_task_work_parent g ON g.work_id=wanted.work_id
        JOIN read_task_parent p ON p.operation_id=g.operation_id WHERE p.kind IN {LOCAL}'''
    historical_scope = f'''SELECT s.work_id,p.operation_id,p.state,p.kind,p.created_at,p.updated_at,
        b.history_id item_id,1 scoped,b.unplanned FROM wanted
        CROSS JOIN read_operation_scope s INDEXED BY ix_read_scope_work ON s.work_id=wanted.work_id
        JOIN read_task_parent p ON p.operation_id=s.operation_id
        JOIN read_task_scope_best b ON b.operation_id=p.operation_id
        WHERE p.kind IN {LOCAL} AND b.history_id IS NOT NULL'''
    return f'''WITH selected(work_id) AS (VALUES {selected}),
        active AS MATERIALIZED ({active_direct} UNION ALL {active_scope}),
        wanted AS MATERIALIZED (SELECT work_id FROM selected WHERE NOT EXISTS
            (SELECT 1 FROM active WHERE active.work_id=selected.work_id)),
        candidates AS ({historical_direct} UNION ALL {historical_scope} UNION ALL SELECT * FROM active),
        resolved AS (SELECT c.*,coalesce(i.state,'pending') item_state,
            coalesce(i.failure_priority,1) failure_priority,coalesce(i.position,0) position,i.concern,i.reason,
            CASE WHEN c.state IN {ACTIVE} AND i.state='running' THEN 0
                 WHEN c.state IN {ACTIVE} AND (i.state='pending' OR c.unplanned=1) THEN 1 ELSE 2 END priority
            FROM candidates c LEFT JOIN read_task_item_fact i ON i.item_id=c.item_id),
        ranked AS (SELECT *,row_number() OVER (PARTITION BY work_id ORDER BY priority,created_at DESC,
            updated_at DESC,failure_priority,position,operation_id DESC,scoped,item_id) rank FROM resolved)
        SELECT work_id,operation_id,state,item_state,reason,
            CASE WHEN scoped=1 THEN CASE WHEN kind='thumbnails' THEN 'local' ELSE 'work' END
                WHEN concern='work' AND kind='thumbnails' THEN 'local' ELSE concern END,scoped,item_id
        FROM ranked WHERE rank=1'''


def winners(connection, work_ids):
    ids=sorted(set(work_ids))
    result={}
    for start in range(0,len(ids),240):
        selected=ids[start:start+240]
        for row in connection.exec_driver_sql(winner_sql(len(selected)),tuple(selected)):
            result[row[0]]={'candidate_id':('scope:'+row[1]) if row[6] else ('item:'+row[7]),
                'payload':dict(zip(('operation_id','operation_state','item_state','reason','concern'),row[1:6]))}
    return result


def task_winner(connection, work_id):
    return winners(connection,[work_id]).get(work_id)


def verify(connection):
    """Explicit narrow source mapping verification, without scope/work expansion."""
    from .projection_maintenance import _DIRECT_SQL, _direct_candidate, _operation
    parents = connection.exec_driver_sql('SELECT id FROM operations ORDER BY id').scalars().all()
    actual_ids = set(connection.exec_driver_sql('SELECT operation_id FROM read_task_parent').scalars())
    if actual_ids != set(parents):
        raise ProjectionUnavailable('normalized task parents mismatch')
    count=0
    for operation_id in parents:
        operation = _operation(connection,operation_id)
        actual_parent = connection.exec_driver_sql('SELECT * FROM read_task_parent WHERE operation_id=?', (operation_id,)).first()
        if tuple(actual_parent) != tuple(_parent(connection,operation_id)):
            raise ProjectionUnavailable('normalized task parent source mismatch')
        expected={}
        groups={}
        any_item=False
        for row in connection.exec_driver_sql(_DIRECT_SQL.format(predicate='i.operation_id=?'),(operation_id,)).mappings():
            any_item=True
            direct=_direct_candidate(connection,row,operation,dry_run=True)
            free=row['explicit_work'] is None and row['asset_id'] is None and row['record_id'] is None
            if direct is None and not free:
                continue
            work_id=direct[0] if direct is not None else None
            concern=('original' if row['asset_id'] is not None or row['kind']=='ugoira_pair'
                else 'local' if row['record_id'] is not None or row['kind'] in ('gif','thumbnail','verify_derivative') else 'work')
            fact=(row['id'],operation_id,work_id,row['state'],int(row['state'] not in ('failed','not_executed')),
                row['position'],concern,row['reason'])
            expected[row['id']]=fact
            groups.setdefault(work_id,[]).append(fact)
        actual={row[0]:tuple(row) for row in connection.exec_driver_sql('SELECT * FROM read_task_item_fact WHERE operation_id=?',(operation_id,))}
        if expected!=actual:
            raise ProjectionUnavailable('normalized task item source mismatch')
        count+=len(expected)
        def best(items):
            ordered=sorted(items,key=lambda fact:(fact[4],fact[5],fact[0]))
            return (next((fact[0] for fact in ordered if fact[3]=='running'),None),
                next((fact[0] for fact in ordered if fact[3]=='pending'),None),
                ordered[0][0] if ordered else None)
        expected_groups={work_id:(work_id,operation_id,*best(items)) for work_id,items in groups.items() if work_id is not None}
        actual_groups={row[0]:tuple(row) for row in connection.exec_driver_sql('SELECT * FROM read_task_work_parent WHERE operation_id=?',(operation_id,))}
        if actual_groups!=expected_groups:
            raise ProjectionUnavailable('normalized task group source mismatch')
        scope_best=connection.exec_driver_sql('SELECT * FROM read_task_scope_best WHERE operation_id=?',(operation_id,)).first()
        if scope_best is None or tuple(scope_best)!=(operation_id,*best(groups.get(None,[])),int(not any_item)):
            raise ProjectionUnavailable('normalized task scope winner mismatch')
        members=set(connection.exec_driver_sql(
            "SELECT DISTINCT CAST(value AS INTEGER) FROM operations,json_each(operations.parameters,'$.work_ids') WHERE operations.id=? AND CAST(value AS INTEGER) IS NOT NULL",(operation_id,)).scalars())
        if members!=set(connection.exec_driver_sql('SELECT work_id FROM read_operation_scope WHERE operation_id=?',(operation_id,)).scalars()):
            raise ProjectionUnavailable('normalized task scope membership mismatch')
    for table in ('read_task_item_fact','read_task_work_parent','read_task_scope_best','read_operation_scope'):
        if connection.exec_driver_sql(f'SELECT 1 FROM {table} WHERE operation_id NOT IN (SELECT operation_id FROM read_task_parent) LIMIT 1').first():
            raise ProjectionUnavailable('normalized task orphan fact')
    return {'parents':len(parents),'items':count}
