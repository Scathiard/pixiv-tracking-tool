"""Read fixed failure categories of this run; never export targets or messages."""
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import time

root = Path('D:/PixivTracker/data')
path = root / 'pixiv_tracker.db'
files = (path, root / 'pixiv_tracker.db-wal', root / 'pixiv_tracker.db-shm')
def metadata():
    return {p.name: {'bytes': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns}
            for p in files if p.exists()}

out = {'observed_at': datetime.now().astimezone().isoformat(),
       'mode': 'SQLite mode=ro + query_only; fixed categories only', 'before': metadata()}
connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
connection.row_factory = sqlite3.Row
connection.execute('PRAGMA query_only=ON')
deadline = time.monotonic() + 5
connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
began = time.monotonic()
try:
    rows = connection.execute('''
      SELECT o.kind AS operation_kind, o.source, i.kind AS item_kind, i.state,
        json_extract(i.result,'$.failure_code') AS failure_code,
        json_extract(i.result,'$.exception_type') AS exception_type,
        json_extract(i.result,'$.status') AS status,
        json_extract(i.result,'$.retryable') AS retryable,
        count(*) AS items, count(DISTINCT o.id) AS operations
      FROM operations o JOIN operation_items i ON i.operation_id=o.id
      WHERE o.created_at >= ? AND i.state IN ('failed','not_executed')
      GROUP BY o.kind,o.source,i.kind,i.state,failure_code,exception_type,status,retryable
      ORDER BY items DESC
    ''', ('2026-10-13 13:58:39',))
    output = []
    for row in rows:
        value = dict(row)
        for key, field in value.items():
            if isinstance(field, str) and not re.fullmatch(r'[A-Za-z0-9_. -]{1,100}', field):
                value[key] = '[redacted non-category value]'
        output.append(value)
    out['groups'] = output
except sqlite3.Error as error:
    out['error'] = {'type': type(error).__name__, 'message': str(error)}
finally:
    connection.close()
out['ms'] = round((time.monotonic() - began) * 1000, 3)
out['after'] = metadata()
out['file_metadata_unchanged'] = out['before'] == out['after']
Path(__file__).with_name('failure-categories.json').write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(out, ensure_ascii=False))
