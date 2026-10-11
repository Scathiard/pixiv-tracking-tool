"""Synthetic old/new identity capture, atomic maintenance, and fail-closed reads."""
from datetime import datetime, timedelta
import pytest
from sqlalchemy import event, select

from src.core.database import ArchiveDatabase
from src.core import projection_maintenance as maintenance
from src.core import read_projection as projection
from src.core.archive_state import raw_work_states
from src.core.models import (Artist, Work, WorkRevision, Asset, Derivative,
    RevisionAsset, RevisionDerivative, Operation, OperationItem, ApplicationState,
    LocalPreference, RemoteBookmark, ArchiveIntent)


@pytest.fixture
def database(tmp_path):
    database = ArchiveDatabase.create(tmp_path / 'synthetic.db')
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id='1', name='synthetic'))
        session.add_all([Work(id=i, pixiv_illust_id=str(i), artist_id=1,
            title=f'work {i}', type='manga', page_count=2,
            posted_at=datetime(2026, 1, i)) for i in (1, 2, 3)])
    yield database
    database.close()


def assert_verified(database):
    with database.read() as session:
        maintenance.verify_projections(session)
        works = list(session.scalars(select(Work)))
        assert maintenance.read_work_states(session, works) == raw_work_states(session, works)[0]


def state(database, work_id=1):
    with database.read() as session:
        return maintenance.read_work_states(session, [session.get(Work, work_id)])[work_id]


def operation(ident='operation', **kwargs):
    return Operation(id=ident, kind='repair', state=kwargs.pop('state', 'running'),
        dedupe_key=ident, label=ident, **kwargs)


def revision(ident, work_id, ordinal=1, **kwargs):
    return WorkRevision(id=ident, work_id=work_id, ordinal=ordinal, title=ident,
        type=kwargs.pop('type', 'manga'), page_count=kwargs.pop('page_count', 2),
        source_signature=ident, **kwargs)


def test_raw_sql_owned_write_and_failure_rollback(database, monkeypatch):
    with database.write() as session:
        session.connection().exec_driver_sql("UPDATE works SET type='illust',page_count=1 WHERE id=1")
        session.add(Asset(id='a', work_id=1, role='image', page_index=0,
            state='verified', relative_path='synthetic.png'))
    assert state(database)['archive'] == {'state':'complete', 'total':1, 'verified':1, 'missing':0, 'unknown':0}
    before = state(database)
    def fail(*args, **kwargs):
        raise RuntimeError('injected maintenance failure')
    with monkeypatch.context() as patch:
        patch.setattr(maintenance, 'reconcile_revisions', fail)
        with pytest.raises(RuntimeError, match='injected'):
            with database.write() as session:
                session.connection().exec_driver_sql('UPDATE works SET page_count=7 WHERE id=1')
                session.add(LocalPreference(work_id=1, is_hidden=True))
    assert state(database) == before
    with database.read() as session:
        assert session.get(Work, 1).page_count == 1
        assert session.get(LocalPreference, 1) is None
        assert session.connection().exec_driver_sql('SELECT count(*) FROM read_model_dirty').scalar() == 0
    assert_verified(database)


def test_external_sql_stays_dirty_on_open_read_and_write_until_explicit_repair(database):
    with database.writer_engine.begin() as connection:
        connection.exec_driver_sql('UPDATE works SET page_count=5 WHERE id=1')
    with pytest.raises(projection.ProjectionUnavailable):
        with database.read():
            pass
    with pytest.raises(projection.ProjectionUnavailable):
        with database.write():
            raise AssertionError('dirty write admitted')
    reopened = ArchiveDatabase(database.path)
    try:
        with pytest.raises(projection.ProjectionUnavailable):
            with reopened.read():
                pass
        with reopened.writer_session.begin() as session:
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            maintenance.rebuild_projections(session)
            maintenance.verify_projections(session)
    finally:
        reopened.close()
    assert state(database)['archive']['total'] == 5


def test_old_new_physical_owner_and_reused_bindings(database):
    with database.write() as session:
        session.add_all([revision('r1', 1), revision('r2', 2)])
        session.flush()
        session.get(Work, 1).default_revision_id = 'r1'
        session.get(Work, 2).default_revision_id = 'r2'
        session.add(Asset(id='shared', work_id=1, revision_id='r1', role='image',
            page_index=0, state='verified', relative_path='shared.png'))
        session.flush()
        session.add_all([RevisionAsset(revision_id='r1', role='image', page_index=0, asset_id='shared'),
            RevisionAsset(revision_id='r2', role='image', page_index=1, asset_id='shared')])
    assert state(database, 1)['archive']['verified'] == state(database, 2)['archive']['verified'] == 1
    with database.write() as session:
        session.connection().exec_driver_sql("UPDATE assets SET work_id=2,state='missing',relative_path=NULL WHERE id='shared'")
    assert state(database, 1)['archive']['verified'] == state(database, 2)['archive']['verified'] == 0
    with database.read() as session:
        assert maintenance.read_revision_state(session, 'r1')['missing'] == 2
        assert maintenance.read_revision_state(session, 'r2')['missing'] == 2
    assert_verified(database)


