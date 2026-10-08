"""Test instrumentation only: independent observations and a bounded closed audit."""
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

SETTLED = ('succeeded', 'failed', 'skipped', 'not_executed')
AUDIT_BATCH_ROWS = 128
AUDIT_BATCH_SECONDS = 4.5
AUDIT_TOTAL_SECONDS = 300
AUDIT_MAX_ROW_SPAN = 2_000_000
TABLE_COLUMNS = {
    'operation_items': 'operation_id,state', 'assets': 'state', 'derivatives': 'state',
    'remote_bookmarks': 'confirmed_at', 'publication_journal': 'id',
}


class ObservationFailure(RuntimeError):
    pass


class AuditFailure(ObservationFailure):
    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


def utc(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class PeriodicObserver:
    """Own one independent schedule; failures never suppress another observer."""
    def __init__(self, callback, failure, *, began, deadline, interval=5):
        self.callback, self.failure = callback, failure
        self.began, self.deadline, self.interval = began, deadline, interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        next_due = self.began
        while not self.stop_event.is_set() and time.monotonic() < self.deadline:
            if self.stop_event.wait(max(0, next_due-time.monotonic())):
                break
            if time.monotonic() >= self.deadline:
                break
            try:
                self.callback()
            except Exception as error:
                self.failure(error)
            # Do not speed up missed samples or expand coverage tolerances.
            next_due = max(next_due+self.interval, self.began +
                           (math.floor((time.monotonic()-self.began)/self.interval)+1)*self.interval)

    def start(self):
        self.thread.start()
        return self

    def stop(self, timeout=10):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout)
        return not self.thread.is_alive()


def resource_sample(runtime, process, began):
    """System counters, file metadata and bounded RAM snapshots; no DB lease."""
    wal = Path(str(runtime.database.path)+'-wal')
    cpu = process.cpu_times()
    return {'elapsed_seconds': time.monotonic()-began, 'rss_bytes': process.memory_info().rss,
        'cpu_user_seconds': cpu.user, 'cpu_system_seconds': cpu.system,
        'cpu_percent_since_last_sample': process.cpu_percent(interval=None),
        'wal_bytes': wal.stat().st_size if wal.exists() else 0,
        'read_active': dict(runtime.database.reads.active), 'read_pending': dict(runtime.database.reads.pending),
        'read_connections': runtime.database.engine.pool.checkedout(),
        'write_connections': runtime.database.writer_engine.pool.checkedout(),
        'worker_error': runtime.queue.worker_error, 'checkpoint': runtime.wal_maintenance.snapshot()}


class MaintenanceReports:
    def __init__(self):
        self.lock = threading.Lock()
        self.observations = []
        self.completed = None

    def record(self, value):
        with self.lock:
            self.observations.append(value)
            if value['completion_observed_in_window']:
                self.completed = value

    def snapshot(self):
        with self.lock:
            return list(self.observations), self.completed


