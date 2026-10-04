"""Startup index preparation uses tiny synthetic libraries, never a real account."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest
from src.core import index_preparation, task_indexes
from src.core.database import ArchiveDatabase, SCHEMA_REVISION, UpgradeRequired
from src.core.models import ApplicationState, Operation, OperationItem
from src.core.ownership import LibraryInUse, LibraryLease
from src.core.runtime import Runtime


def old_current_database(path, monkeypatch):
    # Generate the pre-index 0003 shape without dropping any index or file.
    with monkeypatch.context() as patch:
        patch.setattr(task_indexes, 'create_task_indexes', lambda connection: ())
        database = ArchiveDatabase.create(path)
    with database.write() as session:
        session.add(ApplicationState(key='synthetic-preference', value={'theme': 'dark'}))
        session.add(Operation(id='pending-op', kind='repair', label='synthetic',
                              dedupe_key='synthetic', parameters={}))
        session.flush()
        session.add(OperationItem(id='pending-item', operation_id='pending-op',
            item_key='asset', kind='download', target={'asset_id': 'synthetic'}, phase='download',
            state='pending', attempts=2, lane=None, depends_on=[]))
    return database


def inspect_source(path):
    with closing(sqlite3.connect(path)) as connection:
        return {
            'revision': connection.execute('SELECT version_num FROM alembic_version').fetchone()[0],
            'indexes': dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='index'")),
            'preferences': connection.execute('SELECT key,value FROM application_state ORDER BY key').fetchall(),
            'items': connection.execute('SELECT id,state,attempts,target FROM operation_items ORDER BY id').fetchall(),
        }


def test_new_library_indexes_share_creation_transaction_and_no_backup(tmp_path):
    runtime = Runtime(tmp_path/'data'/'archive.db', tmp_path/'images', create=True)
    try:
        assert runtime.index_preparation is None
        contents = inspect_source(runtime.database.path)
        assert contents['revision'] == SCHEMA_REVISION
        assert set(task_indexes.TASK_INDEX_SQL) <= contents['indexes'].keys()
        assert not (tmp_path/'data'/'upgrade').exists()
    finally:
        assert runtime.close()


def test_readonly_constructor_does_not_prepare_or_backup(tmp_path, monkeypatch):
    database = old_current_database(tmp_path/'archive.db', monkeypatch)
    database.close()
    before = inspect_source(database.path)
    reopened = ArchiveDatabase(database.path)
    reopened.close()
    assert inspect_source(database.path) == before
    assert not (tmp_path/'upgrade').exists()


def test_runtime_prepares_before_queue_and_backup_includes_wal(tmp_path, monkeypatch):
    database = old_current_database(tmp_path/'data'/'archive.db', monkeypatch)
    database.close()
    messages = []
    with closing(sqlite3.connect(database.path)) as wal_owner:
        wal_owner.execute('PRAGMA wal_autocheckpoint=0')
        wal_owner.execute("INSERT INTO application_state(key,value) VALUES ('wal-record','42')")
        wal_owner.commit()
        assert database.path.with_name(database.path.name+'-wal').stat().st_size > 0
        before = inspect_source(database.path)
        import src.core.runtime as runtime_module
        actual_queue = runtime_module.TaskQueue

        def checked_queue(*args, **kwargs):
            assert messages == ['正在备份本地图库，准备任务查询索引',
                                '正在准备本地任务查询索引，原图片与任务保留']
            assert set(task_indexes.TASK_INDEX_SQL) <= inspect_source(database.path)['indexes'].keys()
            with pytest.raises(LibraryInUse):
                LibraryLease(tmp_path/'images').acquire()
            return actual_queue(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(runtime_module, 'TaskQueue', checked_queue)
            runtime = Runtime(database.path, tmp_path/'images', index_progress=messages.append)
        try:
            receipt = runtime.index_preparation
            assert receipt['state'] == 'complete'
            backup = inspect_source(Path(receipt['backup']))
            assert backup == before
            after = inspect_source(database.path)
            assert after['revision'] == SCHEMA_REVISION
            for key in ('preferences','items'):
                assert after[key] == before[key]
            assert json.loads((Path(receipt['backup']).parent/'receipt.json').read_text('utf-8')) == receipt
        finally:
            assert runtime.close()
    directories = list((tmp_path/'data'/'upgrade').iterdir())
    with monkeypatch.context() as patch:
        patch.setattr(index_preparation, '_backup', lambda *args: pytest.fail('restart backed up again'))
        patch.setattr(index_preparation, 'create_task_indexes', lambda *args: pytest.fail('restart issued DDL'))
        restarted = Runtime(database.path, tmp_path/'images')
        assert restarted.index_preparation is None
        assert restarted.close()
    assert list((tmp_path/'data'/'upgrade').iterdir()) == directories


def test_existing_prepare_failure_rolls_back_ddl_and_retains_backup(tmp_path, monkeypatch):
    database = old_current_database(tmp_path/'archive.db', monkeypatch)
    before = inspect_source(database.path)

    def failing_creation(connection):
        backup = next((tmp_path/'upgrade').glob('*/backup.db'))
        assert inspect_source(backup) == before  # Backup must complete BEFORE DDL.
        connection.exec_driver_sql(next(iter(task_indexes.TASK_INDEX_SQL.values())))
        raise RuntimeError('synthetic private remote detail must not reach receipt')

    monkeypatch.setattr(index_preparation, 'create_task_indexes', failing_creation)
    try:
        with LibraryLease(tmp_path/'images') as lease:
            with pytest.raises(index_preparation.TaskIndexPreparationError) as failure:
                index_preparation.prepare_task_indexes(database, lease)
        assert 'synthetic private' not in str(failure.value)
        assert inspect_source(database.path) == before
        receipt_path = next((tmp_path/'upgrade').glob('*/receipt.json'))
        receipt = json.loads(receipt_path.read_text('utf-8'))
        assert receipt['state'] == 'failed' and receipt['error_code'] == 'RuntimeError'
        assert 'synthetic private' not in receipt_path.read_text('utf-8')
        assert inspect_source(Path(receipt['backup'])) == before
    finally:
        database.close()


def test_same_name_wrong_definition_safe_error_without_replacement(tmp_path, monkeypatch):
    database = old_current_database(tmp_path/'archive.db', monkeypatch)
    name = next(iter(task_indexes.TASK_INDEX_SQL))
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE INDEX "{name}" ON operation_items(position)')
    before = inspect_source(database.path)
    try:
        with LibraryLease(tmp_path/'images') as lease:
            with pytest.raises(index_preparation.TaskIndexDefinitionMismatch, match='未删除或重建'):
                index_preparation.prepare_task_indexes(database, lease)
        assert inspect_source(database.path) == before
        assert not (tmp_path/'upgrade').exists()
    finally:
        database.close()


def test_final_receipt_failure_still_rolls_back_index_transaction(tmp_path, monkeypatch):
    database = old_current_database(tmp_path/'archive.db', monkeypatch)
    before = inspect_source(database.path)
    actual_save = index_preparation._save_receipt

    def fail_complete(directory, receipt):
        if receipt['state'] == 'complete':
            raise OSError('synthetic metadata write failure')
        actual_save(directory, receipt)

    monkeypatch.setattr(index_preparation, '_save_receipt', fail_complete)
    try:
        with LibraryLease(tmp_path/'images') as lease:
            with pytest.raises(index_preparation.TaskIndexPreparationError):
                index_preparation.prepare_task_indexes(database, lease)
        assert inspect_source(database.path) == before
        receipt = json.loads(next((tmp_path/'upgrade').glob('*/receipt.json')).read_text('utf-8'))
        assert receipt['state'] == 'failed' and receipt['error_code'] == 'OSError'
        assert inspect_source(Path(receipt['backup'])) == before
    finally:
        database.close()


def test_backup_failure_never_issues_ddl_and_releases_runtime_lease(tmp_path, monkeypatch):
    database = old_current_database(tmp_path/'archive.db', monkeypatch)
    database.close()
    before = inspect_source(database.path)
    monkeypatch.setattr(index_preparation, '_backup', lambda *args: (_ for _ in ()).throw(OSError('synthetic')))
    monkeypatch.setattr(index_preparation, 'create_task_indexes', lambda *args: pytest.fail('DDL before backup'))
    with pytest.raises(index_preparation.TaskIndexPreparationError):
        Runtime(database.path, tmp_path/'images')
    assert inspect_source(database.path) == before
    receipt = json.loads(next((tmp_path/'upgrade').glob('*/receipt.json')).read_text('utf-8'))
    assert receipt['state'] == 'failed' and receipt['error_code'] == 'OSError'
    with LibraryLease(tmp_path/'images'):
        pass


def test_missing_lease_and_in_use_library_do_not_prepare(tmp_path, monkeypatch):
    database = old_current_database(tmp_path/'archive.db', monkeypatch)
    before = inspect_source(database.path)
    try:
        with pytest.raises(index_preparation.TaskIndexPreparationError):
            index_preparation.prepare_task_indexes(database, LibraryLease(tmp_path/'images'))
        with LibraryLease(tmp_path/'images'):
            with pytest.raises(LibraryInUse):
                Runtime(database.path, tmp_path/'images')
        assert inspect_source(database.path) == before
        assert not (tmp_path/'upgrade').exists()
    finally:
        database.close()


def test_legacy_remains_explicit_upgrade_and_no_index_backup(tmp_path, monkeypatch):
    database = old_current_database(tmp_path/'archive.db', monkeypatch)
    with database.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE alembic_version SET version_num='0002_client'")
    database.close()
    before = inspect_source(database.path)
    with pytest.raises(UpgradeRequired):
        Runtime(database.path, tmp_path/'images')
    assert inspect_source(database.path) == before
    assert not (tmp_path/'upgrade').exists()
    with LibraryLease(tmp_path/'images'):
        pass


def test_new_creation_index_failure_rolls_back_tables_too(tmp_path, monkeypatch):
    def failing_creation(connection):
        connection.exec_driver_sql(next(iter(task_indexes.TASK_INDEX_SQL.values())))
        raise RuntimeError('synthetic DDL failure')

    monkeypatch.setattr(task_indexes, 'create_task_indexes', failing_creation)
    path = tmp_path/'archive.db'
    with pytest.raises(RuntimeError, match='synthetic DDL failure'):
        ArchiveDatabase.create(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')").fetchall() == []


def test_definition_comparison_preserves_literal_case():
    original = next(iter(task_indexes.TASK_INDEX_SQL.values()))
    equivalent = original.replace('CREATE INDEX ', 'create index if not exists ').replace(' ON ', ' on ') + ';'
    assert index_preparation._normalized_sql(original) == index_preparation._normalized_sql(equivalent)
    assert index_preparation._normalized_sql(original) != index_preparation._normalized_sql(original.replace('$.work_id', '$.WORK_ID'))
