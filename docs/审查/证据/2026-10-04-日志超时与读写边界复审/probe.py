"""Bounded read-only symptom probe; never print or save account/log contents."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import requests

output = Path(__file__).with_name('initial-http.json')
assert not output.exists()
records = []
with requests.Session() as session:
    session.trust_env = False
    for label, path, limit in [('status','/api/status',5),('progress','/api/progress',8),
                               ('library20','/api/library?limit=20&compact=1',8),('logs','/api/logs',31)]:
        item = {'label':label,'started_at':datetime.now(timezone.utc).isoformat()}
        started = time.perf_counter()
        try:
            response = session.get('http://127.0.0.1:1919' + path,timeout=(2,limit))
            item.update(status=response.status_code,bytes=len(response.content),content_type=response.headers.get('content-type'))
            response.raise_for_status()
            value=response.json()
            if label=='progress':
                item.update(worker_error=value.get('worker_error'),active_operation_count=value.get('active_operation_count'))
            if label=='logs':
                item.update(lines=len(value.get('lines',[])),truncated=value.get('truncated'))
        except Exception as error:
            item.update(error=type(error).__name__)
        item['seconds']=round(time.perf_counter()-started,4)
        records.append(item)
        output.write_text(json.dumps(records,indent=2),encoding='utf-8')
        print(json.dumps(item),flush=True)
