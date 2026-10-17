"""Inspect narrow persisted task metadata through SQLite mode=ro, no app imports."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time

root = Path('D:/PixivTracker/data')
path = root / 'pixiv_tracker.db'
out = {'observed_at': datetime.now().astimezone().isoformat(), 'mode': 'SQLite mode=ro + query_only; no app startup',
       'timestamp_convention': 'stored task timestamps are UTC', 'queries': {}, 'errors': {}}
out['before'] = {p.name: {'bytes': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns}
                 for p in (path, root / 'pixiv_tracker.db-wal', root / 'pixiv_tracker.db-shm') if p.exists()}
connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
connection.row_factory = sqlite3.Row
connection.execute('PRAGMA query_only=ON')
deadline = 0
connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)

def query(name, sql, args=()):
    global deadline
    deadline = time.monotonic() + 5
    began = time.monotonic()
    try:
        rows = [dict(row) for row in connection.execute(sql, args)]
        for row in rows:
            if 'id' in row:
                row['local_operation_ref'] = hashlib.sha256(str(row.pop('id')).encode()).hexdigest()[:12]
        out['queries'][name] = {'ms': round((time.monotonic() - began) * 1000, 3), 'rows': rows}
        return rows
    except sqlite3.Error as error:
        out['errors'][name] = {'type': type(error).__name__,
            'sqlite_errorname': getattr(error, 'sqlite_errorname', None),
            'sqlite_errorcode': getattr(error, 'sqlite_errorcode', None),
            'message': str(error), 'ms': round((time.monotonic() - began) * 1000, 3)}
        return []

try:
    query('operation_states', 'SELECT state,count(*) AS count FROM operations GROUP BY state')
    query('recent_operations', '''SELECT id,kind,source,state,phase,phase_total,phase_done,
       pause_requested,created_at,updated_at,started_at,finished_at FROM operations
       WHERE created_at >= ? ORDER BY created_at DESC LIMIT 32''', ('2026-10-13 13:58:39',))
    query('unfinished_operations', '''SELECT id,kind,source,state,phase,phase_total,phase_done,
       pause_requested,created_at,updated_at,started_at FROM operations
       WHERE state IN ('queued','ready','planning','running','interrupted')
       ORDER BY created_at LIMIT 32''')
    query('automatic_checks_this_run', '''SELECT id,state,created_at,started_at,finished_at,
        phase_total,phase_done,json_extract(result,'$.outcome') AS outcome,
        json_extract(result,'$.counts.succeeded') AS succeeded,
        json_extract(result,'$.counts.failed') AS failed,
        json_extract(result,'$.counts.skipped') AS skipped,
        json_extract(result,'$.counts.not_executed') AS not_executed
        FROM operations WHERE kind='check' AND source='automatic' AND created_at >= ?
        ORDER BY created_at LIMIT 20''', ('2026-10-13 13:58:39',))
    query('outcomes_this_run', '''SELECT kind,source,json_extract(result,'$.outcome') AS outcome,
        count(*) AS count FROM operations WHERE created_at >= ? GROUP BY kind,source,outcome''',
        ('2026-10-13 13:58:39',))
    query('automatic_check_setting', "SELECT key,value FROM application_state WHERE key='automatic_paused'")
    query('schema_version', 'SELECT version_num FROM alembic_version')
finally:
    connection.close()
out['after'] = {p.name: {'bytes': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns}
                for p in (path, root / 'pixiv_tracker.db-wal', root / 'pixiv_tracker.db-shm') if p.exists()}
out['file_metadata_unchanged'] = out['before'] == out['after']
Path(__file__).with_name('persisted-tasks.json').write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(out, ensure_ascii=False))
