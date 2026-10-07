"""Only in-flight GET sharing, using real localhost HTTP and synthetic libraries."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, Lock, Thread
from time import monotonic, sleep

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
import uvicorn

from src.core.models import Artist
from src.core.read_control import current_read
from src.core.runtime import Runtime
from src.core.tasks import ItemResult, ItemSpec
from src.routes.archive_api import create_router
from src.routes import settings_api


class SyntheticClient:
    def __init__(self, account):
        self.account = account
    def get_account_id(self):
        return self.account
    def identity_generation(self):
        return (0, self.account)


def wait_for(predicate, timeout=3):
    deadline = monotonic()+timeout
    while not predicate() and monotonic()<deadline:
        sleep(.005)
    assert predicate()


@contextmanager
def server_for(application):
    import socket
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(application, log_level='error', lifespan='off'))
    thread = Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started)
        yield f'http://127.0.0.1:{listener.getsockname()[1]}'
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        assert not thread.is_alive()


class Barrier:
    def __init__(self):
        self.release = Event()
        self.lock = Lock()
        self.calls = Counter()
        self.received = Counter()
        self.finished = Counter()
        self.budgets = []
        self.statistics = 0
    def hold(self, path):
        budget = current_read.get()
        with self.lock:
            self.calls[path] += 1
            self.budgets.append(budget)
        try:
            while not self.release.wait(.005):
                if budget is not None:
                    budget.checkpoint()
        finally:
            with self.lock:
                self.finished[path] += 1


@pytest.fixture
def harness(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path/'synthetic.db', tmp_path/'images', create=True, client=SyntheticClient('10001'))
    assert runtime.remote.get_account_id() == '10001'
    with runtime.database.write() as session:
        session.add(Artist(id=1, pixiv_user_id='30003', name='synthetic'))
    barrier = Barrier()
    original_run = runtime.database.reads.run
    async def admitted(request, function, **kwargs):
        with barrier.lock:
            barrier.received[request.url.path] += 1
        return await original_run(request, function, **kwargs)
    monkeypatch.setattr(runtime.database.reads, 'run', admitted)
    def local_addresses(port):
        barrier.hold('/api/settings')
        return []
    monkeypatch.setattr(settings_api, 'lan_addresses', local_addresses)
    monkeypatch.setattr(settings_api, 'get_pending_login_url', lambda: None)
    snapshot = runtime.queue.snapshot
    def progress_snapshot(**kwargs):
        result = snapshot(**kwargs)
        barrier.hold('/api/progress')
        return result
    monkeypatch.setattr(runtime.queue, 'snapshot', progress_snapshot)
    def count_statistics(connection, cursor, sql, parameters, context, many):
        if 'downloaded_pages' in sql:
            with barrier.lock:
                barrier.statistics += 1
    event.listen(runtime.database.engine, 'before_cursor_execute', count_statistics)
    application = FastAPI()
    application.include_router(create_router(lambda: runtime))
    try:
        with server_for(application) as origin, httpx.Client(base_url=origin, trust_env=False, timeout=5) as client:
            yield runtime, barrier, client, origin
    finally:
        barrier.release.set()
        event.remove(runtime.database.engine, 'before_cursor_execute', count_statistics)
        assert runtime.close()


@pytest.mark.parametrize('path', ['/api/settings', '/api/progress'])
def test_seven_actual_gets_share_one_snapshot_and_completed_value_is_not_cached(harness, path):
    runtime, barrier, client, _ = harness
    with ThreadPoolExecutor(max_workers=7) as pool:
        pending = [pool.submit(client.get, path) for _ in range(7)]
        try:
            wait_for(lambda: barrier.received[path] == 7)
            wait_for(lambda: barrier.calls[path] >= 1)
        finally:
            barrier.release.set()
        responses = [future.result(timeout=5) for future in pending]
    assert all(response.status_code == 200 for response in responses)
    assert barrier.calls[path] == 1
    if path == '/api/settings':
        assert barrier.statistics == 1
    assert client.get(path).status_code == 200
    assert barrier.calls[path] == 2
    assert not runtime.database.reads._shared


@pytest.mark.parametrize('path', ['/api/settings', '/api/progress'])
def test_different_query_parameters_never_share(harness, path):
    _, barrier, client, _ = harness
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(client.get, path+f'?scope={value}') for value in ('one', 'two')]
        try:
            wait_for(lambda: barrier.calls[path] == 2)
        finally:
            barrier.release.set()
        assert all(future.result(timeout=5).status_code == 200 for future in pending)


def open_get(origin, path):
    import socket
    from urllib.parse import urlsplit
    address = urlsplit(origin)
    connection = socket.create_connection((address.hostname, address.port), timeout=2)
    connection.sendall(f'GET {path} HTTP/1.1\r\nHost: {address.netloc}\r\n\r\n'.encode())
    return connection


@pytest.mark.parametrize('path', ['/api/settings', '/api/progress'])
def test_one_disconnected_get_does_not_cancel_remaining_consumer(harness, path):
    runtime, barrier, client, origin = harness
    first = open_get(origin, path)
    try:
        wait_for(lambda: barrier.calls[path] == 1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            second = pool.submit(client.get, path)
            try:
                wait_for(lambda: barrier.received[path] == 2)
                first.close()
                wait_for(lambda: sum(job[2] for job in runtime.database.reads._shared.values()) == 1)
                assert not barrier.budgets[0].cancelled.is_set()
                assert not barrier.finished[path]
            finally:
                barrier.release.set()
            assert second.result(timeout=5).status_code == 200
        assert barrier.calls[path] == 1
    finally:
        first.close()
        barrier.release.set()


@pytest.mark.parametrize('path', ['/api/settings', '/api/progress'])
def test_last_disconnected_get_cancels_shared_work_and_releases_resources(harness, path):
    runtime, barrier, _, origin = harness
    first, second = open_get(origin, path), open_get(origin, path)
    try:
        wait_for(lambda: barrier.received[path] == 2 and barrier.calls[path] == 1)
        first.close()
        wait_for(lambda: sum(job[2] for job in runtime.database.reads._shared.values()) == 1)
        assert not barrier.budgets[0].cancelled.is_set()
        second.close()
        wait_for(lambda: barrier.finished[path] == 1, timeout=1)
        wait_for(lambda: not any(runtime.database.reads.active.values()), timeout=1)
        assert barrier.budgets[0].cancelled.is_set()
        assert runtime.database.engine.pool.checkedout() == 0
        assert not runtime.database.reads._shared
    finally:
        first.close()
        second.close()
        barrier.release.set()


@pytest.mark.parametrize('path', ['/api/settings', '/api/progress'])
def test_get_after_committed_setting_does_not_join_older_snapshot(harness, path):
    runtime, barrier, client, _ = harness
    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(client.get, path)
        try:
            wait_for(lambda: barrier.calls[path] == 1)
            previous = runtime.database.reads.generation
            runtime.queue.set_automatic_paused(True)
            assert runtime.database.reads.generation > previous
            later = pool.submit(client.get, path)
            wait_for(lambda: barrier.calls[path] == 2)
        finally:
            barrier.release.set()
        assert older.result(timeout=5).json()['automatic_paused'] is False
        assert later.result(timeout=5).json()['automatic_paused'] is True


def test_byte_update_without_commit_does_not_join_older_progress_snapshot(harness):
    runtime, barrier, client, _ = harness
    first_report, advance, next_report, finish = (Event() for _ in range(4))

    class Download:
        def execute(self, item, context):
            context.report_bytes(10, 100)
            first_report.set()
            assert advance.wait(5)
            context.report_bytes(60, 100)
            next_report.set()
            assert finish.wait(5)
            return ItemResult()

    runtime.queue.handlers['repair'] = Download()
    with runtime.database.write() as session:
        ident = runtime.queue.enqueue_in_transaction(
            session, 'repair', {}, 'synthetic download',
            specs=[ItemSpec('one', 'download', {}, 'downloading', 'assets')],
        )['operation_id']

    def byte_count(response):
        assert response.status_code == 200
        operation = next(value for value in response.json()['operations'] if value['id'] == ident)
        assert operation['phase']['done'] == 0
        return operation['result']['current_item']['done']

    with ThreadPoolExecutor(max_workers=3) as pool:
        task = pool.submit(runtime.queue._step, 'download')
        try:
            assert first_report.wait(3)
            older = pool.submit(client.get, '/api/progress')
            wait_for(lambda: barrier.calls['/api/progress'] == 1)
            previous = runtime.database.reads.generation
            advance.set()
            assert next_report.wait(2)
            assert runtime.database.reads.generation == previous
            later = pool.submit(client.get, '/api/progress')
            wait_for(lambda: barrier.calls['/api/progress'] == 2)
            barrier.release.set()
            assert byte_count(older.result(timeout=5)) == 10
            assert byte_count(later.result(timeout=5)) == 60
        finally:
            barrier.release.set()
            advance.set()
            finish.set()
        assert task.result(timeout=5)
    assert runtime.queue.details(ident)['counts'] == {'succeeded': 1}


@pytest.mark.parametrize('failure', ['rollback', 'failed_commit'])
def test_failed_transaction_keeps_generation_and_does_not_publish_changes(harness, failure):
    runtime, barrier, client, _ = harness
    path = '/api/settings'
    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(client.get, path)
        try:
            wait_for(lambda: barrier.calls[path] == 1)
            previous = runtime.database.reads.generation
            if failure == 'failed_commit':
                with pytest.raises(IntegrityError):
                    with runtime.database.write() as session:
                        session.add(Artist(id=1, pixiv_user_id='99999', name='must roll back'))
            else:
                with pytest.raises(RuntimeError, match='synthetic rollback'):
                    with runtime.database.write() as session:
                        session.get(Artist, 1).name = 'must roll back'
                        raise RuntimeError('synthetic rollback')
            assert runtime.database.reads.generation == previous
            later = pool.submit(client.get, path)
            wait_for(lambda: barrier.received[path] == 2)
        finally:
            barrier.release.set()
        assert older.result(timeout=5).json() == later.result(timeout=5).json()
        assert barrier.calls[path] == barrier.statistics == 1
    with runtime.database.read() as session:
        assert session.get(Artist, 1).name == 'synthetic'


def test_account_switch_with_no_database_write_separates_settings_snapshots(harness):
    runtime, barrier, client, _ = harness
    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(client.get, '/api/settings')
        try:
            wait_for(lambda: barrier.calls['/api/settings'] == 1)
            previous = runtime.database.reads.generation
            runtime.client = SyntheticClient('20002')
            assert runtime.remote.get_account_id() == '20002'
            assert runtime.database.reads.generation == previous
            later = pool.submit(client.get, '/api/settings')
            wait_for(lambda: barrier.calls['/api/settings'] == 2)
        finally:
            barrier.release.set()
        assert older.result(timeout=5).json()['login']['account_id'] == '10001'
        assert later.result(timeout=5).json()['login']['account_id'] == '20002'


@pytest.mark.parametrize('setting', ['port', 'current_port', 'pending_login'])
def test_pure_memory_setting_change_separates_inflight_settings(harness, monkeypatch, setting):
    runtime, barrier, client, _ = harness
    pending = [None]
    if setting == 'pending_login':
        runtime.client = None
        monkeypatch.setattr(settings_api, 'get_pending_login_url', lambda: pending[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(client.get, '/api/settings')
        try:
            wait_for(lambda: barrier.calls['/api/settings'] == 1)
            previous = runtime.database.reads.generation
            old_port = settings_api.config.LISTENING_PORT if setting == 'current_port' else settings_api.config.PORT
            if setting == 'port':
                monkeypatch.setattr(settings_api.config, 'PORT', 18888)
            elif setting == 'current_port':
                monkeypatch.setattr(settings_api.config, 'LISTENING_PORT', 18888)
            else:
                pending[0] = 'https://synthetic.invalid/login'
            assert runtime.database.reads.generation == previous
            later = pool.submit(client.get, '/api/settings')
            wait_for(lambda: barrier.calls['/api/settings'] == 2)
        finally:
            barrier.release.set()
        before, after = older.result(timeout=5).json(), later.result(timeout=5).json()
        if setting in ('port', 'current_port'):
            field = 'current_port' if setting == 'current_port' else 'next_port'
            assert before[field] == old_port and after[field] == 18888
        else:
            assert before['login']['status'] == 'offline' and after['login']['status'] == 'pending'


def test_interval_fields_come_from_one_snapshot(harness, monkeypatch):
    _, barrier, client, _ = harness
    calls = []
    def interval(database):
        value = len(calls)+10
        calls.append(value)
        return {'minutes': value, 'next_check_at': f'synthetic-{value}'}
    monkeypatch.setattr(settings_api, 'interval_snapshot', interval)
    barrier.release.set()
    response = client.get('/api/settings')
    assert response.status_code == 200
    assert calls == [10]
    assert response.json()['check_interval_minutes'] == 10
    assert response.json()['next_check_at'] == 'synthetic-10'


def test_pending_login_url_rotation_shares_the_same_display_state(harness, monkeypatch):
    runtime, barrier, client, _ = harness
    runtime.client = None
    pending = ['https://synthetic.invalid/login-one']
    monkeypatch.setattr(settings_api, 'get_pending_login_url', lambda: pending[0])
    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(client.get, '/api/settings')
        try:
            wait_for(lambda: barrier.calls['/api/settings'] == 1)
            pending[0] = 'https://synthetic.invalid/login-two'
            later = pool.submit(client.get, '/api/settings')
            wait_for(lambda: barrier.received['/api/settings'] == 2)
            assert barrier.calls['/api/settings'] == 1
            assert 'synthetic.invalid' not in repr(runtime.database.reads._shared.keys())
        finally:
            barrier.release.set()
        assert older.result(timeout=5).json() == later.result(timeout=5).json()
        assert older.result().json()['login']['status'] == 'pending'
        assert barrier.calls['/api/settings'] == 1


@pytest.mark.parametrize('success', [True, False])
def test_port_patch_fences_only_successful_save_and_is_never_shared(harness, monkeypatch, success):
    runtime, barrier, client, origin = harness
    saved = Event()
    def save_port(value):
        if success:
            monkeypatch.setattr(settings_api.config, 'PORT', value)
        saved.set()
        if not success:
            raise OSError('synthetic settings save failure')
    monkeypatch.setattr(settings_api.config, 'set_port', save_port)
    def patch_settings():
        if success:
            return client.patch('/api/settings', json={'port': 18888})
        # Uvicorn closes a connection after the unhandled synthetic save error.
        # Keep that failing PATCH out of the two GETs' reusable connection pool.
        with httpx.Client(base_url=origin, trust_env=False, timeout=5) as patch_client:
            return patch_client.patch('/api/settings', json={'port': 18888})
    with ThreadPoolExecutor(max_workers=3) as pool:
        older = pool.submit(client.get, '/api/settings')
        try:
            wait_for(lambda: barrier.calls['/api/settings'] == 1)
            previous = runtime.database.reads.generation
            patch = pool.submit(patch_settings)
            assert saved.wait(2)
            if success:
                wait_for(lambda: runtime.database.reads.generation > previous)
                wait_for(lambda: barrier.calls['/api/settings'] == 2)
            else:
                assert patch.result(timeout=3).status_code == 500
                assert runtime.database.reads.generation == previous
            later = pool.submit(client.get, '/api/settings')
            wait_for(lambda: barrier.received['/api/settings'] == 2)
            if success:
                wait_for(lambda: barrier.calls['/api/settings'] == 3)
        finally:
            barrier.release.set()
        assert older.result(timeout=5).status_code == 200
        response = later.result(timeout=5)
        assert response.status_code == 200
        if success:
            assert response.json()['next_port'] == 18888
            assert patch.result(timeout=5).json()['next_port'] == 18888
            assert barrier.calls['/api/settings'] == 3
        else:
            assert barrier.calls['/api/settings'] == 1


def test_empty_successful_write_advances_generation_without_read_pool_borrow(harness):
    runtime, _, _, _ = harness
    read_connections = []
    def checkout(*args):
        read_connections.append(True)
    event.listen(runtime.database.engine, 'checkout', checkout)
    try:
        previous = runtime.database.reads.generation
        with runtime.database.write():
            pass
        assert runtime.database.reads.generation == previous+1
        assert not read_connections
        assert runtime.database.writer_engine.pool.checkedout() == 0
    finally:
        event.remove(runtime.database.engine, 'checkout', checkout)


def test_other_gets_are_not_globally_coalesced(harness, monkeypatch):
    runtime, barrier, client, _ = harness
    original = runtime.library.counts
    path = '/api/library/counts'
    def counts(*args, **kwargs):
        result = original(*args, **kwargs)
        barrier.hold(path)
        return result
    monkeypatch.setattr(runtime.library, 'counts', counts)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(client.get, path) for _ in range(2)]
        try:
            wait_for(lambda: barrier.calls[path] == 2)
        finally:
            barrier.release.set()
        assert all(future.result(timeout=5).status_code == 200 for future in pending)


def test_post_reading_session_requests_keep_distinct_results(harness):
    _, _, client, _ = harness
    with ThreadPoolExecutor(max_workers=7) as pool:
        pending = [pool.submit(client.post, '/api/reading-sessions') for _ in range(7)]
        responses = [future.result(timeout=5) for future in pending]
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()['session_id'] for response in responses}) == 7