def test_revision_rebind_delete_and_current_shape_remain_distinct(database):
    with database.write() as session:
        session.add_all([revision('r1', 1, type='illust', page_count=1), revision('r2', 1, ordinal=2)])
        session.flush()
        session.get(Work, 1).default_revision_id = 'r1'
        session.add_all([Asset(id='saved', work_id=1, revision_id='r1', role='image', page_index=0,
            state='verified', relative_path='a.png'), Asset(id='missing', work_id=1, revision_id='r2', role='image', page_index=0,
            state='missing')])
        session.flush()
        session.add(RevisionAsset(revision_id='r1', role='image', page_index=0, asset_id='saved'))
    assert state(database)['archive']['missing'] == 1
    with database.read() as session:
        assert maintenance.read_revision_state(session, 'r1')['state'] == 'complete'
    with database.write() as session:
        session.connection().exec_driver_sql("UPDATE revision_assets SET asset_id='missing',revision_id='r2' WHERE revision_id='r1'")
    with database.read() as session:
        assert maintenance.read_revision_state(session, 'r1')['verified'] == 0
        assert maintenance.read_revision_state(session, 'r2')['missing'] == 2
    with database.write() as session:
        session.connection().exec_driver_sql("DELETE FROM revision_assets WHERE revision_id='r2'")
        session.connection().exec_driver_sql("DELETE FROM assets WHERE revision_id='r2'")
        session.connection().exec_driver_sql("DELETE FROM work_revisions WHERE id='r2'")
    assert_verified(database)


def test_derivative_physical_shape_owner_and_deduplication(database):
    with database.write() as session:
        session.add(revision('r1', 1))
        session.flush()
        session.get(Work, 1).default_revision_id = 'r1'
        session.add(Derivative(id='physical', work_id=1, revision_id='r1', kind='thumbnail',
            page_index=0, spec_key='v1', state='verified', relative_path='thumb.png'))
        session.flush()
        session.add_all([RevisionDerivative(revision_id='r1', kind='gif', page_index=9,
            spec_key='fake', derivative_id='physical'), RevisionDerivative(revision_id='r1', kind='thumbnail',
            page_index=0, spec_key='v1', derivative_id='physical')])
    with database.read() as session:
        assert projection.resource_totals(session.connection(), 'work:1')['thumbnail'] == 1
    with database.write() as session:
        session.connection().exec_driver_sql("UPDATE derivatives SET work_id=2 WHERE id='physical'")
    with database.read() as session:
        assert projection.resource_totals(session.connection(), 'work:1')['thumbnail'] == 0
    assert_verified(database)


@pytest.mark.parametrize('target,expected', [({'work_id': 1}, 1), ({'work_id':1.0}, 1),
    ({'work_id':True}, 1), ({'work_id':'1'}, None), ({'work_id':{'id':1}}, None),
    ({'work_id':'1','asset_id':'a'}, None), ({'asset_id':'a','record_id':'d'}, 2),
    ({'asset_id':'absent','record_id':'d'}, 3), ({'record_id':'d'}, 3)])
def test_direct_target_json_types_and_identity_precedence(database, target, expected):
    with database.write() as session:
        session.add_all([Asset(id='a', work_id=2, role='image', page_index=0),
            Derivative(id='d', work_id=3, kind='thumbnail', page_index=0, spec_key='v1')])
        session.add(operation())
        session.flush()
        session.add(OperationItem(id='item', operation_id='operation', item_key='item',
            kind='download', target=target, phase='downloading', state='running'))
    with database.read() as session:
        ids = list(session.connection().exec_driver_sql('SELECT work_id FROM read_task_candidates').scalars())
        assert ids == ([] if expected is None else [expected])
    assert_verified(database)


def test_resource_rebinding_and_delete_moves_task_membership(database):
    with database.write() as session:
        session.add(Asset(id='a', work_id=1, role='image', page_index=0))
        session.add(Derivative(id='d', work_id=3, kind='thumbnail', page_index=0))
        session.add(operation())
        session.flush()
        session.add(OperationItem(id='i', operation_id='operation', item_key='i', kind='download',
            target={'asset_id':'a','record_id':'d'}, phase='downloading', state='running'))
    assert state(database, 1)['processing']['state'] == 'running'
    with database.write() as session:
        session.connection().exec_driver_sql("UPDATE assets SET work_id=2 WHERE id='a'")
    assert state(database, 1)['processing']['state'] == 'idle'
    assert state(database, 2)['processing']['state'] == 'running'
    with database.write() as session:
        session.connection().exec_driver_sql("DELETE FROM assets WHERE id='a'")
    assert state(database, 2)['processing']['state'] == 'idle'
    assert state(database, 3)['processing']['state'] == 'running'
    assert_verified(database)


