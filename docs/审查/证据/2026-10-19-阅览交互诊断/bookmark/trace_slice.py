import json
from pathlib import Path
source = Path(r'D:\PixivTracker\data\diagnostic-20261017-232715-11576-973f0fc8794d-part0041.jsonl')
allowed = set('event utc seq trace parent span duration_ms status route method statements execute_ms pool_ms queue_ms query_kind reason thread'.split())
events = []
with source.open('rb') as stream:
    for line in stream:
        if b'd1e0904faa8f' not in line: continue
        row = json.loads(line)
        if row.get('trace') != 'd1e0904faa8f': continue
        clean = {key:value for key,value in row.items() if key in allowed}
        events.append(clean)
        if row.get('event','').endswith('.end') or row.get('event') == 'sql.slow':
            print(json.dumps(clean))
(Path(__file__).parent/'slow-refresh-trace.json').write_text(json.dumps(events,indent=2),encoding='utf-8')