def observe_maintenance(requester, origin, operation_id, *, began, deadline, wall_deadline,
                        reports, telemetry, coverage):
    """One complete real task-detail response; any transport/content failure is hard."""
    path = f'/api/tasks/{operation_id}?limit=1'
    started = time.monotonic()
    record = {'request_kind': 'maintenance_observation', 'path': path, 'operation_id': operation_id}
    try:
        with requester(origin+path, timeout=(5, 5)) as response:
            body = response.content
            record.update(status=response.status_code, bytes=len(body))
            value = json.loads(body)
            ended = time.monotonic()
            elapsed = ended-started
            record.update(status=response.status_code, elapsed_ms=elapsed*1000, bytes=len(body))
            if response.status_code != 200:
                raise ObservationFailure('maintenance HTTP was not 200')
            if elapsed > 5:
                raise ObservationFailure('complete maintenance HTTP response exceeded 5s')
            if not isinstance(value, dict) or value.get('id') != operation_id:
                raise ObservationFailure('maintenance response identity/structure mismatch')
            counts, result = value.get('counts'), value.get('result')
            if not isinstance(counts, dict) or any(not isinstance(key, str) or type(count) is not int or count < 0
                                                  for key, count in counts.items()):
                raise ObservationFailure('maintenance counts missing or invalid')
            if not isinstance(value.get('state'), str) or not isinstance(result, dict):
                raise ObservationFailure('maintenance state/result missing')
            outcome, finished = result.get('outcome'), value.get('finished_at')
            if value['state'] == 'finished' and (not isinstance(outcome, str) or not isinstance(finished, str)):
                raise ObservationFailure('finished maintenance outcome/time missing')
            finished_time = utc(finished) if finished is not None else None
            if value['state'] == 'finished' and outcome != 'succeeded':
                raise ObservationFailure('full maintenance finished unsuccessfully')
            if value['state'] == 'finished' and any(count for state, count in counts.items() if state not in SETTLED):
                raise ObservationFailure('finished maintenance reports unfinished items')
            if outcome == 'succeeded' and (not sum(counts.values()) or counts.get('failed', 0) or counts.get('not_executed', 0)):
                raise ObservationFailure('maintenance item counts contradict succeeded outcome')
            eligible = ended <= deadline and value['state'] == 'finished' and outcome == 'succeeded' and finished_time <= utc(wall_deadline)
            observation = {'elapsed_seconds': ended-began, 'response_seconds': elapsed,
                'state': value['state'], 'outcome': outcome, 'finished_at': finished,
                'counts': counts, 'settled': sum(counts.get(state, 0) for state in SETTLED),
                'completion_observed_in_window': eligible}
            reports.record(observation)
            if ended <= deadline:
                coverage.observe('maintenance', ended)
            telemetry.duration('metadata', elapsed)
            telemetry.duration('maintenance_http', elapsed)
            record.update(ok=True, observation=observation)
    except Exception as error:
        record.update(ok=False, elapsed_ms=(time.monotonic()-started)*1000,
                      error_type=type(error).__name__, error=str(error)[:160])
        telemetry.fail(f'maintenance HTTP observation: {type(error).__name__}: {str(error)[:160]}')
    finally:
        with telemetry.lock:
            telemetry.http.write(json.dumps(record)+'\n')
    return record


def completed_in_window(observations, began, window_end, wall_end):
    """Use the actual workload boundary, including an early stop after failure."""
    for value in observations:
        value['completion_observed_in_window'] = (
            value['completion_observed_in_window'] and began is not None and
            value['elapsed_seconds'] <= window_end-began and utc(value['finished_at']) <= utc(wall_end))
    return next((value for value in reversed(observations) if value['completion_observed_in_window']), None)


def enforce_soak_gates(result, args, fail):
    """No SQL; each required gate remains reachable after any earlier failure."""
    if args.pilot_seconds is not None:
        return
    checks = [
        (result.get('elapsed_seconds', 0) >= max(8*3600, args.duration_hours*3600), 'soak duration shorter than eight actual hours'),
        (result.get('maintenance_completed') is True, 'full 15000-work maintenance cycle did not complete successfully within the run window'),
        (result.get('overlap', {}).get('checking_download') is True, 'production overlap not witnessed: checking_download'),
        (result.get('overlap', {}).get('checking_preview') is True, 'production overlap not witnessed: checking_preview'),
    ]
    for valid, message in checks:
        if not valid:
            fail(message)
    coverage = result.get('activity_coverage')
    if coverage is None:
        fail('continuous soak coverage: observation coverage missing')
    else:
        for violation in coverage['violations']:
            fail('continuous soak coverage: '+violation)


