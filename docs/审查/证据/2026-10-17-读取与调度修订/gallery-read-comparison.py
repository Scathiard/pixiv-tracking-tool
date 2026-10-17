"""One selected real page, read-only, without runtime startup or schema writes."""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

root=Path('D:/claude_sandbox/pixiv-tracking-tool')
worktree=root/'.worktrees/v0.2.0-build20261017.1'
mode=sys.argv[1]
sys.path.insert(0,str((root if mode=='baseline' else worktree)/'app'))
from sqlalchemy import create_engine,event,text
from sqlalchemy.orm import Session
from src.core.projection_maintenance import read_work_states

path=Path('D:/PixivTracker/data/pixiv_tracker.db')
def metadata():
    return {p.name:[p.stat().st_size,p.stat().st_mtime_ns] for p in
        (path,path.with_name(path.name+'-wal'),path.with_name(path.name+'-shm')) if p.exists()}
out={'mode':mode,'observed_at':datetime.now().astimezone().isoformat(),
     'scope':'same visible latest 240 identities, read_work_states, mode=ro; diagnostics disabled in both',
     'before':metadata()}
engine=create_engine('sqlite:///file:D:/PixivTracker/data/pixiv_tracker.db?mode=ro&uri=true',connect_args={'timeout':1})
started=time.monotonic()
steps=[0]
statements=[]
try:
    with Session(engine) as session:
        session.execute(text('PRAGMA query_only=ON'))
        ids=list(session.execute(text('SELECT work_id FROM read_work_order WHERE visible=1 ORDER BY order_key DESC,work_id DESC LIMIT 240')).scalars())
        out['selected_count']=len(ids)
        out['selection_hash']=hashlib.sha256(json.dumps(ids).encode()).hexdigest()
        driver=session.connection().connection.driver_connection
        began=time.monotonic()
        def progress():
            steps[0]+=1000
            return int(time.monotonic()-began>30)
        driver.set_progress_handler(progress,1000)
        result=read_work_states(session,[SimpleNamespace(id=identity) for identity in ids])
        out['read_ms']=round((time.monotonic()-began)*1000,3)
        out['vm_steps_approx']=steps[0]
        out['processing_states']=dict(Counter(row['processing']['state'] for row in result.values()))
        out['result_hash']=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        driver.set_progress_handler(None,0)
except Exception as exc:
    out['error']={'type':type(exc).__name__,'sqlite_type':type(getattr(exc,'orig',exc)).__name__}
    out['read_ms']=round((time.monotonic()-started)*1000,3)
    out['vm_steps_approx']=steps[0]
finally:
    engine.dispose()
out['after']=metadata()
out['file_metadata_unchanged']=out['before']==out['after']
target=worktree/'docs/审查/证据/2026-10-17-读取与调度修订'/('gallery-real-'+mode+'.json')
target.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(out,ensure_ascii=False),flush=True)
