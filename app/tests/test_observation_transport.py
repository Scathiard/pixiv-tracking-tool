"""Observer-only integration: bounded browser intake and streaming correlation."""
import asyncio
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
import pytest

from src.core import diagnostics
from src.routes.diagnostic_api import create_diagnostic_router


def captured(path):
    diagnostics.stop()
    return [json.loads(line) for part in sorted(path.parent.glob('diagnostic-*.jsonl'))
            for line in part.read_text(encoding='utf-8').splitlines()]


def test_fast_aggregation_slow_active_work_and_probe_failure(tmp_path, monkeypatch):
    path = diagnostics.start(tmp_path, build='test', version='0.2.0', interval=60)
    recorder = diagnostics._recorder
    try:
        for _ in range(100):
            with diagnostics.span('db.read.body', endpoint='synthetic'):
                pass
        with diagnostics.span('db.read.body', endpoint='synthetic_slow'):
            recorder.sample()
            time.sleep(.03)
        with monkeypatch.context() as patch:
            patch.setattr(diagnostics, '_clean', lambda *args: (_ for _ in ()).throw(ValueError('private')))
            diagnostics.emit('synthetic.failed_observer')
            with diagnostics.span('synthetic.failed_observer'):
                body_executed = True
        rows = captured(path)
        assert body_executed
        assert not any(row['event'] == 'db.read.body.begin' for row in rows)
        assert any(row['event'] == 'event.aggregate' and row['observed_event'] == 'db.read.body'
                   and row['count'] == 100 for row in rows)
        assert any(row['event'] == 'watch.active' and any(s['event'] == 'db.read.body' for s in row['spans']) for row in rows)
        assert any(row['event'] == 'db.read.body.end' and row['endpoint'] == 'synthetic_slow'
                   and row['duration_ms'] >= 25 and 'started_elapsed_ms' in row for row in rows)
        health = [row for row in rows if row['event'] == 'process.sample'][-1]
        assert health['probe_errors'] >= 2 and health['dropped_records'] == 0
        assert all(key in health for key in ('log_bytes', 'log_records_per_second', 'log_flush_ms', 'writer_cpu_ms', 'queue_peak'))
        assert 'private' not in json.dumps(rows)
    finally:
        diagnostics.stop()


def test_browser_intake_correlates_streaming_and_discards_private_fields(tmp_path):
    path = diagnostics.start(tmp_path, build='test', version='0.2.0', interval=60)
    app = FastAPI()
    app.add_middleware(diagnostics.DiagnosticHTTPMiddleware)
    app.include_router(create_diagnostic_router())

    @app.get('/api/example/{work_id}')
    async def example(work_id: str, request: Request):
        request.scope['state']['diagnostic_business_ready_at'] = time.monotonic()
        async def body():
            yield b'first'
            await asyncio.sleep(.01)
            yield b'last'
        return StreamingResponse(body(), media_type='text/plain')

    session, request_id = 'a' * 32, 'b' * 32
    try:
        with TestClient(app) as client:
            capability = client.get('/api/diagnostics').json()
            response = client.get('/api/example/PRIVATE_ID?filter=PRIVATE_FILTER', headers={
                'X-Pixiv-Client-Session': session, 'X-Pixiv-Client-Request': request_id})
            assert response.text == 'firstlast'
            trace = response.headers['X-Pixiv-Request-Id']
            assert response.headers['X-Pixiv-Diagnostic-Run'] == capability['run_id']
            assert trace in response.headers['Server-Timing']
            batch = {'session': session, 'events': [{'event': 'api.end', 'request_id': request_id,
                'server_trace': trace, 'server_run': capability['run_id'], 'outcome': 'ok',
                'url': 'PRIVATE_URL', 'payload': {'secret': 'PRIVATE_SECRET'}, 'duration_ms': 5}]}
            assert client.post('/api/diagnostics/events', json=batch).status_code == 204
            assert client.post('/api/diagnostics/events', json={'session': session, 'events': batch['events'] * 33}).status_code == 400
            assert client.post('/api/diagnostics/events', content=b'x' * 32769).status_code == 413
            assert client.post('/api/diagnostics/events', json={'session': session, 'events': [{'event': {'bad': 'input'}}]}).status_code == 400
        rows = captured(path)
        http = next(row for row in rows if row['event'] == 'http.request.end')
        browser = next(row for row in rows if row['event'] == 'browser.event')
        assert http['trace'] == browser['server_trace'] == trace
        assert http['client_request'] == browser['request_id'] == request_id
        assert http['client_session'] == browser['session'] == session
        assert http['bytes_sent'] == 9 and http['send_calls'] >= 3
        assert http['headers_ms'] <= http['first_body_ms'] <= http['last_body_ms']
        assert http['after_business_ms'] >= 0
        assert sum(row['event'] == 'http.request.end' for row in rows) == 1  # Intake never traces itself.
        assert 'PRIVATE_' not in json.dumps(rows)
        with TestClient(app) as client:
            assert client.get('/api/diagnostics').status_code == 404
            assert client.post('/api/diagnostics/events', json=batch).status_code == 404
    finally:
        diagnostics.stop()