def test_parent_progress_preserves_candidates_and_competing_winners_without_manifest_rebuild(database, monkeypatch):
    created = datetime(2026, 1, 1)
    with database.write() as session:
        session.add(operation(parameters={'work_ids': [1, 2, 3]}, created_at=created, updated_at=created))
        session.flush()
        session.add_all([OperationItem(id=f'item-{i}', operation_id='operation', item_key=str(i),
            kind='verify', target={'work_id': 1 + i % 3}, phase='checking', state='pending',
            position=i) for i in range(30)])
        session.add(OperationItem(id='fallback', operation_id='operation', item_key='fallback',
            kind='error', target={}, phase='checking', state='pending', position=30))
    queries = []
    plans = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement.lower())
        if statement.startswith('WITH members AS MATERIALIZED'):
            plans.append([row[3] for row in connection.connection.driver_connection.execute(
                'EXPLAIN QUERY PLAN ' + statement, parameters)])
    event.listen(database.writer_engine, 'before_cursor_execute', capture)
    try:
        with database.write() as session:
            driver = session.connection().connection.driver_connection
            before_changes = driver.total_changes
            parent = session.get(Operation, 'operation')
            parent.phase, parent.updated_at = 'checking', created + timedelta(seconds=1)
        new_changes, new_statements = driver.total_changes - before_changes, len(queries)
        assert not any('delete from read_task_candidates' in sql or
            'insert into read_task_candidates' in sql or 'from operation_items' in sql for sql in queries)
        assert not any('delete from read_operation_scope' in sql or
            'insert into read_operation_scope' in sql or 'insert into read_work_state' in sql for sql in queries)
        assert len(plans) == 2
        assert all(any('ix_read_task_operation' in part for part in plan) and
            any('ix_read_task_winner' in part for part in plan) and
            not any('SCAN read_task_candidates' in part or 'SCAN c' in part for part in plan)
            for plan in plans)
        with database.read() as session:
            rows = session.connection().exec_driver_sql('SELECT updated_at FROM read_task_candidates').scalars().all()
            assert len(rows) == 33 and set(rows) == {'2026-01-01 00:00:01.000000'}
        assert_verified(database)

        # Reproduce the former parent + scope rebuild on the same small fixture.
        def former_scope(connection, operation_id, **kwargs):
            affected = maintenance._remove_candidates(connection, operation_id=operation_id, scope_only=True)
            connection.exec_driver_sql('DELETE FROM read_operation_scope WHERE operation_id=?', (operation_id,))
            members, values = maintenance._scope_values(connection, operation_id)
            if members:
                connection.exec_driver_sql('INSERT INTO read_operation_scope VALUES(?,?)',
                    [(operation_id, value) for value in sorted(members)])
            if values is not None:
                for work_id in members:
                    projection.task_fact(connection, work_id, 'scope:' + operation_id, **values)
                affected.update(members)
            return affected
        queries.clear()
        with monkeypatch.context() as patch:
            patch.setattr(maintenance, 'reconcile_operation_progress', lambda *args: None)
            patch.setattr(maintenance, 'reconcile_scope', former_scope)
            with database.write() as session:
                driver = session.connection().connection.driver_connection
                before_changes = driver.total_changes
                parent = session.get(Operation, 'operation')
                parent.phase_done, parent.updated_at = 1, created + timedelta(seconds=2)
        old_changes, old_statements = driver.total_changes - before_changes, len(queries)
        assert new_changes < old_changes and new_statements < old_statements
        print(f'parent_progress rows=31 works=3 candidates=33 old_changes={old_changes} new_changes={new_changes}'
            f' old_statements={old_statements} new_statements={new_statements}')
        assert_verified(database)

        with database.write() as session:
            session.add(operation('competitor', created_at=created, updated_at=created + timedelta(seconds=3)))
            session.flush()
            session.add(OperationItem(id='competing', operation_id='competitor', item_key='competing',
                kind='verify', target={'work_id': 1}, phase='checking', state='pending'))
        with database.read() as session:
            assert projection.task_winner(session.connection(), 1)['payload']['operation_id'] == 'competitor'
        queries.clear()
        with database.write() as session:
            parent = session.get(Operation, 'operation')
            parent.phase_done, parent.updated_at = 2, created + timedelta(seconds=4)
            session.flush()
            parent.detail, parent.updated_at = 'still checking', created + timedelta(seconds=5)
        assert not any('delete from read_task_candidates' in sql or 'from operation_items' in sql for sql in queries)
        with database.read() as session:
            assert projection.task_winner(session.connection(), 1)['payload']['operation_id'] == 'operation'
        assert_verified(database)

        # One direct item change must not rewrite the parent's unchanged scope.
        queries.clear()
        with database.write() as session:
            session.get(OperationItem, 'item-0').state = 'running'
        assert not any('delete from read_operation_scope' in sql or
            'insert into read_operation_scope' in sql for sql in queries)
        assert sum('insert into read_task_candidates' in sql for sql in queries) == 1
        assert sum('insert into read_work_state' in sql for sql in queries) == 1
        assert_verified(database)
    finally:
        event.remove(database.writer_engine, 'before_cursor_execute', capture)


