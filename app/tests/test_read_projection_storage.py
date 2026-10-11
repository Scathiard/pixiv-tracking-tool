"""Synthetic storage tests; these do not certify a fully integrated library API."""
import pytest
from sqlalchemy import create_engine
from src.core import read_projection as projection


@pytest.fixture
def connection():
    engine = create_engine('sqlite://')
    with engine.connect() as connection:
        with connection.begin():
            projection.install(connection)
            projection.finish_verified_batch(connection)
        yield connection
    engine.dispose()


def test_current_and_revision_facts_remain_distinct_and_idempotent(connection):
    projection.resource_fact(connection, 'work:1', 'original:0', 'file', recorded=1, verified=1)
    projection.resource_fact(connection, 'revision:r1', 'original:0', 'file', recorded=1, verified=1)
    projection.resource_fact(connection, 'work:1', 'original:0', 'file', recorded=1, verified=1)
    assert projection.resource_totals(connection, 'work:1')['verified'] == 1
    projection.resource_fact(connection, 'work:1', 'original:0', 'replacement', recorded=1, missing=1)
    assert projection.resource_totals(connection, 'work:1')['missing'] == 1
    assert projection.resource_totals(connection, 'revision:r1')['verified'] == 1
    projection.resource_fact(connection, 'work:1', 'original:0')
    assert projection.resource_totals(connection, 'work:1')['recorded'] == 0


def test_source_and_projection_rollback_together(connection):
    connection.exec_driver_sql('CREATE TABLE synthetic_source(id INTEGER PRIMARY KEY)')
    connection.commit()
    with pytest.raises(RuntimeError):
        with connection.begin():
            connection.exec_driver_sql('INSERT INTO synthetic_source VALUES(1)')
            projection.work_fact(connection, 1, order_key='2026', artist_id=1)
            projection.resource_fact(connection, 'work:1', 'original:0', 'file', recorded=1)
            raise RuntimeError('injected maintenance failure')
    assert connection.exec_driver_sql('SELECT count(*) FROM synthetic_source').scalar() == 0
    assert projection.common_count(connection) == 0
    assert projection.resource_totals(connection, 'work:1')['recorded'] == 0


def test_dirty_is_fail_closed_and_verified_generation_expires_cursor(connection):
    projection.work_fact(connection, 1, order_key='2026')
    cursor = projection.work_page(connection)['after']
    projection.mark_dirty(connection, 'work', 1)
    projection.mark_dirty(connection, 'work', 1)
    assert connection.exec_driver_sql('SELECT count(*) FROM read_model_dirty').scalar() == 1
    with pytest.raises(projection.ProjectionUnavailable):
        projection.work_page(connection)
    with pytest.raises(projection.ProjectionUnavailable):
        projection.finish_verified_batch(connection)
    connection.exec_driver_sql('DELETE FROM read_model_dirty WHERE entity=? AND identity=?', ('work', '1'))
    projection.finish_verified_batch(connection)
    with pytest.raises(projection.CursorExpired):
        projection.work_page(connection, cursor=cursor)


def test_counts_visibility_scope_changes_and_deletion(connection):
    projection.work_fact(connection, 1, order_key='a', artist_id=7, favorite=True)
    projection.work_fact(connection, 2, order_key='b', artist_id=7)
    assert projection.common_count(connection) == 2
    assert projection.common_count(connection, 'artist:7') == 2
    assert projection.common_count(connection, 'favorite') == 1
    projection.work_fact(connection, 1, order_key='a', visible=False, artist_id=9)
    assert projection.common_count(connection) == 1
    assert projection.common_count(connection, 'artist:7') == 1
    assert projection.common_count(connection, 'favorite') == 0
    projection.work_fact(connection, 2)
    assert projection.common_count(connection) == 0


def test_bidirectional_ties_scope_and_unsupported_filter(connection):
    for work in range(1, 8):
        projection.work_fact(connection, work, order_key='same', artist_id=1)
    first = projection.work_page(connection, limit=3)
    second = projection.work_page(connection, limit=3, cursor=first['after'])
    previous = projection.work_page(connection, limit=3, cursor=second['before'], reverse=True)
    assert first['ids'] == [7, 6, 5]
    assert second['ids'] == [4, 3, 2]
    assert previous['ids'] == first['ids']
    assert projection.work_page(connection, scope='artist:2')['ids'] == []
    with pytest.raises(projection.CursorExpired):
        projection.work_page(connection, scope='favorite', cursor=first['after'])
    with pytest.raises(ValueError, match='on-demand'):
        projection.work_page(connection, scope='arbitrary:text')