@contextmanager
def bounded_read(connection, evidence, *, total_deadline, seconds=4.5, clock=time.monotonic):
    deadline = min(total_deadline, clock()+seconds)
    if deadline <= clock():
        raise AuditFailure('closed audit total deadline exhausted', evidence)
    active = {'value': True}
    lock = threading.Lock()
    def interrupt():
        with lock:
            if active['value']:
                connection.interrupt()
    timer = threading.Timer(max(0, deadline-clock()), interrupt)
    timer.daemon = True
    connection.set_progress_handler(lambda: int(clock() >= deadline), 1000)
    timer.start()
    try:
        yield
        if clock() >= deadline:
            raise AuditFailure('closed audit batch/total deadline exhausted', evidence)
    except sqlite3.OperationalError as error:
        raise AuditFailure('closed audit SQL failed: '+type(error).__name__, evidence) from error
    finally:
        with lock:
            active['value'] = False
        timer.cancel()
        connection.set_progress_handler(None, 0)


def fetch_audit_batch(connection, table, low, high):
    return connection.execute(f'SELECT rowid,{TABLE_COLUMNS[table]} FROM "{table}" '
        'WHERE rowid>? AND rowid<=? ORDER BY rowid', (low, high)).fetchall()


def audit_persistence(path, operation_id, started_wall, completed_report, *, require_complete,
                      fixture_works, fixture_history, batch_rows=AUDIT_BATCH_ROWS,
                      total_seconds=AUDIT_TOTAL_SECONDS, clock=time.monotonic):
    """Scan every frozen row in bounded rowid intervals, with independent coverage counts."""
    path = Path(path).resolve()
    evidence = {'status': 'preparing', 'tables': {}, 'batch_rows': batch_rows,
        'per_batch_seconds': AUDIT_BATCH_SECONDS, 'absolute_total_seconds': AUDIT_TOTAL_SECONDS,
        'absolute_row_span': AUDIT_MAX_ROW_SPAN, 'fixture_works': fixture_works, 'fixture_history': fixture_history}
    if not 1 <= batch_rows <= AUDIT_BATCH_ROWS or not 0 < total_seconds <= AUDIT_TOTAL_SECONDS:
        raise AuditFailure('audit limits were invalid/expanded', evidence)
    if any(path.with_name(path.name+suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
        raise AuditFailure('closed audit source still has sidecars', evidence)
    started = clock()
    totals = {'verified_assets': 0, 'verified_previews': 0, 'bookmark_observations': 0, 'publication_journals': 0}
    states = defaultdict(Counter)
    source_before = (path.stat().st_size, path.stat().st_mtime_ns)
    connection = sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)
    try:
        total_deadline = started+total_seconds
        with bounded_read(connection, evidence, total_deadline=total_deadline, clock=clock):
            for table in TABLE_COLUMNS:
                shape = connection.execute("SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone()
                if not shape or 'WITHOUT ROWID' in shape[0].upper():
                    raise AuditFailure('audit source is not the known ordinary rowid schema', evidence)
                low = connection.execute(f'SELECT MIN(rowid) FROM "{table}"').fetchone()[0]
                high = connection.execute(f'SELECT MAX(rowid) FROM "{table}"').fetchone()[0] or 0
                if low is not None and low <= 0:
                    raise AuditFailure('audit encountered an unsupported nonpositive rowid', evidence)
                evidence['tables'][table] = {'max_rowid': high, 'cursor': 0, 'rows': 0, 'batches': 0, 'complete': False}
        span = sum(value['max_rowid'] for value in evidence['tables'].values())
        allowed_span = min(AUDIT_MAX_ROW_SPAN, 4*(fixture_history+16*fixture_works))
        if span > allowed_span:
            raise AuditFailure('audit rowid span exceeds fixed fixture/absolute bound', evidence)
        max_batches = sum(math.ceil(value['max_rowid']/batch_rows) for value in evidence['tables'].values())
        evidence.update(row_span=span, allowed_row_span=allowed_span, max_batches=max_batches)
        total_deadline = min(total_deadline, started+(max_batches+2)*AUDIT_BATCH_SECONDS)
        evidence['derived_total_seconds'] = total_deadline-started
        for table, progress in evidence['tables'].items():
            while progress['cursor'] < progress['max_rowid']:
                low = progress['cursor']
                high = min(low+batch_rows, progress['max_rowid'])
                with bounded_read(connection, evidence, total_deadline=total_deadline, clock=clock):
                    rows = fetch_audit_batch(connection, table, low, high)
                    expected = connection.execute(f'SELECT count(*) FROM "{table}" WHERE rowid>? AND rowid<=?', (low, high)).fetchone()[0]
                    ids = [row[0] for row in rows]
                    if len(rows) != expected or len(rows) > batch_rows or ids != sorted(set(ids)) or any(not low < ident <= high for ident in ids):
                        raise AuditFailure('audit batch omitted/repeated/out-of-range rows', evidence)
                    for row in rows:
                        if table == 'operation_items':
                            states[row[1]][row[2]] += 1
                        elif table == 'assets':
                            totals['verified_assets'] += row[1] == 'verified'
                        elif table == 'derivatives':
                            totals['verified_previews'] += row[1] == 'verified'
                        elif table == 'remote_bookmarks':
                            totals['bookmark_observations'] += row[1] is not None and utc(row[1]) >= utc(started_wall)
                        else:
                            totals['publication_journals'] += 1
                    if high <= low or progress['batches'] >= math.ceil(progress['max_rowid']/batch_rows):
                        raise AuditFailure('audit cursor/batch coverage was invalid', evidence)
                    progress.update(cursor=high, rows=progress['rows']+len(rows), batches=progress['batches']+1)
            progress['complete'] = progress['cursor'] == progress['max_rowid']
        evidence['production_persistence'] = totals
        evidence['maintenance_item_counts'] = dict(states[operation_id])
        with bounded_read(connection, evidence, total_deadline=total_deadline, clock=clock):
            row = connection.execute('SELECT state,result,finished_at FROM operations WHERE id=?', (operation_id,)).fetchone()
            if row is None:
                raise AuditFailure('audited maintenance operation is absent', evidence)
            state, payload, finished_at = row[0], json.loads(row[1]), row[2]
            evidence['maintenance_operation'] = {'state': state, 'result': payload, 'finished_at': finished_at}
            # The full-library reconcile fixture is one operation. Fail closed
            # if that contract changes instead of ignoring child work/results.
            if payload.get('child_operation_ids'):
                raise AuditFailure('maintenance audit requires an explicit child-operation contract', evidence)
            if require_complete or completed_report is not None:
                actual = dict(states[operation_id])
                if state != 'finished' or payload.get('outcome') != 'succeeded' or any(count for key, count in actual.items() if key not in SETTLED):
                    raise AuditFailure('durable maintenance is unfinished/unsuccessful', evidence)
                if actual.get('failed', 0) or actual.get('not_executed', 0):
                    raise AuditFailure('durable item facts contradict succeeded outcome', evidence)
                if sum(actual.values()) < fixture_works:
                    raise AuditFailure('durable maintenance did not cover the fixture work minimum', evidence)
                clean = lambda values: {key: count for key, count in values.items() if count}
                if not isinstance(payload.get('counts'), dict) or clean(payload['counts']) != clean(actual):
                    raise AuditFailure('durable operation counts differ from independent item facts', evidence)
                if completed_report is None or not completed_report['completion_observed_in_window']:
                    raise AuditFailure('maintenance was not observed complete within the workload window', evidence)
                if clean(completed_report['counts']) != clean(actual) or utc(completed_report['finished_at']) != utc(finished_at):
                    raise AuditFailure('completed HTTP report differs from durable maintenance facts', evidence)
        if any(not value['complete'] for value in evidence['tables'].values()):
            raise AuditFailure('closed audit did not cover every table', evidence)
        if source_before != (path.stat().st_size, path.stat().st_mtime_ns):
            raise AuditFailure('closed audit source changed', evidence)
        evidence['status'] = 'complete'
        return evidence
    except AuditFailure:
        evidence['status'] = 'failed'
        raise
    except Exception as error:
        evidence['status'] = 'failed'
        raise AuditFailure('closed audit failed: '+type(error).__name__, evidence) from error
    finally:
        connection.close()
        evidence['elapsed_seconds'] = clock()-started