def test_parent_progress_hint_falls_back_for_raw_sql_and_structural_flushes(database):
    with database.write() as session:
        session.add(operation(parameters={'work_ids': [1, 2]}))
        session.flush()
        session.add(OperationItem(id='item', operation_id='operation', item_key='item',
            kind='verify', target={'work_id': 1}, phase='checking', state='pending'))
    for raw_first in (False, True):
        with database.write() as session:
            parent = session.get(Operation, 'operation')
            if raw_first:
                session.connection().exec_driver_sql("UPDATE operations SET kind='repair' WHERE id='operation'")
            parent.updated_at += timedelta(seconds=1)
            session.flush()
            if not raw_first:
                session.connection().exec_driver_sql("UPDATE operations SET kind='thumbnails' WHERE id='operation'")
        assert_verified(database)
    with database.write() as session:
        parent = session.get(Operation, 'operation')
        parent.state = 'finished'
        session.flush()
        parent.updated_at += timedelta(seconds=1)
    assert_verified(database)


def test_scope_membership_parent_fanout_and_item_delete(database):
    with database.write() as session:
        session.add(operation(parameters={'work_ids':['1', 2, '3suffix']}))
    assert [state(database, i)['processing']['state'] for i in (1,2,3)] == ['queued'] * 3
    with database.write() as session:
        session.add(OperationItem(id='direct', operation_id='operation', item_key='direct',
            kind='download', target={'work_id':1}, phase='download', state='pending'))
    assert state(database, 2)['processing']['state'] == 'idle'
    with database.write() as session:
        session.add(OperationItem(id='free', operation_id='operation', item_key='free',
            kind='error', target={}, phase='checking', state='failed', result={'message':'scope failure'}))
        session.get(Operation, 'operation').state = 'finished'
    assert state(database, 2)['processing']['reason'] == 'scope failure'
    with database.write() as session:
        session.get(Operation, 'operation').parameters = {'work_ids':[3]}
    assert state(database, 2)['processing']['state'] == 'idle'
    assert state(database, 3)['processing']['reason'] == 'scope failure'
    with database.write() as session:
        session.connection().exec_driver_sql("DELETE FROM operation_items WHERE id='free'")
    assert state(database, 3)['processing']['state'] == 'idle'
    assert_verified(database)


def test_count_deletes_and_progress_preserves_cursor_generation(database):
    with database.read() as session:
        first = projection.work_page(session.connection(), limit=1)
    with database.write() as session:
        session.add(Asset(id='a', work_id=1, role='image', page_index=0))
        session.add(operation(parameters={'work_ids':[1]}))
        session.add(ApplicationState(key='maintenance_work:1', value={'status':'waiting'}))
    with database.read() as session:
        assert projection.require_ready(session.connection()) == first['generation']
        assert projection.work_page(session.connection(), cursor=first['after'])['ids'] == [2, 1]
    with database.write() as session:
        session.add(LocalPreference(work_id=1, is_hidden=True))
        session.add(RemoteBookmark(work_id=2, value=True))
        session.add(ArchiveIntent(pixiv_illust_id='3', mode='suppressed'))
    with database.read() as session:
        assert projection.common_count(session.connection()) == 1
        assert projection.common_count(session.connection(), 'favorite') == 1
        with pytest.raises(projection.CursorExpired):
            projection.work_page(session.connection(), cursor=first['after'])
    with database.write() as session:
        session.delete(session.get(Work, 2))
    with database.read() as session:
        assert projection.common_count(session.connection()) == projection.common_count(session.connection(), 'favorite') == 0
    assert_verified(database)


def test_work_update_does_not_enumerate_revision_history(database, monkeypatch):
    with database.write() as session:
        session.add_all([revision(f'h-{i}', 1, i + 1) for i in range(100)])
    refreshed = []
    original = maintenance.reconcile_revisions
    def capture(session, ids, **kwargs):
        refreshed.extend(ids)
        return original(session, ids, **kwargs)
    monkeypatch.setattr(maintenance, 'reconcile_revisions', capture)
    with database.write() as session:
        session.get(Work, 1).page_count = 3
    assert refreshed == []
    assert state(database)['archive']['total'] == 3
    assert_verified(database)


def test_direct_beats_scope_at_identical_rank_before_item_uuid(database):
    with database.write() as session:
        session.add(operation(state='finished', parameters={'work_ids':[1]}))
        session.flush()
        session.add_all([OperationItem(id='z-direct', operation_id='operation', item_key='direct',
            kind='error', target={'work_id':1}, phase='checking', state='failed', position=0,
            result={'message':'direct wins'}), OperationItem(id='a-scope', operation_id='operation', item_key='scope',
            kind='error', target={}, phase='checking', state='failed', position=0,
            result={'message':'scope loses'})])
    assert state(database)['processing']['reason'] == 'direct wins'
    assert_verified(database)


