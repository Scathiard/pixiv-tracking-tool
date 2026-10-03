"""Audit-only synthetic queue probes. No config, tokens, real data, or network."""
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'app'))
from sqlalchemy import event, insert, select
from src.core.database import ArchiveDatabase
from src.core.models import Operation, OperationItem, TaskRound
from src.core.tasks import ItemResult, ItemSpec, TaskQueue

OUT = Path(__file__).resolve().parent

class FixedHandler:
    def __init__(self, events):
        self.events = events
    def plan(self, parameters, context):
        self.events.append({'event': 'plan', 'operation_id': context.operation_id})
        return [ItemSpec(f'snapshot-{i}', 'reconcile_snapshot', {}, 'checking')
                for i in range(parameters.get('count', 1))]
    def execute(self, spec, context):
        self.events.append({'event': 'execute', 'operation_id': context.operation_id, 'key': spec.key})
        return ItemResult()

def fairness():
    db = ArchiveDatabase.create(OUT / 'fairness.db')
    history = []
    handler = FixedHandler(history)
    queue = TaskQueue(db, {'reconcile': handler, 'artist_source': handler})
    big = queue.submit('reconcile', {'count': 20}, 'synthetic big manual')['operation_id']
    assert queue._step('check')
    assert queue._step('check')
    small = queue.submit('artist_source', {}, 'synthetic small manual')['operation_id']
    assert queue._step('check')  # Later small operation obtains planning first.
    history_after_small_plan = list(history)
    for _ in range(20):
        assert queue._step('check')
    with db.read() as session:
        states = {name: session.get(Operation, ident).state for name, ident in [('big', big), ('small', small)]}
    executions = [e['operation_id'] for e in history if e['event'] == 'execute']
    result = {'big': big, 'small': small, 'history_after_small_plan': history_after_small_plan,
              'execution_order': executions, 'old_items_before_small_exec': executions.index(small),
              'final_states': states, 'events': history}
    assert executions == [big] * 20 + [small]
    db.close()
    return result

def scaled_rows(opid):
    """Match report kind/state counts, use production plan dependency pattern.

    Exact original dependency lengths and placement were not supplied by report;
    assets and derivatives are distributed synthetically across 15,280 works.
    """
    rows = []
    aid = did = 0
    def add(key, kind, work, phase, deps=(), state='pending'):
        rows.append(dict(id=f'item-{len(rows)}', operation_id=opid, item_key=key,
            position=len(rows), kind=kind, target={'work_id': work} if work else {}, phase=phase,
            unit='items', depends_on=list(deps), state=state, attempts=0, result={}))
    for work in range(1, 15281):
        checks = []
        for _ in range(3 + (work <= 2677)):
            aid += 1
            key = f'inspect-asset-{aid}'
            add(key, 'reconcile_asset', work, 'verifying', state='succeeded' if aid <= 3 else 'pending')
            checks.append(key)
        for _ in range(1 + (work <= 2701)):
            did += 1
            key = f'inspect-derivative-{did}'
            add(key, 'reconcile_derivative', work, 'verifying', state='succeeded' if did == 1 else 'pending')
            checks.append(key)
        if work <= 1416:
            key = f'inspect-pair-{work}'
            add(key, 'reconcile_pair', work, 'verifying', checks)
            checks.append(key)
        snapshot = f'snapshot-{work}'
        add(snapshot, 'reconcile_snapshot', work, 'checking', state='succeeded' if work == 1 else 'pending')
        add(f'prepare-{work}', 'reconcile_prepare', work, 'reconciling', checks + [snapshot])
    for artist in range(83):
        add(f'profile-{artist}', 'metadata_artist', None, 'checking')
    assert len(rows) == 98557 and aid == 48517 and did == 17981
    return rows

