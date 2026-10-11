"""Synthetic parent-task deltas: preserve reference truth without broad rewrites."""
from contextlib import contextmanager
import json

import pytest
from sqlalchemy import event, select

from src.core.database import ArchiveDatabase
from src.core.models import Artist, Asset, Derivative, Operation, OperationItem, Work
from src.core.projection_maintenance import reconcile_operation, verify_projections
from src.core.read_projection import ProjectionUnavailable


@pytest.fixture
def database(tmp_path):
    value = ArchiveDatabase.create(tmp_path / 'synthetic.db')
    with value.write() as session:
        session.add(Artist(id=1, pixiv_user_id='synthetic', name='Synthetic'))
        session.flush()
        for identity in range(1, 7):
            session.add(Work(id=identity, pixiv_illust_id=str(identity), artist_id=1,
                title=f'Synthetic {identity}', source_metadata={'unused': 'x' * 65536,
                    **({'source_updates': {'page': {'reason': 'synthetic change'}}} if identity == 2 else {})}))
        session.flush()
        for identity in range(1, 7):
            session.add(Asset(id=f'a{identity}', work_id=identity, role='image', page_index=0,
                state='missing' if identity in (1, 2, 4) else 'verified',
                relative_path=None if identity in (1, 2, 4) else f'{identity}.png'))
            session.add(Derivative(id=f'd{identity}', work_id=identity, kind='thumbnail', page_index=0,
                spec_key='v1', state='verified', relative_path=f'{identity}.webp'))
        operation = Operation(id='parent', kind='repair', state='ready', dedupe_key='parent', label='Synthetic',
            parameters={'work_ids': list(range(1, 7))})
        session.add(operation)
        session.flush()
        for identity in range(1, 7):
            session.add(OperationItem(id=f'i{identity}', operation_id='parent', item_key=f'i{identity}',
                kind='image', phase='download', state='failed' if identity == 4 else 'finished' if identity == 5 else 'pending',
                target={'work_id': identity}, position=identity,
                result={'message': 'synthetic failure'} if identity == 4 else {}))
        session.add(OperationItem(id='legacy', operation_id='parent', item_key='legacy', kind='image', phase='download',
            target={}, state='failed', position=100, result={'message': 'legacy failure'}))
    try:
        yield value
    finally:
        value.close()


@contextmanager
def statements(database):
    captured = []
    def record(connection, cursor, sql, parameters, context, many):
        captured.append((sql, parameters, many))
    event.listen(database.writer_engine, 'before_cursor_execute', record)
    try:
        yield captured
    finally:
        event.remove(database.writer_engine, 'before_cursor_execute', record)


def state(database, identity):
    with database.read() as session:
        return json.loads(session.connection().exec_driver_sql(
            'SELECT value FROM read_work_state WHERE work_id=?', (identity,)).scalar_one())


def reference(database):
    with database.read() as session:
        verify_projections(session)


def test_parent_state_delta_avoids_batch_delete_source_work_and_order_rewrites(database):
    settled = {identity: state(database, identity) for identity in (4, 5)}
    with statements(database) as captured:
        with database.write() as session:
            session.get(Operation, 'parent').state = 'running'
            session.get(OperationItem, 'i1').state = 'running'
    costs = {'batch_deletes': sum(sql.startswith('DELETE FROM read_task_candidates WHERE operation_id=') for sql, _, _ in captured),
        'source_work_reads': sum(sql.lstrip().startswith('SELECT') and 'FROM works' in sql for sql, _, _ in captured),
        'order_writes': sum(sql.startswith(('INSERT INTO read_work_order', 'UPDATE read_common_counts')) for sql, _, _ in captured)}
    assert costs == dict.fromkeys(costs, 0)
    assert state(database, 1)['processing']['state'] == 'running'
    assert {identity: state(database, identity) for identity in (4, 5)} == settled
    state_writes = [(parameters if many else [parameters]) for sql, parameters, many in captured
                    if sql.startswith('UPDATE read_work_state')]
    written_ids = {row[1] for batch in state_writes for row in batch}
    assert not written_ids.intersection((4, 5))
    cache_sql, parameters, _ = next(row for row in captured if row[0].startswith('SELECT s.work_id'))
    with database.read() as session:
        plans = [row[3] for row in session.connection().exec_driver_sql('EXPLAIN QUERY PLAN ' + cache_sql, parameters)]
    assert any('ix_read_task_winner' in plan for plan in plans)
    assert not any('SCAN' in plan or 'TEMP B-TREE' in plan for plan in plans), plans
    reference(database)


def test_finished_scope_shrink_delete_and_mixed_source_change_match_reference(database):
    with database.write() as session:
        session.get(Operation, 'parent').state = 'finished'
        session.get(OperationItem, 'i1').state = 'finished'
        session.get(OperationItem, 'i2').state = 'failed'
        session.get(OperationItem, 'i2').result = {'message': 'new failure'}
        session.get(Work, 2).source_metadata = {'source_updates': {'changed': {'reason': 'new source'}}}
        session.get(Asset, 'a1').state = 'verified'
        session.get(Asset, 'a1').relative_path = '1.png'
    assert state(database, 1)['archive']['state'] == 'complete'
    assert state(database, 2)['source_update']['reason'] == 'new source'
    reference(database)
    with database.write() as session:
        session.get(Operation, 'parent').parameters = {'work_ids': [1, 2]}
    with database.read() as session:
        assert list(session.connection().exec_driver_sql(
            'SELECT work_id FROM read_operation_scope WHERE operation_id=? ORDER BY work_id', ('parent',)).scalars()) == [1, 2]
    reference(database)
    with database.write() as session:
        session.delete(session.get(Operation, 'parent'))
    with database.read() as session:
        assert session.connection().exec_driver_sql('SELECT count(*) FROM read_task_candidates').scalar_one() == 0
    reference(database)


def test_unchanged_operation_does_not_write_candidates_or_work_state(database):
    with statements(database) as captured:
        with database.write() as session:
            assert reconcile_operation(session.connection(), 'parent') == set()
    assert not any(sql.startswith(('INSERT INTO read_task_candidates', 'UPDATE read_task_candidates',
        'DELETE FROM read_task_candidates', 'UPDATE read_work_state')) for sql, _, _ in captured)


def test_existing_work_missing_cache_rolls_back_task_change(database):
    with database.engine.begin() as connection:
        connection.exec_driver_sql('DELETE FROM read_work_state WHERE work_id=1')
    with pytest.raises(ProjectionUnavailable):
        with database.write() as session:
            session.get(Operation, 'parent').state = 'running'
    with database.read() as session:
        assert session.get(Operation, 'parent').state == 'ready'


def test_nonexistent_task_identity_keeps_reference_semantics(database):
    with database.write() as session:
        session.get(OperationItem, 'i1').target = {'work_id': 999999}
        session.get(Operation, 'parent').state = 'running'
    reference(database)
