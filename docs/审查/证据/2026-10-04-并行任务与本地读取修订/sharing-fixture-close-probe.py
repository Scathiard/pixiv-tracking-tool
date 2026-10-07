"""A deterministic localhost-only probe of the ASGI exception/close window.

Loaded only by the isolated pytest command; never imported by product code.
"""
import asyncio
from threading import Event

import httpx
import pytest
from uvicorn.protocols.http.httptools_impl import RequestResponseCycle


@pytest.fixture(autouse=True)
def hold_close_until_later_get_is_sent(monkeypatch):
    later_selected = Event()
    error_started = Event()
    server_closed = Event()
    original_run = RequestResponseCycle.run_asgi
    original_send = httpx.Client.send
    async def run(self, app):
        failed_patch = False
        async def observed_app(scope, receive, send):
            nonlocal failed_patch
            try:
                return await app(scope, receive, send)
            except OSError:
                if scope['method'] == 'PATCH':
                    failed_patch = True
                    error_started.set()
                    print('[DEBUG-sharing-failure] 500 sent; yield until later GET selects its connection')
                    assert await asyncio.to_thread(later_selected.wait, 2)
                raise
        try:
            return await original_run(self, observed_app)
        finally:
            if failed_patch:
                server_closed.set()
                print('[DEBUG-sharing-failure] original Uvicorn exception path closed PATCH transport')
    def send(self, request, *args, **kwargs):
        if request.method == 'GET' and error_started.is_set():
            print('[DEBUG-sharing-failure] pool before later GET', repr(self._transport._pool))
            def trace(name, info):
                if name.startswith('connection.') or name.endswith('.failed'):
                    print('[DEBUG-sharing-failure] later transport trace', name, repr(info))
                if name == 'http11.send_request_headers.started':
                    print('[DEBUG-sharing-failure] later GET selected connection; wait for actual server close')
                    later_selected.set()
                    assert server_closed.wait(2)
            request.extensions['trace'] = trace
        response = original_send(self, request, *args, **kwargs)
        if request.method == 'PATCH':
            print('[DEBUG-sharing-failure] PATCH response', response.status_code,
                'Connection=', response.headers.get('connection'),
                'request-connection=', request.headers.get('connection'),
                'pool=', repr(self._transport._pool))
        return response
    monkeypatch.setattr(RequestResponseCycle, 'run_asgi', run)
    monkeypatch.setattr(httpx.Client, 'send', send)
    yield
    later_selected.set()
