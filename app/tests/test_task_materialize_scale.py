"""Append manifests without scanning settled history; preserve order and dependencies."""
from contextlib import contextmanager
import json

import pytest
from sqlalchemy import create_engine, event, insert, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.core.database import ArchiveDatabase
from src.core.models import Base, Operation, OperationItem
from src.core.tasks import ItemSpec, TaskQueue


class MemoryDatabase:
    def __init__(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
    @contextmanager
    def read(self):
        with Session(self.engine) as session:
            yield session
    @contextmanager
    def write(self):
        with Session(self.engine) as session, session.begin():
            yield session
    def close(self):
        self.engine.dispose()


def seed(database, count):
    with database.write() as session:
        session.add(Operation(id='operation', kind='check', parameters={}, dedupe_key='synthetic', label='synthetic'))
        session.flush()
        session.execute(insert(OperationItem), [{'id': f'item-{index}', 'operation_id': 'operation',
            'item_key': f'old-{index}', 'position': index, 'kind': 'checked', 'target': {},
            'phase': 'checking', 'state': ['succeeded', 'future_state', 'pending'][index % 3],
            'lane': [None, 'local', 'legacy_lane', ''][index % 4]} for index in range(count)])


@contextmanager
def query_probe(database):
    records, active = [], {}
    def finish(driver):
        record = active.pop(driver, None)
        if record is not None:
            driver.set_progress_handler(None, 0)
    def before(connection, cursor, sql, parameters, context, many):
        driver = connection.connection.driver_connection
        finish(driver)
        record = {'sql': sql, 'vm_steps': 0}
        cursor.execute('EXPLAIN QUERY PLAN ' + sql, parameters)
        record['plan'] = [list(row) for row in cursor.fetchall()]
        records.append(record)
        active[driver] = record
        def progress():
            record['vm_steps'] += 1
            return 0
        driver.set_progress_handler(progress, 1)
    def checkin(driver, record):
        if driver is not None:
            finish(driver)
    event.listen(database.engine, 'before_cursor_execute', before)
    event.listen(database.engine, 'checkin', checkin)
    try:
        yield records
    finally:
        for driver in list(active):
            finish(driver)
        event.remove(database.engine, 'before_cursor_execute', before)
        event.remove(database.engine, 'checkin', checkin)


@pytest.mark.parametrize('specs', [[], [ItemSpec('old-0', 'checked', {}, 'checking')]])
def test_noop_materialization_never_reads_position(specs):
    database = MemoryDatabase()
    try:
        seed(database, 1)
        queue = TaskQueue(database, {})
        with query_probe(database) as records, database.write() as session:
            pending = Operation(kind='check', parameters={}, dedupe_key='pending', label='flush contract')
            session.add(pending)
            queue.materialize_in_transaction(session, 'operation', specs)
            assert pending.id is not None
        assert not any('position' in row['sql'].lower() for row in records), records
        assert queue._wake.is_set()
    finally:
        database.close()


@pytest.mark.parametrize('count', [1000, 16000])
def test_append_position_cost_is_bounded_across_saved_states_and_lanes(count, tmp_path):
    database = MemoryDatabase()
    try:
        seed(database, count)
        queue = TaskQueue(database, {})
        with query_probe(database) as records, database.write() as session:
            queue.materialize_in_transaction(session, 'operation', [ItemSpec('new', 'checked', {}, 'checking')])
        with database.read() as session:
            assert session.scalar(select(OperationItem.position).where(OperationItem.item_key == 'new')) == count
        (tmp_path / 'materialize-vm.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
        assert sum(row['vm_steps'] for row in records) < 3000, records
    finally:
        database.close()


def test_file_manifest_preserves_duplicate_keys_dependencies_and_high_unknown_position(tmp_path):
    database = ArchiveDatabase.create(tmp_path / 'small.db')
    try:
        seed(database, 4)
        with database.write() as session:
            record = session.get(OperationItem, 'item-1')
            record.position, record.state, record.lane = 999, 'future-state-v2', None
        queue = TaskQueue(database, {})
        specs = [ItemSpec('old-0', 'checked', {}, 'checking', depends_on=('missing-ignored',)),
                 ItemSpec('first', 'checked', {'value': 1}, 'checking', depends_on=('old-0',)),
                 ItemSpec('first', 'checked', {'value': 2}, 'checking', depends_on=('missing-ignored',)),
                 ItemSpec('second', 'checked', {}, 'checking', depends_on=('first',))]
        with database.write() as session:
            queue.materialize_in_transaction(session, 'operation', specs)
        with database.read() as session:
            first = session.scalar(select(OperationItem).where(OperationItem.item_key == 'first'))
            second = session.scalar(select(OperationItem).where(OperationItem.item_key == 'second'))
            assert (first.position, second.position) == (1000, 1001)
            assert first.target == {'value': 1} and second.depends_on == ['first']
        for lane, highest, key in [('future-lane-v2', 1999, 'unknown-lane'), ('', 2999, 'empty-lane')]:
            with database.write() as session:
                record = session.get(OperationItem, 'item-1')
                record.position, record.lane = highest, lane
                queue.materialize_in_transaction(session, 'operation', [ItemSpec(key, 'checked', {}, 'checking')])
            with database.read() as session:
                assert session.scalar(select(OperationItem.position).where(OperationItem.item_key == key)) == highest + 1
        with pytest.raises(ValueError, match='清单依赖顺序不合法'):
            with database.write() as session:
                queue.materialize_in_transaction(session, 'operation', [
                    ItemSpec('rolled-back', 'checked', {}, 'checking'),
                    ItemSpec('invalid', 'checked', {}, 'checking', depends_on=('later',))])
        with database.read() as session:
            assert session.scalar(select(OperationItem.id).where(OperationItem.item_key == 'rolled-back')) is None
        with database.write() as session:
            session.add(Operation(id='empty', kind='check', parameters={}, dedupe_key='empty', label='synthetic empty'))
            queue.materialize_in_transaction(session, 'empty', [ItemSpec('empty-first', 'checked', {}, 'checking')])
        with database.read() as session:
            assert session.scalar(select(OperationItem.position).where(OperationItem.item_key == 'empty-first')) == 0
    finally:
        database.close()
