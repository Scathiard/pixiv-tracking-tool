"""Controlled synthetic-copy probe. Prepared only; --run requires parent start."""
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
RUN = ROOT / 'run-01'
PROTOCOL = json.loads((ROOT / 'protocol.json').read_text(encoding='utf-8'))
SOURCE = Path(PROTOCOL['source_database'])
ARMS = list(PROTOCOL['arms'])


def emit(event, **values):
    print(json.dumps({'event': event, **values}), flush=True)


def snapshot_helper():
    path = Path(PROTOCOL['process_snapshot_helper'])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == PROTOCOL['process_snapshot_helper_sha256']
    spec = importlib.util.spec_from_file_location('fixed_settings_probe_helper', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.process_snapshot


def measure(label, function, hard_seconds, snapshot):
    emit('operation_started', label=label, hard_seconds=hard_seconds)
    before = snapshot()
    cpu, started = time.process_time(), time.perf_counter()
    error, value = None, None
    try:
        value = function()
    except sqlite3.Error as exc:
        error = f'{type(exc).__name__}: {exc}'
    ended, cpu_end = time.perf_counter(), time.process_time()
    after = snapshot()
    result = {'label': label, 'seconds': ended-started, 'cpu_seconds': cpu_end-cpu,
        'error': error, 'value': value,
        'io_delta': {key: after['io'][key]-before['io'][key] for key in before['io']},
        'rss_bytes': after['working_set_bytes'], 'peak_rss_bytes': after['peak_working_set_bytes']}
    emit('operation_completed', label=label)
    return result


def source_guard():
    assert SOURCE.resolve() == Path(r'D:\claude_sandbox\pixiv-tracking-tool\.devtools\test-runs\b20261004.3-reliability-20261008-08\check-workers-1\archive.db').resolve()
    assert (SOURCE.stat().st_size, SOURCE.stat().st_mtime_ns) == (PROTOCOL['source_size'], PROTOCOL['source_mtime_ns'])
    assert json.loads((SOURCE.parent/'result.json').read_text(encoding='utf-8'))['runtime_closed'] is True
    wal = Path(str(SOURCE)+'-wal')
    assert not wal.exists() or wal.stat().st_size == 0


def database(arm):
    assert arm in ARMS
    return RUN / 'copies' / (arm+'.db')


def copy_database(arm, snapshot):
    source_guard()
    target = database(arm)
    assert not target.exists()
    def copy():
        digest = hashlib.sha256()
        with SOURCE.open('rb') as source, target.open('xb') as output:
            while block := source.read(1024*1024):
                digest.update(block)
                output.write(block)
            output.flush()
        assert target.stat().st_size == PROTOCOL['source_size']
        return {'sha256': digest.hexdigest(), 'bytes': target.stat().st_size}
    result = measure('copy', copy, 180, snapshot)
    def verify_target():
        digest = hashlib.sha256()
        read_bytes = 0
        with target.open('rb') as stream:
            while block := stream.read(1024*1024):
                digest.update(block)
                read_bytes += len(block)
        return {'sha256': digest.hexdigest(), 'bytes': read_bytes,
            'matches_source_stream': not result['error'] and digest.hexdigest() == result['value']['sha256']}
    verification = measure('verify_target', verify_target, 180, snapshot)
    source_guard()
    return {'copy': result, 'target_verification': verification}


def metadata(connection):
    return {name: connection.execute('PRAGMA '+name).fetchone()[0]
        for name in ('page_size', 'page_count', 'freelist_count', 'cache_size', 'synchronous', 'journal_mode')}


def prepare(arm, snapshot):
    c = sqlite3.connect(database(arm))
    c.execute('PRAGMA synchronous=FULL')
    before = metadata(c)
    creations = []
    for key in PROTOCOL['arms'][arm]:
        def create(key=key):
            c.execute('BEGIN IMMEDIATE')
            try:
                c.execute(PROTOCOL['indexes'][key])
                c.commit()
            except BaseException:
                c.rollback()
                raise
        creations.append(measure('create_'+key, create, 180, snapshot))
        if creations[-1]['error']:
            break
    sizes = {}
    for name in ('ix_works_settings_shape', 'ix_revisions_settings_shape'):
        present = c.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', ('index', name)).fetchone()
        if present:
            try:
                pages, size = c.execute('SELECT count(*),coalesce(sum(pgsize),0) FROM dbstat WHERE name=?', (name,)).fetchone()
                sizes[name] = {'definition': present[0], 'pages': pages, 'bytes': size}
            except sqlite3.Error as exc:
                sizes[name] = {'definition': present[0], 'error': str(exc)}
    after = metadata(c)
    checkpoint = measure('checkpoint', lambda: list(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()), 180, snapshot)
    c.close()
    wal = Path(str(database(arm))+'-wal')
    clean = not wal.exists() or wal.stat().st_size == 0
    return {'before': before, 'after': after, 'creations': creations, 'index_sizes': sizes,
        'checkpoint': checkpoint, 'closed_without_wal': clean,
        'ready': clean and not any(x['error'] for x in creations) and not checkpoint['error']}


def read_queries(arm, snapshot):
    path = database(arm)
    wal = Path(str(path)+'-wal')
    assert not wal.exists() or wal.stat().st_size == 0
    c = sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)
    c.execute('PRAGMA query_only=ON')
    sql, params = PROTOCOL['sql'], PROTOCOL['parameters']
    plan = c.execute('EXPLAIN QUERY PLAN '+sql, params).fetchall()
    rows = []
    for repeat in range(3):
        calls = 0
        deadline = time.perf_counter()+PROTOCOL['progress_budget_seconds']
        def progress():
            nonlocal calls
            calls += 1
            return int(time.perf_counter() >= deadline)
        c.set_progress_handler(progress, 1000)
        def query():
            c.execute('BEGIN')
            return list(c.execute(sql, params).fetchone())
        row = measure('query_'+str(repeat), query, PROTOCOL['query_hard_budget_seconds'], snapshot)
        c.set_progress_handler(None, 0)
        c.rollback()
        row.update(repeat=repeat, temperature='new_process_connection_first' if repeat == 0 else 'same_connection_warm',
            vm_steps_lower_bound=calls*1000, vm_steps_upper_bound=calls*1000+999)
        row['equal_expected'] = row['error'] is None and row['value'] == PROTOCOL['expected_result']
        rows.append(row)
    info = metadata(c)
    c.close()
    return {'plan': plan, 'metadata': info, 'rows': rows, 'all_equal': all(row['equal_expected'] for row in rows)}


