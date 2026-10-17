"""Bounded read-only failure categories; no targets, messages or credentials."""
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

root = Path('D:/PixivTracker/data')
paths = [root / ('pixiv_tracker.db' + suffix) for suffix in ('', '-wal', '-shm')]
def metadata():
    return {path.name: [path.stat().st_size, path.stat().st_mtime_ns] for path in paths if path.exists()}
output = {'before': metadata(), 'scope': 'failed item categories for diagnostic run, narrow parent selection first'}
connection = sqlite3.connect((root / 'pixiv_tracker.db').as_uri() + '?mode=ro', uri=True, timeout=1)
connection.execute('PRAGMA query_only=ON')
started = time.monotonic()
connection.set_progress_handler(lambda: int(time.monotonic() - started > 10), 1000)
try:
    parents = connection.execute("SELECT operation_id,kind FROM read_task_parent WHERE created_at>=? AND created_at<? AND state='finished'", ('2026-10-13 13:58:39', '2026-10-16 06:30:00')).fetchall()
    groups = Counter()
    failed_parents = 0
    for ident, kind in parents:
        failed = connection.execute("SELECT sum(value) FROM read_task_operation_counts WHERE operation_id=? AND state IN ('failed','not_executed')", (ident,)).fetchone()[0]
        if not failed:
            continue
        failed_parents += 1
        source = connection.execute('SELECT source FROM operations WHERE id=?', (ident,)).fetchone()[0]
        rows = connection.execute("SELECT kind,state,json_extract(result,'$.code'),json_extract(result,'$.failure_code'),json_extract(result,'$.status'),json_extract(result,'$.retryable') FROM operation_items INDEXED BY ix_task_item_problems WHERE operation_id=? AND state IN ('failed','not_executed') ORDER BY position", (ident,))
        for row in rows:
            groups[(kind, source, *row)] += 1
    keys = ('operation_kind','source','item_kind','state','code','failure_code','status','retryable')
    output.update(parents_selected=len(parents), failed_parents=failed_parents,
        groups=[{**dict(zip(keys, values)), 'items': count} for values,count in groups.most_common()])
except sqlite3.Error as error:
    output['error'] = type(error).__name__
finally:
    connection.close()
output.update(elapsed_ms=round((time.monotonic()-started)*1000,1), after=metadata())
output['file_metadata_unchanged'] = output['before'] == output['after']
Path('docs/审查/证据/2026-10-17-读取与调度修订/failure-categories-narrow.json').write_text(json.dumps(output, ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(output, ensure_ascii=False))