def test_task_and_ledger_only_updates_never_enumerate_resource_pages(database, monkeypatch):
    with database.write() as session:
        session.add_all([Asset(id=f'page-{i}', work_id=1, role='image', page_index=i,
            state='verified', relative_path=f'{i}.png') for i in range(100)])
        session.get(Work, 1).page_count = 100
        session.add(operation())
        session.flush()
        session.add(OperationItem(id='i', operation_id='operation', item_key='i',
            kind='download', target={'work_id':1}, phase='downloading', state='pending'))
    def expensive(*args, **kwargs):
        raise AssertionError('task-only update enumerated P resources')
    with monkeypatch.context() as patch:
        patch.setattr(maintenance, '_work_resources', expensive)
        with database.write() as session:
            session.get(OperationItem, 'i').state = 'running'
            session.add(ApplicationState(key='maintenance_work:1', value={'status':'waiting'}))
    assert state(database)['processing']['state'] == 'running'
    assert_verified(database)


def test_nonranking_operation_update_does_not_fan_out(database, monkeypatch):
    with database.write() as session:
        session.add(operation())
        session.flush()
        session.add(OperationItem(id='i', operation_id='operation', item_key='i', kind='error',
            target={'work_id':1}, phase='checking', state='pending'))
    def fanout(*args, **kwargs):
        raise AssertionError('nonranking metadata update expanded operation history')
    with monkeypatch.context() as patch:
        patch.setattr(maintenance, 'reconcile_operation', fanout)
        with database.write() as session:
            session.get(Operation, 'operation').label = 'new label only'
    assert_verified(database)


def test_asset_status_change_does_not_expand_task_target_history(database, monkeypatch):
    with database.write() as session:
        session.add(Asset(id='a', work_id=1, role='image', page_index=0))
        session.add(operation())
        session.flush()
        session.add(OperationItem(id='i', operation_id='operation', item_key='i', kind='download',
            target={'asset_id':'a'}, phase='downloading', state='pending'))
    def fanout(*args, **kwargs):
        raise AssertionError('asset status expanded historical task candidates')
    with monkeypatch.context() as patch:
        patch.setattr(maintenance, 'reconcile_item', fanout)
        with database.write() as session:
            session.get(Asset, 'a').state = 'verified'
            session.get(Asset, 'a').relative_path = 'a.png'
    assert_verified(database)


def test_single_task_item_write_vm_steps_are_independent_of_h_and_p(tmp_path, record_property):
    measurements = []
    for history in (100, 1000, 10000):
        database = ArchiveDatabase.create(tmp_path / f'synthetic-{history}.db')
        try:
            with database.write() as session:
                session.add(Artist(id=1, pixiv_user_id='1', name='synthetic'))
                session.add(Work(id=1, pixiv_illust_id='1', artist_id=1, title='synthetic', page_count=history))
                session.add(operation(state='finished'))
                session.flush()
                session.execute(Asset.__table__.insert(), [{'id':f'a-{i}', 'work_id':1, 'role':'image',
                    'page_index':i, 'state':'verified', 'relative_path':f'{i}.png'} for i in range(history)])
                session.execute(OperationItem.__table__.insert(), [{'id':f'i-{i}', 'operation_id':'operation',
                    'item_key':str(i), 'kind':'download', 'target':{'work_id':1}, 'phase':'downloading',
                    'state':'succeeded', 'position':i} for i in range(history)])
            ticks = [0]
            def tick():
                ticks[0] += 1
                return 0
            driver = None
            try:
                with database.write() as session:
                    driver = session.connection().connection.driver_connection
                    driver.set_progress_handler(tick, 1)
                    session.connection().exec_driver_sql("UPDATE operation_items SET state='failed' WHERE id='i-0'")
            finally:
                if driver is not None:
                    driver.set_progress_handler(None, 0)
            measurements.append((history, ticks[0]))
        finally:
            database.close()
    record_property('single_item_h_p_vm_steps', repr(measurements))
    print('single_item_h_p_vm_steps:', measurements)
    counts = [count for _, count in measurements]
    assert max(counts) <= min(counts) * 1.15 + 30, measurements


@pytest.mark.parametrize('alteration', [
    "DROP TRIGGER read_generation_works_update",
    "DROP INDEX ix_read_task_winner",
    "DROP TABLE read_revision_state",
    "DROP TRIGGER read_capture_assets_update; CREATE TRIGGER read_capture_assets_update AFTER UPDATE ON assets BEGIN SELECT 1; END",
    "DROP INDEX ix_read_task_winner; CREATE INDEX ix_read_task_winner ON read_task_candidates(work_id,item_id)",
])
def test_schema_contract_rejects_missing_and_same_name_wrong_objects(database, alteration):
    from src.core.database import UpgradeRequired
    with database.writer_engine.begin() as connection:
        for statement in alteration.split('; ', 1):
            connection.exec_driver_sql(statement)
    with pytest.raises(UpgradeRequired):
        ArchiveDatabase(database.path)
    with database.writer_session() as session:
        with pytest.raises(projection.ProjectionUnavailable):
            maintenance.verify_projections(session)


def test_external_ddl_cannot_disable_capture_after_database_open(database):
    from src.core.database import UpgradeRequired
    with database.writer_engine.begin() as connection:
        connection.exec_driver_sql('DROP TRIGGER read_capture_works_update')
        connection.exec_driver_sql('UPDATE works SET page_count=99 WHERE id=1')
    # Even with no surviving capture marker, the original owner fails closed.
    with pytest.raises(UpgradeRequired):
        with database.read():
            pass
    with pytest.raises(UpgradeRequired):
        with database.write():
            pass


