"""One read-only comparison; no schema changes or database copies."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
PROTOCOL = json.loads((ROOT/'protocol.json').read_text(encoding='utf-8'))
DATABASE = Path(PROTOCOL['database'])
RUN = ROOT/'run-01'


def guard():
    expected = ROOT.parent/'b20261008-settings-index-comparison-01/run-01/copies/both.db'
    assert DATABASE.resolve() == expected.resolve()
    assert (DATABASE.stat().st_size,DATABASE.stat().st_mtime_ns) == (PROTOCOL['database_size'],PROTOCOL['database_mtime_ns'])
    wal = Path(str(DATABASE)+'-wal')
    assert not wal.exists() or wal.stat().st_size == 0


def helper():
    path = Path(PROTOCOL['helper'])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == PROTOCOL['helper_sha256']
    spec = importlib.util.spec_from_file_location('fixed_copy_probe_helpers',path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def child(arm):
    guard()
    shared = helper()
    snapshot = shared.snapshot_helper()
    c = sqlite3.connect(DATABASE.as_uri()+'?mode=ro&immutable=1',uri=True)
    c.execute('PRAGMA query_only=ON')
    sql,parameters = PROTOCOL['sql'][arm],PROTOCOL['parameters']
    plan = c.execute('EXPLAIN QUERY PLAN '+sql,parameters).fetchall()
    rows = []
    for repeat in range(3):
        calls = 0
        deadline = time.perf_counter()+4.5
        def progress():
            nonlocal calls
            calls += 1
            return int(time.perf_counter()>=deadline)
        c.set_progress_handler(progress,1000)
        def query():
            c.execute('BEGIN')
            return list(c.execute(sql,parameters).fetchone())
        row = shared.measure('query_'+str(repeat),query,10,snapshot)
        c.set_progress_handler(None,0)
        c.rollback()
        row.update(repeat=repeat,temperature='new_process_connection_first' if repeat==0 else 'same_connection_warm',
            vm_quantum=1000,vm_steps_approx=calls*1000,
            equal_expected=row['error'] is None and row['value']==PROTOCOL['expected_result'])
        rows.append(row)
    c.close()
    guard()
    return {'arm':arm,'plan':plan,'rows':rows,'database_metadata_unchanged':True,
        'all_equal':all(row['equal_expected'] for row in rows)}


def supervise(arm,index):
    process = subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).resolve()),'--child',arm],
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')
    messages = queue.Queue()
    def receive():
        for line in process.stdout:
            messages.put(line)
        messages.put(None)
    reader = threading.Thread(target=receive,daemon=True)
    reader.start()
    started = time.perf_counter()
    overall,deadline = started+75,started+30
    value,failure = None,None
    with (RUN/f'{index:02d}-{arm}.jsonl').open('x',encoding='utf-8') as output:
        while True:
            left = min(overall,deadline)-time.perf_counter()
            if left<=0:
                failure='hard_wall_timeout'
                process.kill()
                break
            try:
                line=messages.get(timeout=left)
            except queue.Empty:
                failure='hard_wall_timeout'
                process.kill()
                break
            if line is None:
                break
            output.write(line)
            output.flush()
            try:
                message=json.loads(line)
            except json.JSONDecodeError:
                continue
            if message['event']=='operation_started':
                deadline=min(overall,time.perf_counter()+message['hard_seconds'])
            elif message['event']=='operation_completed':
                deadline=min(overall,time.perf_counter()+30)
            elif message['event']=='result':
                value=message['result']
    process.wait(timeout=3)
    reader.join(timeout=1)
    return {'arm':arm,'index':index,'pid':process.pid,'exit_code':process.returncode,
        'failure':failure or ('child_failed' if process.returncode!=0 or value is None else None),
        'supervisor_seconds':time.perf_counter()-started,'result':value}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--child',choices=['baseline','candidate'])
    args=parser.parse_args()
    if args.child:
        print(json.dumps({'event':'result','result':child(args.child)}),flush=True)
        return
    guard()
    RUN.mkdir()
    records=[]
    for index,arm in enumerate(('baseline','candidate','candidate','baseline')):
        record=supervise(arm,index)
        records.append(record)
        (RUN/f'{index:02d}-{arm}.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
        print(arm,record['failure'] or [(round(row['seconds'],4),row['error'],row['value']) for row in record['result']['rows']],flush=True)
    guard()
    result={'protocol':PROTOCOL,'records':records,'database_metadata_unchanged':True,
        'all_12_queries_equal':all(not record['failure'] and record['result']['all_equal'] for record in records),
        'all_children_waited_exit_zero':all(record['exit_code']==0 for record in records),
        'no_os_cache_flush':True,'process_io_is_not_physical_io':True}
    (RUN/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