def update_cost(arm, snapshot):
    c = sqlite3.connect(database(arm))
    c.execute('PRAGMA synchronous=FULL')
    original = c.execute('SELECT w.id,w.default_revision_id,w.type,w.page_count,r.type,r.page_count '
        'FROM works w JOIN work_revisions r ON r.id=w.default_revision_id ORDER BY w.id LIMIT 1').fetchone()
    assert original is not None
    wid, rid, wt, wp, rt, rp = original
    rows = []
    for repeat in range(3):
        for restoring in (False, True):
            calls = 0
            def count():
                nonlocal calls
                calls += 1
                return 0
            c.set_progress_handler(count, 100)
            def transaction():
                transaction_started = time.perf_counter()
                c.execute('BEGIN IMMEDIATE')
                try:
                    c.execute('UPDATE works SET default_revision_id=?,type=?,page_count=? WHERE id=?',
                        (rid if restoring else None, wt if restoring else ('manga' if wt!='manga' else 'illust'), wp if restoring else wp+1, wid))
                    c.execute('UPDATE work_revisions SET type=?,page_count=? WHERE id=?',
                        (rt if restoring else ('manga' if rt!='manga' else 'illust'), rp if restoring else rp+1, rid))
                    before_commit = time.perf_counter()
                    c.commit()
                    return {'dml_and_begin_seconds': before_commit-transaction_started,
                        'commit_seconds': time.perf_counter()-before_commit}
                except BaseException:
                    c.rollback()
                    raise
            row = measure(('restore_' if restoring else 'update_')+str(repeat), transaction, 10, snapshot)
            c.set_progress_handler(None, 0)
            row.update(repeat=repeat, restoring=restoring, vm_steps_lower_bound=calls*100, vm_steps_upper_bound=calls*100+99)
            rows.append(row)
            if row['error']:
                c.close()
                return {'original': original, 'rows': rows, 'restored': False}
    actual = c.execute('SELECT w.id,w.default_revision_id,w.type,w.page_count,r.type,r.page_count '
        'FROM works w JOIN work_revisions r ON r.id=w.default_revision_id WHERE w.id=?', (wid,)).fetchone()
    restored = actual == original
    checkpoint = measure('checkpoint', lambda: list(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()), 10, snapshot)
    c.close()
    return {'original': original, 'rows': rows, 'restored': restored, 'checkpoint': checkpoint}


