"""Read-only, immutable, bounded original-count measurement; no source imports."""
import faulthandler
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

import psutil

faulthandler.enable()
ARTIFACTS = Path(__file__).resolve().parent
SOURCE_DIRECTORY = Path('D:/claude_sandbox/pixiv-tracking-tool/.devtools/test-runs/b20261004.3-reliability-20261008-10/check-workers-1')
SOURCE = SOURCE_DIRECTORY/'archive.db'
SQLS = [
    ('completed_operation_items', "SELECT count(*) FROM operation_items WHERE operation_id=? AND state IN ('succeeded','failed','skipped','not_executed')"),
    ('verified_assets', "SELECT count(*) FROM assets WHERE state='verified'"),
]


def save(name, value):
    (ARTIFACTS/name).write_text(json.dumps(value, indent=2), encoding='utf-8')


def source_metadata():
    info = SOURCE.stat()
    return {'size_bytes': info.st_size, 'mtime_ns': info.st_mtime_ns,
            'sidecars': [str(path) for suffix in ('-wal', '-shm', '-journal')
                         if (path := SOURCE.with_name(SOURCE.name+suffix)).exists()]}


def preflight():
    original_result = json.loads((SOURCE_DIRECTORY/'result.json').read_text('utf-8'))
    matching, denied = [], []
    for process in psutil.process_iter(['pid', 'name']):
        if process.pid == os.getpid() or 'python' not in (process.info['name'] or '').casefold():
            continue
        try:
            command = ' '.join(process.cmdline()).casefold()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied:
            denied.append(process.pid)
            continue
        if 'b20261004.3-reliability-20261008-10' in command or 'parallel_reliability_probe.py' in command:
            matching.append({'pid': process.pid, 'name': process.info['name'],
                             'reason': 'original trial or another reliability probe'})
    result = {'source': str(SOURCE), 'runtime_closed': original_result.get('runtime_closed'),
              'maintenance_operation_id': original_result.get('maintenance_operation_id'),
              'source_metadata': source_metadata(), 'matching_processes': matching,
              'python_processes_with_denied_cmdline': denied}
    result['safe_to_open_immutable'] = (result['runtime_closed'] is True and
        bool(result['maintenance_operation_id']) and not result['source_metadata']['sidecars']
        and not matching and not denied)
    save('preflight.json', result)
    return result


def probe():
    before = preflight()
    if not before['safe_to_open_immutable']:
        print(json.dumps({'blocked': 'source closure or process evidence was not conclusive'}))
        return 3
    process = psutil.Process()
    uri = SOURCE.as_uri()+'?mode=ro&immutable=1'
    evidence = {'python': sys.version, 'executable': sys.executable,
        'sqlite': sqlite3.sqlite_version, 'uri': uri,
        'per_query_budget_seconds': 4.5, 'batch_budget_seconds': 30,
        'vm_sampling_steps': 1000, 'preflight': before, 'queries': [],
        'logical_read_definition': 'Windows process io_counters().read_bytes; not physical disk reads',
        'cache_limitations': 'first means first in this process/connection; OS caches not cleared; warm reads have no concurrent workload'}
    connection = sqlite3.connect(uri, uri=True)
    batch_start = time.perf_counter()
    deadline = batch_start + 30
    try:
        evidence['connection_defaults'] = {key: connection.execute('PRAGMA '+key).fetchone()[0]
                                           for key in ('cache_size', 'page_size', 'synchronous')}
        for name, sql in SQLS:
            params = (before['maintenance_operation_id'],) if '?' in sql else ()
            query = {'name': name, 'sql': sql, 'parameters': params,
                     'plan': [list(row) for row in connection.execute('EXPLAIN QUERY PLAN '+sql, params)],
                     'runs': []}
            evidence['queries'].append(query)
            for index in range(3):
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    evidence['batch_budget_exhausted'] = True
                    break
                budget = min(4.5, remaining)
                state = {'active': True, 'callbacks': 0}
                lock = threading.Lock()
                start_io = process.io_counters()
                start_cpu = time.process_time()
                started = time.perf_counter()
                query_deadline = started + budget
                def progress():
                    state['callbacks'] += 1
                    return int(time.perf_counter() >= query_deadline)
                def interrupt():
                    with lock:
                        if state['active']:
                            connection.interrupt()
                connection.set_progress_handler(progress, 1000)
                timer = threading.Timer(budget, interrupt)
                timer.daemon = True
                timer.start()
                run = {'phase': 'first' if index == 0 else 'warm-'+str(index), 'budget_seconds': budget}
                try:
                    run['result'] = connection.execute(sql, params).fetchone()[0]
                    run['status'] = 'complete'
                except sqlite3.OperationalError as error:
                    run.update(status='error', error_type=type(error).__name__, error=str(error),
                               sqlite_errorcode=getattr(error, 'sqlite_errorcode', None))
                finally:
                    with lock:
                        state['active'] = False
                    timer.cancel()
                    connection.set_progress_handler(None, 0)
                    run['wall_seconds'] = time.perf_counter() - started
                    run['cpu_seconds'] = time.process_time() - start_cpu
                    end_io = process.io_counters()
                    run['logical_read_bytes'] = end_io.read_bytes-start_io.read_bytes
                    run['logical_read_calls'] = end_io.read_count-start_io.read_count
                    run['vm_steps_sampled'] = state['callbacks']*1000
                    run['within_original_4_5s'] = run['status'] == 'complete' and run['wall_seconds'] < 4.5
                    query['runs'].append(run)
                    save('result.json', evidence)
            if evidence.get('batch_budget_exhausted'):
                break
    finally:
        connection.close()
        evidence['batch_wholewall_seconds'] = time.perf_counter()-batch_start
        evidence['source_metadata_after'] = source_metadata()
        evidence['source_metadata_unchanged'] = evidence['source_metadata_after'] == before['source_metadata']
        save('result.json', evidence)
    print(json.dumps({'batch_wholewall_seconds': evidence['batch_wholewall_seconds'],
        'source_metadata_unchanged': evidence['source_metadata_unchanged'],
        'runs': [{'name': query['name'], 'runs': query['runs']} for query in evidence['queries']]}, indent=2))
    return 0


if '--probe' in sys.argv:
    sys.exit(probe())
else:
    command = [sys.executable, str(Path(__file__).resolve()), '--probe']
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', errors='replace')
    (ARTIFACTS/'stdout.log').write_text(result.stdout, encoding='utf-8')
    (ARTIFACTS/'stderr.log').write_text(result.stderr, encoding='utf-8')
    save('process.json', {'command': command, 'returncode': result.returncode,
        'access_violation_in_output': 'access violation' in (result.stdout+result.stderr).casefold()})
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    print(json.dumps({'returncode': result.returncode}))
    sys.exit(result.returncode)
