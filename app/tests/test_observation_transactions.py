"""Real handlers and SQLite commits; all remote responses are synthetic."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace
from threading import Event

import pytest
from sqlalchemy import event, select

from src.core.archive import ArchiveHandler
from src.core.bookmarks import BookmarkService
from src.core.database import ArchiveDatabase
from src.core.media import Publisher
from src.core.models import ArchiveIntent, Artist, BookmarkOperation, RemoteBookmark, Work, WorkRevision
from src.core.read_control import LocalReadBusy
from src.core.reconcile import ReconcileHandler
from src.core.source_status import SourceStatus


class Remote:
    account = 'account-a'
    hook = None

    def __init__(self, database):
        self.database = database
        self.payload = {'illust_id': '101', 'title': 'synthetic', 'type': 'illust',
            'page_count': 1, 'caption': 'wide caption '*1024, 'tags': ['synthetic'],
            'urls': [{'original': 'https://synthetic.invalid/101.png'}],
            'is_bookmarked': True, 'bookmark_count': 2, 'view_count': 3}

    def get_account_id(self):
        assert not getattr(self.database._writer_state, 'active', False)
        return self.account

    def get_illust_detail(self, identity):
        assert not getattr(self.database._writer_state, 'active', False)
        assert identity == '101'
        if self.hook:
            self.hook()
        return dict(self.payload)


@pytest.fixture
def case(tmp_path):
    database = ArchiveDatabase.create(tmp_path/'synthetic.db')
    remote = Remote(database)
    bookmarks = BookmarkService(database, remote, current_account=lambda: remote.account)
    source = SourceStatus(database, remote)
    publisher = Publisher(database, tmp_path/'images')
    metadata = ArchiveHandler('metadata', database, publisher, remote,
        bookmarks=bookmarks, source_status=source)
    reconcile = ReconcileHandler(database, publisher, remote,
        bookmarks=bookmarks, source_status=source)
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id='10', name='synthetic'))
    work_id = metadata._save_remote_work(1, remote.payload)
    assert work_id is not None
    yield SimpleNamespace(database=database, remote=remote, bookmarks=bookmarks,
        source=source, metadata=metadata, reconcile=reconcile, work_id=work_id,
        path=tmp_path/'synthetic.db')
    assert bookmarks.stop()
    database.close()


def execute(case, path):
    item = SimpleNamespace(kind='metadata_work' if path == 'refresh' else 'reconcile_snapshot',
        target={'work_id': case.work_id, 'revision': 1})
    context = SimpleNamespace(checkpoint=lambda: None)
    return getattr(case, 'metadata' if path == 'refresh' else 'reconcile').execute(item, context)


@contextmanager
def measured(case, label):
    database = case.database
    dml = Counter()
    statements = []
    commits = []
    wal = Path(str(case.path)+'-wal')
    # Pin the original WAL so this tiny logical-volume measurement cannot be
    # confused by a WAL reset. No timing or physical-disk claim is made.
    with database.read() as pinned:
        pinned.get(Work, case.work_id)
        before = wal.stat().st_size
        page_size = int.from_bytes(wal.read_bytes()[8:12], 'big')
        def statement(connection, cursor, sql, parameters, context, many):
            action = sql.lstrip().split()[0].upper()
            if action in ('INSERT', 'UPDATE', 'DELETE'):
                dml[action] += 1
                statements.append(sql)
        def committed(session):
            commits.append(True)
        event.listen(database.writer_engine, 'before_cursor_execute', statement)
        event.listen(database.writer_session, 'after_commit', committed)
        try:
            yield commits
        finally:
            event.remove(database.writer_engine, 'before_cursor_execute', statement)
            event.remove(database.writer_session, 'after_commit', committed)
            delta = wal.stat().st_size-before
            assert delta >= 0 and delta % (page_size+24) == 0
            result = {'case': label, 'successful_commits': len(commits), 'dml': dict(dml),
                'dml_statements_without_values': statements,
                'wal_added_frames': delta//(page_size+24), 'wal_added_bytes': delta,
                'page_size': page_size, 'timing_claim': False}
            destination = os.environ.get('OBSERVATION_METRICS')
            if destination:
                with Path(destination).open('a', encoding='utf-8') as output:
                    output.write(json.dumps(result)+'\n')


@pytest.mark.parametrize('path', ['refresh', 'reconcile'])
@pytest.mark.parametrize('changed', [True, False])
def test_one_observation_uses_two_short_commits(case, path, changed):
    if changed:
        case.remote.payload['title'] = 'new synthetic title'
    with measured(case, f'{path}:{"changed" if changed else "unchanged"}') as commits:
        result = execute(case, path)
    assert result.state == 'succeeded'
    assert len(commits) == 2
    with case.database.read() as session:
        work = session.get(Work, case.work_id)
        fact = session.get(RemoteBookmark, case.work_id)
        assert work.title == case.remote.payload['title']
        availability = work.source_metadata['remote_availability']
        assert availability['state'] == 'available'
        assert availability['version'] == availability['last_attempt']['version'] == 1
        assert fact.value is True and fact.account_id == 'account-a'
    assert case.bookmarks.state(case.work_id)['cached'] is False


@pytest.mark.parametrize('path', ['refresh', 'reconcile'])
def test_reservation_rollback_keeps_both_versions_and_never_calls_remote(case, monkeypatch, path):
    def fail(*args, **kwargs):
        raise LocalReadBusy('synthetic reservation rollback')
    monkeypatch.setattr(case.source, '_begin', fail)
    case.remote.hook = lambda: pytest.fail('remote must follow committed reservations')
    with pytest.raises(LocalReadBusy):
        execute(case, path)
    with case.database.read() as session:
        assert session.get(RemoteBookmark, case.work_id).version == 0
        assert 'version' not in session.get(Work, case.work_id).source_metadata['remote_availability']
    assert not case.bookmarks._fresh


@pytest.mark.parametrize('path', ['refresh', 'reconcile'])
@pytest.mark.parametrize('failure', ['facts', 'commit'])
def test_fact_transaction_rollback_publishes_no_metadata_source_bookmark_or_freshness(case, monkeypatch, path, failure):
    case.remote.payload['title'] = 'must roll back'
    if failure == 'facts':
        original = case.bookmarks._apply_observation
        def fail(*args, **kwargs):
            assert original(*args, **kwargs)
            raise LocalReadBusy('synthetic fact rollback')
        monkeypatch.setattr(case.bookmarks, '_apply_observation', fail)
    else:
        attempts = []
        def fail_commit(session):
            attempts.append(True)
            if len(attempts) == 2:
                raise LocalReadBusy('synthetic commit failure')
        event.listen(case.database.writer_session, 'before_commit', fail_commit)
    try:
        with pytest.raises(LocalReadBusy):
            execute(case, path)
    finally:
        if failure == 'commit':
            event.remove(case.database.writer_session, 'before_commit', fail_commit)
    with case.database.read() as session:
        work = session.get(Work, case.work_id)
        availability = work.source_metadata['remote_availability']
        assert work.title == 'synthetic'
        assert availability['version'] == 1 and 'last_attempt' not in availability
        assert session.get(RemoteBookmark, case.work_id).value is None
        assert len(list(session.scalars(select(WorkRevision)))) == 1
    assert not case.bookmarks._fresh
    assert case.database.writer_engine.pool.checkedout() == 0


@pytest.mark.parametrize('path', ['refresh', 'reconcile'])
@pytest.mark.parametrize('race', ['account', 'source_version', 'bookmark_version', 'delete', 'intent_revision', 'pending'])
def test_late_response_preserves_account_versions_deletion_and_pending_write(case, path, race):
    case.remote.payload['title'] = 'new response'
    def intervening_change():
        if race == 'account':
            case.remote.account = 'account-b'
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
                        session.add(BookmarkOperation(id='synthetic-pending', request_id='synthetic-request',
                            work_id=case.work_id, desired=False, kind='write', account_id='account-a',
                            version=fact.version, status='pending'))
    case.remote.hook = intervening_change
    execute(case, path)
    with case.database.read() as session:
        work = session.get(Work, case.work_id)
        assert work.title == ('new response' if race in ('bookmark_version', 'pending') else 'synthetic')
        assert session.get(RemoteBookmark, case.work_id).value is None
        if race in ('bookmark_version', 'pending'):
            assert work.source_metadata['remote_availability']['last_attempt']['state'] == 'available'
    assert not case.bookmarks._fresh


@pytest.mark.parametrize('path', ['refresh', 'reconcile'])
@pytest.mark.parametrize('race', ['account', 'account_roundtrip', 'bookmark_version'])
def test_freshness_publication_follows_commit_and_rejects_late_context(case, monkeypatch, path, race):
    original = case.bookmarks._mark_fresh
    published = []
    def late(work_id, version, account, **kwargs):
        assert not getattr(case.database._writer_state, 'active', False)
        assert case.database.writer_engine.pool.checkedout() == 0
        with case.database.read() as session:
            assert session.get(RemoteBookmark, work_id).value is True
            assert session.get(Work, work_id).source_metadata['remote_availability']['last_attempt']['state'] == 'available'
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
    execute(case, path)
    assert published == [True]
    assert case.bookmarks.state(case.work_id)['cached'] is True
    if race != 'bookmark_version':
        assert not case.bookmarks._fresh


@pytest.mark.parametrize('path', ['refresh', 'reconcile'])
def test_unchanged_second_response_updates_observation_without_new_work_revision(case, path):
    execute(case, path)
    execute(case, path)
    with case.database.read() as session:
        work = session.get(Work, case.work_id)
        availability = work.source_metadata['remote_availability']
        assert availability['version'] == availability['last_attempt']['version'] == 2
        assert session.get(RemoteBookmark, case.work_id).version == 2
        assert len(list(session.scalars(select(WorkRevision)))) == 1


@pytest.mark.parametrize('changed', [True, False])
def test_standalone_wrappers_control_preserves_same_facts_in_five_commits(case, changed):
    if changed:
        case.remote.payload['title'] = 'new synthetic title'
    with measured(case, f'separate-correct-control:{"changed" if changed else "unchanged"}') as commits:
        version = case.bookmarks.begin_observation(case.work_id)
        source_version = case.source.begin('work', case.work_id, 'account-a')
        assert case.metadata._save_remote_work(1, case.remote.payload,
            expected_work_id=case.work_id, expected_revision=1,
            expected_source_version=source_version) == case.work_id
        assert case.source.apply_available('work', case.work_id, case.remote.payload, source_version, 'account-a')
        assert case.bookmarks.apply_observation(case.work_id, True, 'account-a', version)
    assert len(commits) == 5
    assert case.bookmarks.state(case.work_id)['value'] is True
    with case.database.read() as session:
        availability = session.get(Work, case.work_id).source_metadata['remote_availability']
        assert availability['version'] == availability['last_attempt']['version'] == 1


@pytest.mark.parametrize('path', ['refresh', 'reconcile'])
def test_account_changes_while_fact_transaction_waits_for_writer(case, monkeypatch, path):
    reserved, proceed, identity_checked = Event(), Event(), Event()
    def remote_hook():
        reserved.set()
        assert proceed.wait(2)
    case.remote.hook = remote_hook
    original = case.source.account_context
    def before_writer():
        result = original()
        identity_checked.set()
        return result
    monkeypatch.setattr(case.source, 'account_context', before_writer)
    case.remote.payload['title'] = 'must not apply after writer wait'
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(execute, case, path)
        try:
            assert reserved.wait(2)
            with case.database._write_lock:
                proceed.set()
                assert identity_checked.wait(2)
                case.remote.account = 'account-b'
        finally:
            proceed.set()
        pending.result(timeout=5)
    with case.database.read() as session:
        assert session.get(Work, case.work_id).title == 'synthetic'
        assert session.get(RemoteBookmark, case.work_id).value is None
    assert not case.bookmarks._fresh


def test_reconcile_ugoira_metadata_is_obtained_outside_both_short_transactions(case):
    calls = []
    def metadata(identity):
        assert not getattr(case.database._writer_state, 'active', False)
        assert identity == '101'
        calls.append(True)
        return {'frames': [{'file': 'synthetic.png', 'delay': 80}],
            'zip_urls': {'original': 'https://synthetic.invalid/101.zip'}}
    case.remote.get_ugoira_metadata = metadata
    case.remote.payload['type'] = 'ugoira'
    with measured(case, 'reconcile:ugoira') as commits:
        result = execute(case, 'reconcile')
    assert len(commits) == 2 and calls == [True]
    assert result.detail['remote_ready'] is True