def supervise(stage, arm, serial, hard_seconds):
    log = RUN / f'{serial:02d}-{stage}-{arm}.jsonl'
    process = subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--child', stage, '--arm', arm],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8')
    messages = queue.Queue()
    def receive():
        for line in process.stdout:
            messages.put(line)
        messages.put(None)
    reader = threading.Thread(target=receive, daemon=True)
    reader.start()
    started = time.perf_counter()
    overall_deadline = started+hard_seconds
    deadline = min(overall_deadline, started+(30 if stage in ('query', 'update') else hard_seconds))
    result, failed = None, None
    with log.open('x', encoding='utf-8') as output:
        while True:
            remaining = min(deadline, overall_deadline)-time.perf_counter()
            if remaining <= 0:
                failed = 'hard_wall_timeout'
                process.kill()
                break
            try:
                line = messages.get(timeout=remaining)
            except queue.Empty:
                failed = 'hard_wall_timeout'
                process.kill()
                break
            if line is None:
                break
            output.write(line)
            output.flush()
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message['event'] == 'operation_started':
                deadline = min(overall_deadline, time.perf_counter()+message['hard_seconds'])
            elif message['event'] == 'operation_completed':
                deadline = min(overall_deadline, time.perf_counter()+30)
            elif message['event'] == 'result':
                result = message['result']
    process.wait(timeout=3)
    reader.join(timeout=1)
    record = {'stage': stage, 'arm': arm, 'pid': process.pid, 'exit_code': process.returncode,
        'supervisor_seconds': time.perf_counter()-started, 'failure': failed, 'result': result, 'log': log.name}
    if process.returncode != 0 or result is None:
        record['failure'] = failed or 'child_failed'
    (RUN / f'{serial:02d}-{stage}-{arm}.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(stage, arm, record['failure'] or 'completed', flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--child', choices=['copy', 'prepare', 'query', 'update'])
    parser.add_argument('--arm', choices=ARMS)
    args = parser.parse_args()
    if args.child:
        function = {'copy': copy_database, 'prepare': prepare, 'query': read_queries, 'update': update_cost}[args.child]
        emit('result', result=function(args.arm, snapshot_helper()))
        return
    if not args.run:
        print('Prepared only. Parent start notification is required before --run. No database opened.')
        return
    source_guard()
    RUN.mkdir()
    (RUN/'copies').mkdir()
    records, ready, hashes = [], [], []
    def launch(stage, arm, budget):
        record = supervise(stage, arm, len(records), budget)
        records.append(record)
        return record
    for arm in ARMS:
        record = launch('copy', arm, 180)
        if not record['failure']:
            copied = record['result']['copy']
            verified = record['result']['target_verification']
            if not copied['error'] and not verified['error'] and verified['value']['matches_source_stream']:
                hashes.append(verified['value']['sha256'])
    assert len(hashes) == len(ARMS) and len(set(hashes)) == 1, 'Copies incomplete or source differs; retained without further work'
    for arm in ARMS:
        record = launch('prepare', arm, 180)
        if not record['failure'] and record['result']['ready']:
            ready.append(arm)
    for arm in ARMS+list(reversed(ARMS)):
        if arm in ready:
            launch('query', arm, 75)
    for arm in ARMS:
        if arm in ready:
            launch('update', arm, 30)
    source_guard()
    result = {'protocol': PROTOCOL, 'source_sha256': hashes[0], 'source_metadata_unchanged': True,
        'os_cache_not_flushed': True, 'copy_verification_and_creation_warm_os_cache': True,
        'process_io_is_not_physical_io': True, 'ready_arms': ready, 'records': records}
    queries = [record for record in records if record['stage']=='query']
    result['all_24_queries_equal'] = len(queries)==8 and all(not r['failure'] and r['result']['all_equal'] for r in queries)
    result['all_updates_restored'] = len(ready)==4 and all(not r['failure'] and r['result']['restored'] for r in records if r['stage']=='update')
    (RUN/'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('Finished; copies and logs retained. Read result.json for failures and equality.', flush=True)


if __name__ == '__main__':
    main()