def test_metadata_only_revision_write_refreshes_display_without_resource_scan(database, monkeypatch):
    from src.core import display_metadata
    with database.write() as session:
        session.add(revision('r1', 1, source_metadata={'tag_translations':{'tag':'old'}}))
    def expensive(*args, **kwargs):
        raise AssertionError('metadata-only revision update enumerated P resource facts')
    with monkeypatch.context() as patch:
        patch.setattr(maintenance, '_revision_resources', expensive)
        with database.write() as session:
            session.get(WorkRevision, 'r1').source_metadata = {'tag_translations':{'tag':'new'}, 'urls':['synthetic'] * 100}
    with database.read() as session:
        assert display_metadata.read(session, ['r1'], revisions=True, include_translations=True)['r1']['tag_translations'] == {'tag':'new'}
    assert_verified(database)


def test_task_only_write_does_not_select_source_metadata_bytes(database):
    from sqlalchemy import event
    with database.write() as session:
        session.get(Work, 1).source_metadata = {'urls':['synthetic-url'] * 1000}
        session.add(operation())
        session.flush()
        session.add(OperationItem(id='i', operation_id='operation', item_key='i', kind='download',
            target={'work_id':1}, phase='downloading', state='pending'))
    statements = []
    def capture(connection, cursor, sql, params, context, many):
        if sql.lstrip().upper().startswith('SELECT'):
            statements.append(sql)
    event.listen(database.writer_engine, 'before_cursor_execute', capture)
    try:
        with database.write() as session:
            session.connection().exec_driver_sql("UPDATE operation_items SET state='running' WHERE id='i'")
    finally:
        event.remove(database.writer_engine, 'before_cursor_execute', capture)
    assert not any('works.source_metadata' in statement for statement in statements), statements
    assert state(database)['processing']['state'] == 'running'


def test_readiness_marker_is_scoped_to_owned_read_transaction(database):
    with database.read() as session:
        assert isinstance(session.info['read_projection_generation'], int)
        assert projection.session_generation(session) >= 1
        retained = session
    assert 'read_projection_generation' not in retained.info
    with database.write() as session:
        assert 'read_projection_generation' not in session.info


def test_derivative_update_does_not_refresh_all_bound_revision_archives(database, monkeypatch):
    with database.write() as session:
        session.add_all([revision(f'r-{i}', 1, i + 1) for i in range(20)])
        session.flush()
        session.get(Work, 1).default_revision_id = 'r-0'
        session.add(Derivative(id='d', work_id=1, revision_id='r-0', kind='thumbnail',
            page_index=0, state='missing', spec_key='v1'))
        session.flush()
        session.add_all([RevisionDerivative(revision_id=f'r-{i}', kind='thumbnail', page_index=0,
            spec_key='v1', derivative_id='d') for i in range(20)])
    refreshed = []
    original = maintenance.reconcile_revisions
    def capture(session, ids, **kwargs):
        refreshed.extend(ids)
        return original(session, ids, **kwargs)
    monkeypatch.setattr(maintenance, 'reconcile_revisions', capture)
    with database.write() as session:
        session.get(Derivative, 'd').state = 'verified'
        session.get(Derivative, 'd').relative_path = 'd.png'
    assert refreshed == []
    with database.read() as session:
        assert projection.resource_totals(session.connection(), 'work:1')['thumbnail'] == 1
    assert_verified(database)


def test_physical_readiness_delta_never_replaces_resource_scopes(database, monkeypatch):
    with database.write() as session:
        session.add(revision('r1', 1))
        session.flush()
        work = session.get(Work, 1)
        work.default_revision_id = work.latest_revision_id = 'r1'
        session.add_all([Asset(id='a', work_id=1, revision_id='r1', role='image', page_index=0),
            Derivative(id='d', work_id=1, revision_id='r1', kind='thumbnail', page_index=0, spec_key='v1')])
        session.flush()
        session.add_all([RevisionAsset(revision_id='r1', role='image', page_index=0, asset_id='a'),
            RevisionDerivative(revision_id='r1', kind='thumbnail', page_index=0, spec_key='v1', derivative_id='d')])
    def full_scope(*args, **kwargs):
        raise AssertionError('single physical readiness edit enumerated sibling resources')
    with monkeypatch.context() as patch:
        patch.setattr(maintenance, '_work_resources', full_scope)
        patch.setattr(maintenance, '_revision_resources', full_scope)
        for physical_state, path, verified, thumbnail in (
            ('verified', 'isolated.png', 1, 1), ('failed', None, 0, 0),
            ('unknown', 'isolated.png', 0, 0), ('verified', 'isolated.png', 1, 1)):
            with database.write() as session:
                session.connection().exec_driver_sql('UPDATE assets SET state=?,relative_path=? WHERE id=?', (physical_state, path, 'a'))
                session.connection().exec_driver_sql('UPDATE derivatives SET state=?,relative_path=? WHERE id=?', (physical_state, path, 'd'))
            with database.read() as session:
                assert projection.resource_totals(session.connection(), 'work:1')['verified'] == verified
                assert projection.resource_totals(session.connection(), 'work:1')['thumbnail'] == thumbnail
                assert maintenance.read_revision_state(session, 'r1')['verified'] == verified
    assert_verified(database)


