"""Synthetic discovery through real handlers/queue; no network or real library."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from src.core.archive import ArchiveHandler
from src.core.bookmarks import BookmarkService
from src.core.database import ArchiveDatabase
from src.core.media import Publisher
from src.core.models import (ArchiveIntent, Artist, Asset, BookmarkOperation, Operation,
    OperationItem, RemoteBookmark, Work, WorkRevision)
from src.core.read_control import LocalReadBusy
from src.core.source_status import SourceStatus
from src.core.tasks import ItemSpec, TaskContext, TaskQueue


class Remote:
    account = 'account-a'
    epoch = 1

    def __init__(self, database):
        self.database = database
        self.payload = {'illust_id': '101', 'title': 'synthetic', 'type': 'illust',
            'page_count': 1, 'caption': 'synthetic caption', 'tags': ['synthetic'],
            'urls': [{'original': 'https://synthetic.invalid/101.png'}],
            'is_bookmarked': True, 'bookmark_count': 2, 'view_count': 3}

    def get_account_id(self):
        assert not getattr(self.database._writer_state, 'active', False)
        return self.account

    def generation_context(self):
        return self.account, self.epoch

    def get_illust_detail(self, identity):
        assert not getattr(self.database._writer_state, 'active', False)
        assert identity == '101'
        return dict(self.payload)


@pytest.fixture(scope='module')
def template(tmp_path_factory):
    path = tmp_path_factory.mktemp('discovery-schema')/'template.db'
    database = ArchiveDatabase.create(path)
    handler = ArchiveHandler('check', database, Publisher(database, path.parent/'images'), object())
    queue = TaskQueue(database, {'check': handler})
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id='10', name='synthetic'))
    work_id = handler._save_remote_work(1, Remote(database).payload)
    operation_id = queue.submit('check', {'artist_ids': [1]}, 'synthetic discovery')['operation_id']
    assert queue.stop()
    database.close()
    return path, work_id, operation_id


@pytest.fixture
def case(tmp_path, template):
    template_path, work_id, operation_id = template
    path = tmp_path/'synthetic.db'
    shutil.copyfile(template_path, path)
    database = ArchiveDatabase(path)
    remote = Remote(database)
    bookmarks = BookmarkService(database, remote, current_account=lambda: remote.account)
    source = SourceStatus(database, remote)
    handler = ArchiveHandler('check', database, Publisher(database, tmp_path/'images'), remote,
        bookmarks=bookmarks, source_status=source)
    queue = TaskQueue(database, {'check': handler, 'repair': handler})
    yield SimpleNamespace(database=database, remote=remote, bookmarks=bookmarks,
        source=source, handler=handler, queue=queue, work_id=work_id, operation_id=operation_id)
    assert queue.stop()
    assert bookmarks.stop()
    database.close()


def prepare(case, *, new=False, bookmark_only=False, source_account='account-a'):
    data = dict(case.remote.payload)
    if new:
        data['illust_id'] = '102'
    spec = ItemSpec('discovery', 'discover_work', {'artist_id': 1, 'data': data,
        'remote_generation': case.remote.generation_context(), 'bookmark_only': bookmark_only}, 'checking', 'works')
    with case.database.write() as session:
        if not new:
            version = case.bookmarks._begin_observations(session, [case.work_id])[case.work_id]
            source_version = case.source._begin(session, 'work', case.work_id, source_account)
            case.queue.materialize_in_transaction(session, case.operation_id, [ItemSpec(
                f'observation-{case.work_id}', 'observation', {'bookmark_version': version,
                    'source_version': source_version, 'source_account': source_account,
                    'account': 'account-a', 'revision': 1}, 'inventory', 'works')])
        case.queue.materialize_in_transaction(session, case.operation_id, [spec])
        item_id = session.scalar(select(OperationItem.id).where(OperationItem.operation_id == case.operation_id,
            OperationItem.item_key == 'discovery'))
    return spec, TaskContext(case.queue, case.operation_id, item_id, kind='check')


@contextmanager
def measured(case, label):
    commits = []
    statements = []
    def committed(session):
        commits.append(True)
    def statement(connection, cursor, sql, parameters, context, many):
        if sql.lstrip().split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE'):
            statements.append(sql)
    event.listen(case.database.writer_session, 'after_commit', committed)
    event.listen(case.database.writer_engine, 'before_cursor_execute', statement)
    try:
        yield commits
    finally:
        event.remove(case.database.writer_session, 'after_commit', committed)
        event.remove(case.database.writer_engine, 'before_cursor_execute', statement)
        destination = os.environ.get('DISCOVERY_METRICS')
        if destination:
            with Path(destination).open('a', encoding='utf-8') as output:
                output.write(json.dumps({'case': label, 'successful_commits': len(commits),
                    'dml_statements_without_values': statements, 'timing_claim': False})+'\n')


@pytest.mark.parametrize('changed', [True, False])
def test_existing_discovery_combines_three_fact_commits_into_one(case, changed):
    item, context = prepare(case)
    if changed:
        item.target['data']['title'] = 'changed synthetic title'
    with measured(case, f'existing:{changed}') as commits:
        result = case.handler.execute(item, context)
    assert result.state == 'succeeded'
    with case.database.read() as session:
        work = session.get(Work, case.work_id)
        assert work.title == item.target['data']['title']
        assert work.source_metadata['remote_availability']['last_attempt']['state'] == 'available'
        fact = session.get(RemoteBookmark, case.work_id)
        assert fact.value is True and fact.account_id == 'account-a'
    assert case.bookmarks.state(case.work_id)['cached'] is False
    assert len(commits) == 1


def test_new_work_and_download_children_commit_with_bookmark_fact(case):
    item, context = prepare(case, new=True)
    with measured(case, 'new-work') as commits:
        result = case.handler.execute(item, context)
    assert len(commits) == 1 and result.state == 'succeeded'
    with case.database.read() as session:
        work = session.scalar(select(Work).where(Work.pixiv_illust_id == '102'))
        assert work and work.bookmark.value is True
        assert work.source_metadata['remote_availability']['account_id'] == 'account-a'
        parent = session.get(Operation, case.operation_id)
        child = session.get(Operation, parent.result['child_operation_ids'][0])
        assert child.result['parent_operation_ids'] == [parent.id]
        kinds = set(session.scalars(select(OperationItem.kind).where(OperationItem.operation_id == child.id)))
        assert {'download', 'thumbnail'} <= kinds
    assert case.bookmarks.state(work.id)['cached'] is False


def test_source_account_is_distinct_from_bookmark_account(case, monkeypatch):
    monkeypatch.setattr(case.source, 'account_context', lambda: 'source-account')
    item, context = prepare(case, source_account='source-account')
    with measured(case, 'distinct-source-account') as commits:
        case.handler.execute(item, context)
    assert len(commits) == 1
    with case.database.read() as session:
        work = session.get(Work, case.work_id)
        assert work.source_metadata['remote_availability']['account_id'] == 'source-account'
        assert work.bookmark.account_id == 'account-a' and work.bookmark.value is True


@pytest.mark.parametrize('failure', ['facts', 'commit'])
def test_fact_rollback_removes_metadata_children_source_bookmark_and_cache(case, monkeypatch, failure):
    item, context = prepare(case)
    item.target['data']['title'] = 'must roll back'
    context.checkpoint()
    before_parents = {identity: list(parents) for identity, parents in case.queue._control_parents.items()}
    assets = select(Asset.id, Asset.state, Asset.source_url, Asset.relative_path, Asset.revision_id)
    with case.database.read() as session:
        before_assets = session.execute(assets).all()
    if failure == 'facts':
        original = case.bookmarks._apply_observation
        def fail(*args, **kwargs):
            assert original(*args, **kwargs)
            raise LocalReadBusy('synthetic fact failure')
        monkeypatch.setattr(case.bookmarks, '_apply_observation', fail)
    else:
        def fail(session):
            raise LocalReadBusy('synthetic commit failure')
        event.listen(case.database.writer_session, 'before_commit', fail)
    try:
        with measured(case, f'rollback:{failure}') as commits:
            with pytest.raises(LocalReadBusy):
                case.handler.execute(item, context)
    finally:
        if failure == 'commit':
            event.remove(case.database.writer_session, 'before_commit', fail)
    assert len(commits) == 0 and not case.bookmarks._fresh
    with case.database.read() as session:
        work = session.get(Work, case.work_id)
        assert work.title == 'synthetic' and work.bookmark.value is None
        assert 'last_attempt' not in work.source_metadata['remote_availability']
        assert len(list(session.scalars(select(WorkRevision)))) == 1
        assert session.execute(assets).all() == before_assets
        assert not session.get(Operation, case.operation_id).result.get('child_operation_ids')
    assert case.database.writer_engine.pool.checkedout() == 0
    assert case.queue._control_parents == before_parents


@pytest.mark.parametrize('race', ['account', 'account_roundtrip', 'source_version',
    'bookmark_version', 'pending', 'delete', 'intent_revision'])
def test_late_discovery_rejects_stale_account_intent_and_fact_versions(case, race):
    item, context = prepare(case)
    item.target['data']['title'] = 'new observation'
    original = context.checkpoint
    def change():
        original()
        context.checkpoint = original
        if race in ('account', 'account_roundtrip'):
            case.remote.account = 'account-b'
            case.bookmarks._account_context()
            if race == 'account_roundtrip':
                case.remote.account = 'account-a'
                case.bookmarks._account_context()
        elif race == 'source_version':
            case.source.begin('work', case.work_id, 'account-a')
        else:
            with case.database.write() as session:
                if race in ('delete', 'intent_revision'):
                    intent = session.get(ArchiveIntent, '101')
                    intent.revision += 1
                    if race == 'delete':
                        intent.mode = 'suppressed'
                else:
                    fact = session.get(RemoteBookmark, case.work_id)
                    fact.version += 1
                    if race == 'pending':
                        session.add(BookmarkOperation(id='pending', request_id='pending-request',
                            work_id=case.work_id, desired=False, kind='write', account_id='account-a',
                            version=fact.version, status='pending'))
    context.checkpoint = change
    case.handler.execute(item, context)
    with case.database.read() as session:
        work = session.get(Work, case.work_id)
        assert work.title == ('new observation' if race in ('bookmark_version', 'pending') else 'synthetic')
        assert work.bookmark.value is None
        if race in ('bookmark_version', 'pending'):
            assert work.source_metadata['remote_availability']['last_attempt']['state'] == 'available'
        else:
            assert not session.get(Operation, case.operation_id).result.get('child_operation_ids')
    assert not case.bookmarks._fresh


@pytest.mark.parametrize('race', ['account', 'account_roundtrip', 'bookmark_version'])
def test_cache_publication_occurs_after_commit_and_rechecks_context(case, monkeypatch, race):
    item, context = prepare(case)
    original = case.bookmarks._mark_fresh
    published = []
    def late(work_id, version, account, **kwargs):
        assert not getattr(case.database._writer_state, 'active', False)
        assert case.database.writer_engine.pool.checkedout() == 0
        with case.database.read() as session:
            assert session.get(Work, work_id).bookmark.value is True
        if race in ('account', 'account_roundtrip'):
            case.remote.account = 'account-b'
            case.bookmarks._account_context()
            if race == 'account_roundtrip':
                case.remote.account = 'account-a'
                case.bookmarks._account_context()
        else:
            case.bookmarks.begin_observation(work_id)
        original(work_id, version, account, **kwargs)
        published.append(True)
    monkeypatch.setattr(case.bookmarks, '_mark_fresh', late)
    case.handler.execute(item, context)
    assert published == [True]
    assert case.bookmarks.state(case.work_id)['cached'] is True


def test_stale_page_reloads_current_account_response_before_combined_facts(case):
    item, context = prepare(case)
    item.target['data']['title'] = 'old persisted page'
    case.remote.payload['title'] = 'current account response'
    case.remote.account = 'account-b'
    case.remote.epoch += 1
    case.handler.execute(item, context)
    with case.database.read() as session:
        work = session.get(Work, case.work_id)
        assert work.title == 'current account response'
        assert work.bookmark.value is True and work.bookmark.account_id == 'account-b'
        assert work.source_metadata['remote_availability']['account_id'] == 'account-b'
        assert session.get(OperationItem, context.item_id).target['remote_generation'] == ['account-b', 2]


@pytest.mark.parametrize('metadata_failure', [False, True])
def test_ugoira_network_outside_writer_and_error_download_children_retained(case, metadata_failure):
    item, context = prepare(case, new=True)
    item.target['data']['type'] = 'ugoira'
    calls = []
    def metadata(identity):
        assert not getattr(case.database._writer_state, 'active', False)
        assert identity == '102'
        calls.append(True)
        if metadata_failure:
            raise RuntimeError('synthetic metadata failure')
        return {'frames': [{'file': 'synthetic.png', 'delay': 80}],
            'zip_urls': {'original': 'https://synthetic.invalid/102.zip'}}
    case.remote.get_ugoira_metadata = metadata
    with measured(case, f'ugoira:{metadata_failure}') as commits:
        case.handler.execute(item, context)
    assert len(commits) == 1 and calls == [True]
    with case.database.read() as session:
        parent = session.get(Operation, case.operation_id)
        child = session.get(Operation, parent.result['child_operation_ids'][0])
        kinds = set(session.scalars(select(OperationItem.kind).where(OperationItem.operation_id == child.id)))
        assert ('error' in kinds) is metadata_failure
        if not metadata_failure:
            assert {'download', 'timing', 'gif'} <= kinds


def test_bookmark_only_keeps_observed_marker_and_does_not_change_metadata(case):
    item, context = prepare(case, bookmark_only=True)
    item.target['data']['title'] = 'not a metadata update'
    result = case.handler.execute(item, context)
    assert result.state == 'succeeded' and result.detail['confirmed_works'] == 1
    with case.database.read() as session:
        assert session.get(Work, case.work_id).title == 'synthetic'
        observation = session.scalar(select(OperationItem).where(OperationItem.operation_id == case.operation_id,
            OperationItem.item_key == f'observation-{case.work_id}'))
        assert observation.result['observed'] is True
        assert session.get(RemoteBookmark, case.work_id).value is True
        assert not session.get(Operation, case.operation_id).result.get('child_operation_ids')


def test_account_change_while_waiting_for_writer_does_not_apply_discovery(case, monkeypatch):
    item, context = prepare(case)
    item.target['data']['title'] = 'must not apply after writer wait'
    checked = Event()
    calls = []
    original = case.source.account_context
    def before_writer():
        result = original()
        calls.append(True)
        if len(calls) == 2:
            checked.set()
        return result
    monkeypatch.setattr(case.source, 'account_context', before_writer)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with case.database._write_lock:
            pending = pool.submit(case.handler.execute, item, context)
            assert checked.wait(2)
            case.remote.account = 'account-b'
        pending.result(timeout=5)
    with case.database.read() as session:
        work = session.get(Work, case.work_id)
        assert work.title == 'synthetic' and work.bookmark.value is None
        assert 'last_attempt' not in work.source_metadata['remote_availability']
        assert not session.get(Operation, case.operation_id).result.get('child_operation_ids')
    assert not case.bookmarks._fresh
