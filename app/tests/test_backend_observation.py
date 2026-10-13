"""Small synthetic checks for phase evidence without changing business outcomes."""
import json
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from src.core import diagnostics
from src.core.archive_state import work_states
from src.core.database import ArchiveDatabase
from src.core.models import Artist, Operation, OperationItem, Work
from src.core.tasks import ManualPauseRequested, TaskContext, TaskQueue
from src.routes.media_response import local_media_response


def finish_capture(path):
    diagnostics.stop()
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_state_consumption_keeps_results_and_json_failure(tmp_path):
    database = ArchiveDatabase.create(tmp_path / 'synthetic.db')
    try:
        with database.write() as session:
            session.add(Artist(id=1, pixiv_user_id='synthetic', name='synthetic'))
            session.flush()
            session.add(Work(id=1, pixiv_illust_id='1', artist_id=1, title='synthetic',
                type='illust', page_count=1, downloaded_at=datetime(2026, 10, 13)))
        with database.read() as session:
            expected = work_states(session, list(session.scalars(select(Work))))
        path = diagnostics.start(tmp_path / 'capture', build='synthetic', version='0.2.0', interval=60)
        with database.read() as session:
            assert work_states(session, list(session.scalars(select(Work)))) == expected
        with database.write() as session:
            session.execute(text("UPDATE read_work_state SET value='{' WHERE work_id=1"))
        with database.read() as session:
            with pytest.raises(json.JSONDecodeError):
                work_states(session, list(session.scalars(select(Work))))
        rows = finish_capture(path)
        assert any(row['event'] == 'read.task_winners.consume.end' and row['row_count'] == 0 for row in rows)
        consumed = [row for row in rows if row['event'] == 'read.work_states.consume.end']
        assert consumed[0]['row_count'] == 1 and consumed[0]['decode_ms'] >= 0 and consumed[0]['assemble_ms'] >= 0
        assert consumed[1]['error_type'] == 'JSONDecodeError' and consumed[1]['row_count'] == 1
    finally:
        diagnostics.stop()
        database.close()


def test_scheduler_pause_and_recovery_keep_persistent_states(tmp_path):
    database = ArchiveDatabase.create(tmp_path / 'synthetic.db')
    queue = TaskQueue(database, {'reconcile': object()})
    operation_id, item_id = str(uuid4()), str(uuid4())
    try:
        with database.write() as session:
            session.add(Operation(id=operation_id, kind='reconcile', state='running',
                source='manual', dedupe_key=operation_id, label='synthetic', parameters={}, result={}))
            session.flush()
            session.add(OperationItem(id=item_id, operation_id=operation_id, item_key='synthetic',
                kind='verify_asset', phase='verify', lane='local', state='running', target={}, result={}))
        path = diagnostics.start(tmp_path / 'capture', build='synthetic', version='0.2.0', interval=60)
        queue.recover()
        with database.read() as session:
            assert session.get(OperationItem, item_id).state == 'pending'
        assert queue.set_manual_paused(operation_id, True)['pause_requested'] is True
        with pytest.raises(ManualPauseRequested):
            TaskContext(queue, operation_id, item_id, kind='reconcile').checkpoint()
        assert queue._step('local') is False
        assert queue.details(operation_id)['pause_requested'] is True
        assert queue.snapshot()['operations'][0]['pause_requested'] is True
        rows = finish_capture(path)
        assert any(row['event'] == 'task.recovery.end' and row['item_count'] == 1 for row in rows)
        assert any(row['event'] == 'task.pause_committed' and row['paused'] is True for row in rows)
        assert any(row['event'] == 'task.checkpoint_rejected' and row['reason'] == 'manual_pause' for row in rows)
        assert any(row['event'] == 'event.aggregate' and row['observed_event'] == 'task.scheduler_poll'
            and row['outcome'] == 'no_candidate' for row in rows)
        assert any(row['event'] == 'read.tasks.detail.consume.end' and row['row_count'] == 1 for row in rows)
    finally:
        diagnostics.stop()
        queue.stop()
        database.close()


def test_media_stat_keeps_conditional_response_and_oserror(tmp_path):
    media = tmp_path / 'synthetic.webp'
    media.write_bytes(b'synthetic-media')
    request = SimpleNamespace(headers={})
    expected = local_media_response(request, media)
    path = diagnostics.start(tmp_path / 'capture', build='synthetic', version='0.2.0', interval=60)
    try:
        actual = local_media_response(request, media)
        assert dict(actual.headers) == dict(expected.headers)
        assert local_media_response(SimpleNamespace(headers={'if-none-match': actual.headers['etag']}), media).status_code == 304
        with pytest.raises(FileNotFoundError):
            local_media_response(request, tmp_path / 'missing.webp')
        rows = finish_capture(path)
        assert any(row['event'] == 'media.response.stat.end' and row['file_bytes'] == 15 for row in rows)
        assert any(row['event'] == 'media.response.stat.end' and row.get('error_type') == 'FileNotFoundError' for row in rows)
    finally:
        diagnostics.stop()