def test_single_asset_write_vm_and_entities_are_independent_of_p(tmp_path, record_property):
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from src.core.revisions import refresh_for_record
    measurements = []
    for pages in (100, 1000, 10000):
        database = ArchiveDatabase.create(tmp_path / f'synthetic-asset-{pages}.db')
        try:
            with database.write() as session:
                session.add(Artist(id=1, pixiv_user_id='1', name='synthetic'))
                session.add(Work(id=1, pixiv_illust_id='1', artist_id=1, title='r1', type='manga', page_count=pages))
                session.flush()
                session.add(revision('r1', 1, pages + 1, page_count=pages, source_metadata={'urls':['synthetic'] * pages}))
                session.execute(WorkRevision.__table__.insert(), [{'id':f'h-{i}', 'work_id':1,
                    'ordinal':i + 1, 'title':f'old-{i}', 'type':'manga', 'page_count':1,
                    'source_signature':f'old-{i}', 'state':'pending', 'viewable':False} for i in range(pages)])
                session.flush()
                work = session.get(Work, 1)
                work.default_revision_id = work.latest_revision_id = 'r1'
                work.source_metadata = {'urls':['synthetic'] * pages}
                session.execute(Asset.__table__.insert(), [{'id':f'a-{i}', 'work_id':1, 'revision_id':'r1',
                    'role':'image', 'page_index':i, 'state':'missing'} for i in range(pages)])
                session.execute(RevisionAsset.__table__.insert(), [{'revision_id':'r1', 'role':'image',
                    'page_index':i, 'asset_id':f'a-{i}'} for i in range(pages)])
            ticks, loaded, selected_sql = [0], [], []
            def tick():
                ticks[0] += 1
                return 0
            def capture(session, entity):
                if isinstance(entity, Asset):
                    loaded.append(entity.id)
            def capture_sql(connection, cursor, statement, parameters, context, many):
                if statement.lstrip().upper().startswith('SELECT'):
                    selected_sql.append(statement)
            driver = None
            event.listen(Session, 'loaded_as_persistent', capture)
            event.listen(database.writer_engine, 'before_cursor_execute', capture_sql)
            try:
                with database.write() as session:
                    driver = session.connection().connection.driver_connection
                    driver.set_progress_handler(tick, 1)
                    asset = session.get(Asset, 'a-0')
                    asset.state, asset.relative_path = 'verified', 'isolated.png'
                    refresh_for_record(session, asset)
            finally:
                event.remove(Session, 'loaded_as_persistent', capture)
                event.remove(database.writer_engine, 'before_cursor_execute', capture_sql)
                if driver is not None:
                    driver.set_progress_handler(None, 0)
            assert loaded == ['a-0'], (pages, loaded)
            assert not any('source_metadata' in statement for statement in selected_sql), (pages, selected_sql)
            with database.read() as session:
                assert maintenance.read_revision_state(session, 'r1')['verified'] == 1
                assert projection.resource_totals(session.connection(), 'work:1')['verified'] == 1
                assert session.scalar(select(WorkRevision.state).where(WorkRevision.id == 'r1')) == 'partial'
                plan = session.connection().exec_driver_sql("EXPLAIN QUERY PLAN SELECT id FROM work_revisions WHERE work_id=? AND state != 'redundant' ORDER BY (CASE WHEN state='complete' AND viewable=1 THEN 2 WHEN viewable=1 THEN 1 ELSE 0 END) DESC,ordinal DESC LIMIT 1", (1,)).all()
                assert any('SEARCH work_revisions USING INDEX ix_revision_selection' in row[3] for row in plan), plan
                assert not any('TEMP B-TREE' in row[3] or 'SCAN' in row[3] for row in plan), plan
            measurements.append((pages, ticks[0]))
        finally:
            database.close()
    record_property('single_asset_full_refresh_p_h_vm_steps', repr(measurements))
    print('single_asset_full_refresh_p_h_vm_steps:', measurements)
    counts = [count for _, count in measurements]
    assert max(counts) <= min(counts) * 1.15 + 30, measurements


def test_startup_rejects_multiple_schema_revision_rows(database):
    from src.core.database import UpgradeRequired
    with database.writer_engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO alembic_version(version_num) VALUES('foreign_revision')")
    with pytest.raises(UpgradeRequired, match='唯一一行'):
        ArchiveDatabase(database.path)


