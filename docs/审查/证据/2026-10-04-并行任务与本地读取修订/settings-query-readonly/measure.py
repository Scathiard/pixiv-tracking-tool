"""Read-only, serial settings SQL/cache comparison on a closed synthetic DB."""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent/'b20261004.3-reliability-20261008-08'/'check-workers-1'
DATABASE = SOURCE/'archive.db'
OBSERVATIONS = ROOT.parent/'b20261004.3-reliability-settings-isolated-20261008-07'/'result.json'


class IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        'read_operations', 'write_operations', 'other_operations',
        'read_bytes', 'write_bytes', 'other_bytes')]


class MemoryCounters(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
            'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]


def process_snapshot():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.GetCurrentProcess()
    io = IOCounters()
    kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IOCounters)]
    assert kernel.GetProcessIoCounters(handle, ctypes.byref(io))
    memory = MemoryCounters()
    memory.cb = ctypes.sizeof(memory)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    assert psapi.GetProcessMemoryInfo(handle, ctypes.byref(memory), memory.cb)
    return {'io': {name: getattr(io, name) for name, _ in io._fields_},
        'working_set_bytes': memory.WorkingSetSize, 'peak_working_set_bytes': memory.PeakWorkingSetSize}


def queries():
    prior = json.loads(OBSERVATIONS.read_text(encoding='utf-8'))
    observation = next(item for item in prior['query_observations'] if 'downloaded_pages' in item['sql'])
    baseline, parameters = observation['sql'], observation['parameters']
    start = baseline.index('FROM assets JOIN (SELECT revision_assets.asset_id')
    shape_start = baseline.index('WHERE current_works.type = ?', start)
    shape_end = baseline.index(' GROUP BY assets.work_id)', shape_start)
    shape = baseline[shape_start+len('WHERE '):shape_end]
    candidate = baseline[:start] + '''FROM assets JOIN current_works ON current_works.work_id = assets.work_id
WHERE ((current_works.revision_id IS NULL AND assets.revision_id IS NULL)
    OR EXISTS (SELECT 1 FROM revision_assets
        WHERE revision_assets.asset_id = assets.id
          AND revision_assets.revision_id IN (SELECT revision_id FROM current_works)))
  AND (''' + shape + ')' + baseline[shape_end:]
    return {'baseline': baseline, 'candidate': candidate}, parameters


def child(query, cache):
    assert DATABASE.resolve() == Path(r'D:\claude_sandbox\pixiv-tracking-tool\.devtools\test-runs\b20261004.3-reliability-20261008-08\check-workers-1\archive.db').resolve()
    assert json.loads((SOURCE/'result.json').read_text(encoding='utf-8'))['runtime_closed'] is True
    wal = Path(str(DATABASE)+'-wal')
    assert not wal.exists() or wal.stat().st_size == 0
    before_file = (DATABASE.stat().st_size, DATABASE.stat().st_mtime_ns)
    sql, parameters = queries()
    connection = sqlite3.connect(DATABASE.as_uri()+'?mode=ro&immutable=1', uri=True)
    connection.execute('PRAGMA query_only=ON')
    original_cache = connection.execute('PRAGMA cache_size').fetchone()[0]
    if cache == '32m':
        connection.execute('PRAGMA cache_size=-32768')
    cache_size = connection.execute('PRAGMA cache_size').fetchone()[0]
    plan = connection.execute('EXPLAIN QUERY PLAN '+sql[query], parameters).fetchall()
    rows = []
    for repeat in range(3):
        calls = 0
        deadline = time.perf_counter()+4.5
        def progress():
            nonlocal calls
            calls += 1
            return int(time.perf_counter() >= deadline)
        connection.set_progress_handler(progress, 1000)
        before = process_snapshot()
        cpu, started = time.process_time(), time.perf_counter()
        error, value = None, None
        try:
            connection.execute('BEGIN')
            value = list(connection.execute(sql[query], parameters).fetchone())
        except sqlite3.OperationalError as exc:
            error = str(exc)
        finally:
            ended, cpu_end = time.perf_counter(), time.process_time()
            after = process_snapshot()
            connection.set_progress_handler(None, 0)
            connection.rollback()
        rows.append({'repeat': repeat, 'temperature': 'first' if repeat == 0 else 'same_connection_warm',
            'seconds': ended-started, 'cpu_seconds': cpu_end-cpu, 'result': value, 'error': error,
            'vm_steps_lower_bound': calls*1000, 'vm_steps_upper_bound': calls*1000+999,
            'progress_quantum': 1000, 'io_delta': {key: after['io'][key]-before['io'][key] for key in before['io']},
            'working_set_bytes': after['working_set_bytes'], 'peak_working_set_bytes': after['peak_working_set_bytes']})
    connection.close()
    assert before_file == (DATABASE.stat().st_size, DATABASE.stat().st_mtime_ns)
    return {'query': query, 'cache': cache, 'original_cache_size': original_cache,
        'cache_size': cache_size, 'query_only': True, 'uri_mode': 'ro&immutable=1',
        'file_unchanged': True, 'plan': plan, 'rows': rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--query', choices=['baseline', 'candidate'])
    parser.add_argument('--cache', choices=['default', '32m'])
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child(args.query, args.cache)))
        return
    assert args.output and not args.output.exists(), 'Use a new output path'
    sql, parameters = queries()
    order = [('baseline', 'default'), ('baseline', '32m'), ('candidate', '32m'), ('candidate', 'default')]
    results = []
    for index, (query, cache) in enumerate(order+list(reversed(order))):
        process = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
            '--child', '--query', query, '--cache', cache], capture_output=True, text=True, check=True, timeout=25)
        result = json.loads(process.stdout)
        result['order_index'] = index
        results.append(result)
        print(query, cache, [(round(r['seconds'], 4), r['result'], r['error']) for r in result['rows']], flush=True)
    complete = [row['result'] for result in results for row in result['rows'] if row['error'] is None]
    equal = bool(complete) and all(row == complete[0] for row in complete)
    args.output.write_text(json.dumps({'database': str(DATABASE), 'source_commit': '9eb03bc',
        'no_background_workers': True, 'no_os_cache_flush': True,
        'process_io_is_not_physical_io': True, 'columns': ['downloaded_pages','registered_originals',
            'verified_originals','unverified_originals','missing_originals'], 'all_complete_results_equal': equal,
        'sql': sql, 'parameters': parameters, 'results': results}, indent=2), encoding='utf-8')
    assert equal


if __name__ == '__main__':
    main()