def test_task_priority_updates_delete_and_payload(connection):
    projection.task_fact(connection, 1, 'old', priority=2, created_at='2026-02', payload={'state':'failed'})
    projection.task_fact(connection, 1, 'active', priority=0, created_at='2026-01', payload={'state':'running'})
    assert projection.task_winner(connection, 1)['candidate_id'] == 'active'
    projection.task_fact(connection, 1, 'active')
    assert projection.task_winner(connection, 1)['payload'] == {'state':'failed'}
    assert projection.task_winner(connection, 2) is None


def _measure(connection, action):
    driver = connection.connection.driver_connection
    steps = [0]
    def progress():
        steps[0] += 1
        return 0
    driver.set_progress_handler(progress, 1)
    try:
        result = action()
    finally:
        driver.set_progress_handler(None, 0)
    return steps[0], result


def test_fixed_k_storage_work_does_not_scan_n_h_p(record_property):
    results = []
    for n in (1000, 10000, 100000):
        engine = create_engine('sqlite://')
        with engine.begin() as connection:
            projection.install(connection)
            connection.exec_driver_sql('INSERT INTO read_work_order(work_id,order_key,visible,artist_id,work_type,favorite) VALUES(?,?,?,?,?,?)', [(i, f'{i:09}', 1, 1, 'illust', 0) for i in range(1, n+1)])
            connection.exec_driver_sql('INSERT INTO read_common_counts VALUES(?,?)', ('visible', n))
            connection.exec_driver_sql('INSERT INTO read_task_candidates VALUES(?,?,?,?,?,?,?,?,?,?)', [(1, f'c{i}', 2, f'{i:09}', '', 1, i, f'o{i}', f'i{i}', '{}') for i in range(n)])
            connection.exec_driver_sql('INSERT INTO read_resource_facts VALUES(?,?,?,?,?,?,?,?)', [('work:1', f'p{i}', f'f{i}', 1, 1, 0, 0, 0) for i in range(n)])
            connection.exec_driver_sql('INSERT INTO read_resource_totals VALUES(?,?,?,?,?,?)', ('work:1', n, n, 0, 0, 0))
            projection.finish_verified_batch(connection)
            first_steps, page = _measure(connection, lambda: projection.work_page(connection, limit=20))
            deep_cursor = projection.Cursor(page['generation'], 'visible', f'{n//2:09}', n//2)
            deep_steps, deep = _measure(connection, lambda: projection.work_page(connection, limit=20, cursor=deep_cursor))
            task_steps, winner = _measure(connection, lambda: projection.task_winner(connection, 1))
            totals_steps, totals = _measure(connection, lambda: projection.resource_totals(connection, 'work:1'))
            update_steps, _ = _measure(connection, lambda: projection.resource_fact(connection, 'work:1', 'p0', 'f0', recorded=1, missing=1))
            count_steps, count = _measure(connection, lambda: projection.common_count(connection))
            assert len(page['ids']) == len(deep['ids']) == 20
            assert winner['candidate_id'] == f'c{n-1}'
            assert totals['verified'] == count == n
            results.append((n, first_steps, deep_steps, task_steps, totals_steps, update_steps, count_steps))
            plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN SELECT candidate_id FROM read_task_candidates WHERE work_id=1 ORDER BY priority,created_at DESC,updated_at DESC,failure_priority,position,operation_id DESC,(CASE WHEN substr(candidate_id,1,6)='scope:' THEN 1 ELSE 0 END),item_id LIMIT 1").all()
            assert any('ix_read_task_winner' in row[3] for row in plan)
            assert not any('TEMP B-TREE' in row[3] for row in plan)
        engine.dispose()
    record_property('fixed_k_vm_steps', repr(results))
    print('fixed_k_vm_steps:', results)
    for index in range(1, 7):
        values = [row[index] for row in results]
        assert max(values) <= min(values)*1.15 + 20, (index, results)

def test_installer_requires_transaction_and_ddl_rolls_back():
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        with pytest.raises(RuntimeError, match="explicit transaction"):
            projection.install(connection)
        with pytest.raises(RuntimeError, match="injected"):
            with connection.begin():
                projection.install(connection)
                raise RuntimeError("injected install failure")
        assert connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name LIKE 'read_%'").all() == []
    engine.dispose()