def test_cached_revision_lifecycle_preserves_logical_ugoira_slots_and_rolls_back(database, monkeypatch):
    from src.core.revisions import refresh_for_record, revision_facts
    with database.write() as session:
        session.add(revision('animation', 1, type='ugoira'))
        session.flush()
        work = session.get(Work, 1)
        work.type, work.default_revision_id, work.latest_revision_id = 'ugoira', 'animation', 'animation'
        session.add_all([Asset(id='zip', work_id=2, role='image', page_index=99, state='unknown', relative_path='zip.bin'),
            Asset(id='timing', work_id=2, role='image', page_index=98, state='verified', relative_path='timing.json'),
            Derivative(id='gif', work_id=2, kind='thumbnail', page_index=77, spec_key='other', state='unknown', relative_path='gif.bin')])
        session.flush()
        session.add_all([RevisionAsset(revision_id='animation', role='ugoira_zip', page_index=9, asset_id='zip'),
            RevisionAsset(revision_id='animation', role='ugoira_timing', page_index=8, asset_id='timing'),
            RevisionDerivative(revision_id='animation', kind='gif', page_index=7, spec_key='other', derivative_id='gif')])
    with database.write() as session:
        record = session.get(Derivative, 'gif')
        refresh_for_record(session, record)
        rev = session.get(WorkRevision, 'animation')
        assert maintenance.cached_revision_facts(session, rev) == revision_facts(session, rev) == {'complete':False, 'viewable':True, 'state':'partial'}
    def fail(*args, **kwargs):
        raise RuntimeError('injected after lifecycle delta')
    with monkeypatch.context() as patch:
        patch.setattr(maintenance, 'reconcile_revisions', fail)
        with pytest.raises(RuntimeError, match='after lifecycle'):
            with database.write() as session:
                record = session.get(Asset, 'zip')
                record.state = 'verified'
                refresh_for_record(session, record)
                assert session.connection().exec_driver_sql('SELECT verified FROM read_revision_lifecycle_totals WHERE revision_id=?', ('animation',)).scalar() == 2
    with database.read() as session:
        assert session.get(Asset, 'zip').state == 'unknown'
        assert session.get(WorkRevision, 'animation').state == 'partial'
        assert session.connection().exec_driver_sql('SELECT verified FROM read_revision_lifecycle_totals WHERE revision_id=?', ('animation',)).scalar() == 1
    assert_verified(database)


def test_missing_lifecycle_fact_is_not_implicitly_rebuilt_by_resource_writer(database):
    from src.core.revisions import refresh_for_record
    with database.write() as session:
        session.add(revision('r1', 1))
        session.flush()
        session.add(Asset(id='a', work_id=1, role='image', page_index=0))
        session.flush()
        session.add(RevisionAsset(revision_id='r1', role='image', page_index=0, asset_id='a'))
    with database.writer_engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM read_revision_lifecycle_totals WHERE revision_id='r1'")
    with pytest.raises(projection.ProjectionUnavailable, match='lifecycle fact is missing'):
        with database.write() as session:
            asset = session.get(Asset, 'a')
            asset.state, asset.relative_path = 'verified', 'isolated.png'
            refresh_for_record(session, asset)
    with database.writer_engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1 FROM read_revision_lifecycle_totals WHERE revision_id='r1'").first() is None
        assert connection.exec_driver_sql("SELECT state FROM assets WHERE id='a'").scalar() == 'unknown'


@pytest.mark.parametrize('tamper,match', [
    ("DELETE FROM read_resource_totals WHERE scope='work:1'", 'aggregate is missing'),
    ("INSERT INTO read_resource_totals(scope) VALUES('work:999')", 'orphan resource scope'),
    ("INSERT INTO read_resource_totals(scope) VALUES('work:01')", 'orphan resource scope'),
    ("INSERT INTO read_resource_totals(scope) VALUES('revision:absent')", 'orphan resource scope'),
    ("INSERT INTO read_resource_totals(scope) VALUES('invalid:1'); INSERT INTO read_resource_facts VALUES('invalid:1','invalid','invalid',0,0,0,0,0)", 'orphan resource scope'),
])
def test_explicit_verifier_rejects_missing_resource_aggregates_and_orphan_scopes(database, tamper, match):
    with database.write() as session:
        session.add(Asset(id='a', work_id=1, role='image', page_index=0, state='verified', relative_path='a.png'))
    with database.writer_engine.begin() as connection:
        for statement in tamper.split('; '):
            connection.exec_driver_sql(statement)
    with database.read() as session:
        with pytest.raises(projection.ProjectionUnavailable, match=match):
            maintenance.verify_projections(session)


def test_empty_resource_scope_totals_are_valid_but_nonempty_missing_totals_block_state_writer(database):
    with database.writer_engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO read_resource_totals(scope) VALUES('work:2')")
    assert_verified(database)
    with database.write() as session:
        session.add(Asset(id='a', work_id=1, role='image', page_index=0, state='verified', relative_path='a.png'))
    with database.writer_engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM read_resource_totals WHERE scope='work:1'")
    with pytest.raises(projection.ProjectionUnavailable, match='aggregate is missing'):
        with database.write() as session:
            session.connection().exec_driver_sql("UPDATE works SET caption='isolated' WHERE id=1")
    with database.writer_engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT caption FROM works WHERE id=1').scalar() == ''
        assert connection.exec_driver_sql("SELECT 1 FROM read_resource_totals WHERE scope='work:1'").first() is None
