"""Frozen diagnostic seam from 05e3c30; synthetic localhost only."""
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


@pytest.mark.parametrize('success', [True, False])
def test_port_patch_fences_only_successful_save_and_is_never_shared(harness, monkeypatch, success):
    runtime, barrier, client, _ = harness
    saved = Event()
    def save_port(value):
        if success:
            monkeypatch.setattr(settings_api.config, 'PORT', value)
        saved.set()
        if not success:
            raise OSError('synthetic settings save failure')
    monkeypatch.setattr(settings_api.config, 'set_port', save_port)
    with ThreadPoolExecutor(max_workers=3) as pool:
        older = pool.submit(client.get, '/api/settings')
        try:
            wait_for(lambda: barrier.calls['/api/settings'] == 1)
            previous = runtime.database.reads.generation
            patch = pool.submit(client.patch, '/api/settings', json={'port': 18888})
            assert saved.wait(2)
            if success:
                wait_for(lambda: runtime.database.reads.generation > previous)
                wait_for(lambda: barrier.calls['/api/settings'] == 2)
            else:
                assert patch.result(timeout=3).status_code == 500
                assert runtime.database.reads.generation == previous
            later = pool.submit(client.get, '/api/settings')
            try:
                wait_for(lambda: barrier.received['/api/settings'] == 2)
            except AssertionError:
                print('[DEBUG-sharing-failure] later_done=', later.done(),
                    'received=', dict(barrier.received), 'calls=', dict(barrier.calls))
                if later.done():
                    later.result(timeout=0)
                raise
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
