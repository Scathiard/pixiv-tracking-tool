"""Settings tail must enumerate local adapters without hostname DNS or extra DB ownership."""
import socket
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psutil
import pytest

from src.core.database import ArchiveDatabase
from src.routes.settings_api import create_settings_router


@pytest.mark.parametrize('adapter_failure', [False, True])
def test_settings_snapshot_tail_never_resolves_hostname(tmp_path, monkeypatch, adapter_failure):
    db = ArchiveDatabase.create(tmp_path / 'lan.db')
    def forbidden_dns(*args, **kwargs):
        raise AssertionError('Hostname resolution can stall unrelated settings fields')
    def adapters():
        assert db.engine.pool.checkedout() == 0
        if adapter_failure:
            raise OSError('synthetic adapter unavailable')
        return {'active': [SimpleNamespace(family=socket.AF_INET, address='192.168.1.2'),
                           SimpleNamespace(family=socket.AF_INET, address='192.168.1.2'),
                           SimpleNamespace(family=socket.AF_INET6, address='fe80::1%5')],
                'loopback': [SimpleNamespace(family=socket.AF_INET, address='127.0.0.1')],
                'down': [SimpleNamespace(family=socket.AF_INET, address='10.1.1.3')]}
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden_dns)
    monkeypatch.setattr(psutil, 'net_if_addrs', adapters)
    monkeypatch.setattr(psutil, 'net_if_stats', lambda: {'active': SimpleNamespace(isup=True),
        'loopback': SimpleNamespace(isup=True), 'down': SimpleNamespace(isup=False)})
    runtime = SimpleNamespace(database=db, client=None, queue=SimpleNamespace(automatic_paused=lambda: False))
    app = FastAPI(); app.include_router(create_settings_router(lambda: runtime))
    try:
        with TestClient(app) as client:
            started = time.perf_counter()
            response = client.get('/api/settings')
            elapsed = time.perf_counter() - started
        assert response.status_code == 200 and elapsed < .5
        data = response.json()
        assert data['lan_urls'] == ([] if adapter_failure else [f"http://192.168.1.2:{data['current_port']}"])
        assert 'stats' not in data and data['login']['status'] == 'offline'
        assert db.engine.pool.checkedout() == 0
    finally:
        db.close()
