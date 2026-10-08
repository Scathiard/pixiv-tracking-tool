"""Additive settings indexes use synthetic libraries and the existing backup path."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import MetaData, event

from src.core import index_preparation, task_indexes
from src.core.database import ArchiveDatabase, SCHEMA_REVISION, UpgradeRequired
from src.core.models import Artist, Base, Work, WorkRevision
from src.core.ownership import LibraryLease
from src.core.runtime import Runtime
from src.core.upgrade import upgrade_0003
from strict_legacy_fixture import strict_client_database


def old_settings_database(path, monkeypatch, *, missing_tasks=False):
    # A separate metadata copy generates the prior schema directly. No index
    # or file is dropped, and production metadata remains unchanged.
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    for table in metadata.tables.values():
        for index in tuple(table.indexes):
            if index.name in index_preparation.SETTINGS_INDEX_SQL:
                table.indexes.remove(index)
    with monkeypatch.context() as patch:
        patch.setattr(Base.metadata, 'create_all', metadata.create_all)
        if missing_tasks:
            patch.setattr(task_indexes, 'create_task_indexes', lambda connection: ())
        database = ArchiveDatabase.create(path)
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id='1', name='synthetic'))
        session.flush()
        session.add(Work(id=1, artist_id=1, pixiv_illust_id='101', title='synthetic',
            type='manga', page_count=2, default_revision_id='r1', caption='x'*2048))
        session.flush()
        session.add(WorkRevision(id='r1', work_id=1, ordinal=1, title='synthetic',
            type='manga', page_count=2, source_signature='synthetic'))
    return database


def snapshot(path):
    with closing(sqlite3.connect(path)) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {
            'indexes': dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='index'")),
            'rows': {name: sorted(connection.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)
                     for name in tables},
        }


def assert_settings_indexes(database):
    expected = {
        'ix_works_settings_shape': ('works', ['default_revision_id', 'pixiv_illust_id', 'type', 'page_count']),
        'ix_revisions_settings_shape': ('work_revisions', ['id', 'type', 'page_count']),
    }
    with database.engine.connect() as connection:
        assert index_preparation.missing_settings_indexes(connection) == []
        assert connection.exec_driver_sql('SELECT version_num FROM alembic_version').scalar() == SCHEMA_REVISION
        for name, (table, columns) in expected.items():
            assert [row[2] for row in connection.exec_driver_sql(f'PRAGMA index_info("{name}")')] == columns
            attributes = next(row for row in connection.exec_driver_sql(f'PRAGMA index_list("{table}")') if row[1] == name)
            assert attributes[2] == 0 and attributes[4] == 0  # Non-unique, not partial.
    with database.writer_engine.connect() as connection:
        assert connection.exec_driver_sql('PRAGMA synchronous').scalar() == 2


def test_new_database_declares_both_settings_indexes_without_backup(tmp_path):
    runtime = Runtime(tmp_path/'archive.db', tmp_path/'images', create=True)
    try:
        assert_settings_indexes(runtime.database)
        assert runtime.index_preparation is None
        assert not (tmp_path/'upgrade').exists()
    finally:
        assert runtime.close()


def test_readonly_open_never_adds_settings_indexes_or_backup(tmp_path, monkeypatch):
    database = old_settings_database(tmp_path/'archive.db', monkeypatch)
    database.close()
    before = snapshot(database.path)
    reopened = ArchiveDatabase(database.path)
    try:
        with reopened.read() as session:
            assert session.get(Work, 1).page_count == 2
    finally:
        reopened.close()
    assert snapshot(database.path) == before
    assert not (tmp_path/'upgrade').exists()


def test_runtime_prepares_settings_before_workers_with_wal_backup_and_idempotence(tmp_path, monkeypatch):
    database = old_settings_database(tmp_path/'archive.db', monkeypatch)
    database.close()
    import src.core.runtime as runtime_module
    actual_queue = runtime_module.TaskQueue
    with closing(sqlite3.connect(database.path)) as wal_owner:
        wal_owner.execute('PRAGMA wal_autocheckpoint=0')
        wal_owner.execute("UPDATE works SET caption='committed WAL synthetic' WHERE id=1")
        wal_owner.commit()
        assert database.path.with_name(database.path.name+'-wal').stat().st_size > 0
        before = snapshot(database.path)

        def checked_queue(database, *args, **kwargs):
            assert_settings_indexes(database)
            return actual_queue(database, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(runtime_module, 'TaskQueue', checked_queue)
            runtime = Runtime(database.path, tmp_path/'images')
        try:
            receipt = runtime.index_preparation
            assert receipt['state'] == 'complete'
            assert receipt['missing_indexes'] == list(index_preparation.SETTINGS_INDEX_SQL)
            assert snapshot(Path(receipt['backup'])) == before
            after = snapshot(database.path)
            assert after['rows'] == before['rows']
            assert {name: after['indexes'][name] for name in before['indexes']} == before['indexes']
            assert set(after['indexes']) - set(before['indexes']) == set(index_preparation.SETTINGS_INDEX_SQL)
        finally:
            assert runtime.close()
    directories = list((tmp_path/'upgrade').iterdir())
    with monkeypatch.context() as patch:
        patch.setattr(index_preparation, '_backup', lambda *args: pytest.fail('repeated backup'))
        patch.setattr(index_preparation, 'create_settings_indexes', lambda *args: pytest.fail('repeated DDL'))
        restarted = Runtime(database.path, tmp_path/'images')
        assert restarted.index_preparation is None
        assert restarted.close()
    assert list((tmp_path/'upgrade').iterdir()) == directories


@pytest.mark.parametrize('name', index_preparation.SETTINGS_INDEX_SQL)
def test_conflicting_settings_definition_fails_without_backup_or_replacement(tmp_path, monkeypatch, name):
    database = old_settings_database(tmp_path/'archive.db', monkeypatch)
    with database.engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE UNIQUE INDEX "{name}" ON works (type, page_count) WHERE id>0')
    before = snapshot(database.path)
    try:
        with LibraryLease(tmp_path/'images') as lease:
            with pytest.raises(index_preparation.TaskIndexDefinitionMismatch):
                index_preparation.prepare_task_indexes(database, lease)
        assert snapshot(database.path) == before
        assert not (tmp_path/'upgrade').exists()
    finally:
        database.close()


def test_settings_creation_failure_rolls_back_task_and_settings_ddl(tmp_path, monkeypatch):
    database = old_settings_database(tmp_path/'archive.db', monkeypatch, missing_tasks=True)
    before = snapshot(database.path)

    def fail_after_first_settings_index(connection):
        assert set(task_indexes.TASK_INDEX_SQL) <= set(connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index'").scalars())
        connection.exec_driver_sql(next(iter(index_preparation.SETTINGS_INDEX_SQL.values())))
        raise RuntimeError('synthetic confidential failure')

    monkeypatch.setattr(index_preparation, 'create_settings_indexes', fail_after_first_settings_index)
    try:
        with LibraryLease(tmp_path/'images') as lease:
            with pytest.raises(index_preparation.TaskIndexPreparationError):
                index_preparation.prepare_task_indexes(database, lease)
        assert snapshot(database.path) == before
        receipt_path = next((tmp_path/'upgrade').glob('*/receipt.json'))
        receipt = json.loads(receipt_path.read_text('utf-8'))
        assert receipt['state'] == 'failed' and receipt['error_code'] == 'RuntimeError'
        assert 'confidential' not in receipt_path.read_text('utf-8')
        assert snapshot(Path(receipt['backup'])) == before
    finally:
        database.close()


def test_final_receipt_failure_rolls_back_both_settings_indexes(tmp_path, monkeypatch):
    database = old_settings_database(tmp_path/'archive.db', monkeypatch)
    before = snapshot(database.path)
    actual_save = index_preparation._save_receipt
    def fail_complete(directory, receipt):
        if receipt['state'] == 'complete':
            raise OSError('synthetic receipt failure')
        actual_save(directory, receipt)
    monkeypatch.setattr(index_preparation, '_save_receipt', fail_complete)
    try:
        with LibraryLease(tmp_path/'images') as lease:
            with pytest.raises(index_preparation.TaskIndexPreparationError):
                index_preparation.prepare_task_indexes(database, lease)
        assert snapshot(database.path) == before
        receipt = json.loads(next((tmp_path/'upgrade').glob('*/receipt.json')).read_text('utf-8'))
        assert receipt['state'] == 'failed' and receipt['error_code'] == 'OSError'
        assert snapshot(Path(receipt['backup'])) == before
    finally:
        database.close()


def test_already_present_settings_index_is_preserved_and_only_other_is_created(tmp_path, monkeypatch):
    database = old_settings_database(tmp_path/'archive.db', monkeypatch)
    name, sql = next(iter(index_preparation.SETTINGS_INDEX_SQL.items()))
    with database.engine.begin() as connection:
        connection.exec_driver_sql(sql.replace('CREATE INDEX ', 'create index if not exists '))
    before = snapshot(database.path)
    try:
        with LibraryLease(tmp_path/'images') as lease:
            receipt = index_preparation.prepare_task_indexes(database, lease)
        assert receipt['missing_indexes'] == ['ix_revisions_settings_shape']
        assert_settings_indexes(database)
        assert snapshot(database.path)['indexes'][name] == before['indexes'][name]
        assert snapshot(database.path)['rows'] == before['rows']
        assert snapshot(Path(receipt['backup'])) == before
    finally:
        database.close()


def test_settings_backup_failure_does_not_create_and_releases_lease(tmp_path, monkeypatch):
    database = old_settings_database(tmp_path/'archive.db', monkeypatch)
    database.close()
    before = snapshot(database.path)
    monkeypatch.setattr(index_preparation, '_backup', lambda *args: (_ for _ in ()).throw(OSError('synthetic')))
    monkeypatch.setattr(index_preparation, 'create_settings_indexes', lambda *args: pytest.fail('DDL before backup'))
    with pytest.raises(index_preparation.TaskIndexPreparationError):
        Runtime(database.path, tmp_path/'images')
    assert snapshot(database.path) == before
    with LibraryLease(tmp_path/'images'):
        pass


def test_settings_commit_failure_rolls_back_and_runtime_releases_lease(tmp_path, monkeypatch):
    database = old_settings_database(tmp_path/'archive.db', monkeypatch)
    database.close()
    before = snapshot(database.path)
    actual_engine = ArchiveDatabase._engine
    failed_commits = []

    def failing_engine(path, **kwargs):
        engine = actual_engine(path, **kwargs)
        def fail_index_commit(connection):
            # The SQLAlchemy commit event runs after successful DDL, before
            # the DBAPI SQLite COMMIT. This is exception-path coverage, not
            # an operating-system I/O failure or power-loss simulation.
            existing = set(connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index'").scalars())
            if set(index_preparation.SETTINGS_INDEX_SQL) <= existing:
                failed_commits.append(existing)
                raise RuntimeError('synthetic confidential commit failure')
        event.listen(engine, 'commit', fail_index_commit)
        return engine

    monkeypatch.setattr(ArchiveDatabase, '_engine', staticmethod(failing_engine))
    with pytest.raises(index_preparation.TaskIndexPreparationError) as failure:
        Runtime(database.path, tmp_path/'images')
    assert len(failed_commits) == 1
    assert 'confidential' not in str(failure.value)
    assert snapshot(database.path) == before
    receipt_path = next((tmp_path/'upgrade').glob('*/receipt.json'))
    receipt = json.loads(receipt_path.read_text('utf-8'))
    assert receipt['state'] == 'failed' and receipt['error_code'] == 'RuntimeError'
    assert 'confidential' not in receipt_path.read_text('utf-8')
    assert snapshot(Path(receipt['backup'])) == before
    with LibraryLease(tmp_path/'images'):
        pass


def test_new_database_settings_ddl_failure_rolls_back_tables(tmp_path, monkeypatch):
    actual_engine = ArchiveDatabase._engine
    def failing_engine(path, **kwargs):
        engine = actual_engine(path, **kwargs)
        seen = []
        def reject_second_setting(conn, cursor, statement, params, context, many):
            if statement.startswith('CREATE INDEX ') and any(name in statement for name in index_preparation.SETTINGS_INDEX_SQL):
                seen.append(statement)
                if len(seen) == 2:
                    raise RuntimeError('synthetic settings DDL failure')
        event.listen(engine, 'before_cursor_execute', reject_second_setting)
        return engine
    monkeypatch.setattr(ArchiveDatabase, '_engine', staticmethod(failing_engine))
    path = tmp_path/'archive.db'
    with pytest.raises(RuntimeError, match='synthetic settings DDL failure'):
        ArchiveDatabase.create(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')").fetchall() == []


def test_strict_legacy_requires_upgrade_and_candidate_contains_settings_indexes(tmp_path):
    source = strict_client_database(tmp_path/'source.db')
    before = snapshot(source)
    with pytest.raises(UpgradeRequired):
        Runtime(source, tmp_path/'images')
    assert snapshot(source) == before
    assert not (tmp_path/'upgrade').exists()
    result = upgrade_0003(source, tmp_path/'candidate.db', tmp_path/'backup.db')
    assert snapshot(source) == before
    assert snapshot(Path(result['backup'])) == before
    candidate = ArchiveDatabase(result['candidate'])
    try:
        assert_settings_indexes(candidate)
    finally:
        candidate.close()