def performance():
    db = ArchiveDatabase.create(OUT / 'scale.db')
    with db.write() as session:
        round_record = TaskRound(id='scale-round')
        session.add(round_record)
        session.flush()
        session.add(Operation(id='scale-op', kind='reconcile', label='synthetic 98557 items',
            dedupe_key='synthetic', source='manual', state='ready', round_id='scale-round',
            result={'planning_complete': True, 'inventory_complete': True}))
        session.flush()
        rows = scaled_rows('scale-op')
        for start in range(0, len(rows), 2000):
            session.execute(insert(OperationItem), rows[start:start+2000])
    counts = Counter((r['kind'], r['state']) for r in rows)
    lengths = Counter(len(r['depends_on']) for r in rows)
    records = []
    handler = FixedHandler([])
    queue = TaskQueue(db, {'reconcile': handler})
    for lane in ('check', 'download', 'local'):
        current = {'lane': lane, 'sql': None, 'params': None, 'query_started': None}
        def capture(connection, cursor, statement, parameters, context, executemany):
            if (statement.startswith('SELECT operation_items.id,') and 'json_each(operation_items.depends_on)' in statement):
                current['sql'], current['params'] = statement, parameters
                cursor.execute('EXPLAIN QUERY PLAN ' + statement, parameters)
                current['plan'] = [list(row) for row in cursor.fetchall()]
                current['query_started'] = time.perf_counter()
                native = connection.connection.driver_connection
                native.set_progress_handler(lambda: int(time.perf_counter() - current['query_started'] > 3), 10000)
        event.listen(db.engine, 'before_cursor_execute', capture)
        start = time.perf_counter()
        try:
            current['step_result'] = queue._step(lane)
            current['status'] = 'completed'
        except Exception as exc:
            current['status'] = 'interrupted' if 'interrupted' in str(exc) else type(exc).__name__
        finally:
            current['step_seconds'] = time.perf_counter() - start
            current['query_and_followup_seconds'] = time.perf_counter() - current['query_started'] if current['query_started'] else None
            event.remove(db.engine, 'before_cursor_execute', capture)
            with db.engine.connect() as connection:
                connection.connection.driver_connection.set_progress_handler(None, 0)
        current.pop('query_started')
        records.append(current)
    candidates = []
    # Candidate SQL remains in the probe. No application source or schema edit.
    for original in records:
        if original['lane'] not in ('check', 'local') or not original['sql']:
            continue
        sql = original['sql'].replace('JOIN operation_items AS operation_items_1 ON', 'CROSS JOIN operation_items AS operation_items_1 ON')
        assert sql != original['sql']
        rec = {'lane': original['lane'], 'variant': 'JSON outer CROSS JOIN', 'sql': sql}
        with db.write() as session:
            connection = session.connection()
            rec['plan'] = [list(row) for row in connection.exec_driver_sql('EXPLAIN QUERY PLAN ' + sql, original['params'])]
            started = time.perf_counter()
            native = connection.connection.driver_connection
            native.set_progress_handler(lambda: int(time.perf_counter() - started > 3), 10000)
            try:
                found = connection.exec_driver_sql(sql, original['params']).fetchall()
                rec['status'], rec['rows'] = 'completed', len(found)
                rec['first_ids'] = [r[0] for r in found[:4]]
            except Exception as exc:
                rec['status'] = 'interrupted' if 'interrupted' in str(exc) else type(exc).__name__
            finally:
                rec['seconds'] = time.perf_counter() - started
                native.set_progress_handler(None, 0)
        candidates.append(rec)
    db.close()
    return {'scale': 98557, 'state_counts': {f'{k}/{s}': n for (k,s),n in counts.items()},
        'depends_on_length_counts': dict(lengths), 'production_orm_step': records, 'probe_only_candidates': candidates}

result = {'sqlite_version': sqlite3.sqlite_version, 'fairness': fairness(), 'performance': performance(),
    'limits': ['Synthetic data only; original snapshot distribution matched by kind/state counts, dependency placement synthesized from production planner.',
               '3 seconds is a probe interruption budget, not product timeout or measured natural completion.',
               'Production _step capture includes full ORM column query; candidate execution fetches same columns through SQLAlchemy connection.',
               'No real data, config, tokens, network, source modifications, schema changes, or release changes.']}
(OUT / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'sqlite_version': result['sqlite_version'], 'fairness': {'old_items_before_small_exec': result['fairness']['old_items_before_small_exec'], 'final_states': result['fairness']['final_states']},
    'production': [{k:v for k,v in r.items() if k not in ('sql','params','plan')} for r in result['performance']['production_orm_step']],
    'candidate': [{k:v for k,v in r.items() if k not in ('sql','plan','first_ids')} for r in result['performance']['probe_only_candidates']]}, ensure_ascii=False))
