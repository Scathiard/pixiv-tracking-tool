"""Bounded counterexamples for the reliability instrument, with no product imports."""
from datetime import datetime, timezone
import io
import json
import sqlite3
import threading
import time
from types import SimpleNamespace as NS

import pytest

import parallel_reliability_observation as observation
from parallel_reliability_probe import ActivityCoverage, Telemetry

STAMP = datetime(2026, 10, 8, tzinfo=timezone.utc)
FINISHED = '2026-10-08T00:00:01+00:00'


def fixture_db(tmp_path, *, states=('succeeded', 'skipped'), operation_state='finished', counts=None):
    path = tmp_path/'archive.db'
    counts = counts if counts is not None else dict(__import__('collections').Counter(states))
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE operation_items(operation_id TEXT,state TEXT);
            CREATE TABLE assets(state TEXT); CREATE TABLE derivatives(state TEXT);
            CREATE TABLE remote_bookmarks(confirmed_at TEXT); CREATE TABLE publication_journal(id TEXT);
            CREATE TABLE operations(id TEXT PRIMARY KEY,state TEXT,result TEXT,finished_at TEXT);''')
        db.executemany('INSERT INTO operation_items VALUES(?,?)', [('maintenance', value) for value in states])
        db.executemany('INSERT INTO assets VALUES(?)', [('verified',), ('missing',), ('verified',)])
        db.executemany('INSERT INTO derivatives VALUES(?)', [('verified',), ('missing',)])
        db.executemany('INSERT INTO remote_bookmarks VALUES(?)', [(FINISHED,), (None,), ('2026-10-07T00:00:00',)])
        db.execute('INSERT INTO publication_journal VALUES(?)', ('journal',))
        db.execute('INSERT INTO operations VALUES(?,?,?,?)', ('maintenance', operation_state,
            json.dumps({'outcome': 'succeeded', 'counts': counts}), FINISHED))
    return path, {'counts': counts, 'finished_at': FINISHED, 'completion_observed_in_window': True}


def audit(path, report, **kwargs):
    return observation.audit_persistence(path, 'maintenance', STAMP, report,
        require_complete=True, fixture_works=2, fixture_history=10, **kwargs)


def test_closed_audit_covers_five_tables_and_original_four_summaries(tmp_path):
    path, report = fixture_db(tmp_path)
    result = audit(path, report, batch_rows=1)
    assert result['production_persistence'] == {
        'verified_assets': 2, 'verified_previews': 1, 'bookmark_observations': 1, 'publication_journals': 1}
    assert result['maintenance_item_counts'] == {'succeeded': 1, 'skipped': 1}
    assert all(value['complete'] and value['cursor'] == value['max_rowid'] for value in result['tables'].values())
    assert sum(value['rows'] for value in result['tables'].values()) == 11
    assert result['max_batches'] == 11 and result['derived_total_seconds'] <= 300


@pytest.mark.parametrize('states', [('succeeded', 'pending'), ('succeeded', 'failed'), ('succeeded', 'not_executed')])
def test_false_finished_http_and_operation_cannot_hide_durable_unfinished_or_failed_items(tmp_path, states):
    path, report = fixture_db(tmp_path, states=states)
    with pytest.raises(observation.AuditFailure, match='unfinished|contradict'):
        audit(path, report)


def test_only_finishing_at_close_cannot_supply_missing_window_report(tmp_path):
    path, _ = fixture_db(tmp_path)
    with pytest.raises(observation.AuditFailure, match='within the workload window'):
        audit(path, None)


@pytest.mark.parametrize('target', ['http', 'operation'])
def test_independent_item_counts_must_match_both_http_and_operation(tmp_path, target):
    path, report = fixture_db(tmp_path, counts={'succeeded': 9} if target == 'operation' else None)
    if target == 'http':
        report['counts'] = {'succeeded': 9}
    with pytest.raises(observation.AuditFailure, match='counts differ|report differs'):
        audit(path, report)


@pytest.mark.parametrize('mode', ['omitted', 'repeated', 'outside'])
def test_batch_coverage_detects_missing_duplicate_and_invalid_cursor_rows(tmp_path, monkeypatch, mode):
    path, report = fixture_db(tmp_path)
    original = observation.fetch_audit_batch
    def corrupted(connection, table, low, high):
        rows = original(connection, table, low, high)
        if table == 'assets':
            if mode == 'omitted': return rows[:-1]
            if mode == 'repeated': return [rows[0]]*len(rows)
            return [(high+1, *rows[0][1:]), *rows[1:]]
        return rows
    monkeypatch.setattr(observation, 'fetch_audit_batch', corrupted)
    with pytest.raises(observation.AuditFailure, match='omitted/repeated/out-of-range') as failure:
        audit(path, report)
    assert failure.value.evidence['tables']['assets']['complete'] is False


def test_batch_budget_interrupt_after_successful_sql_still_fails(tmp_path, monkeypatch):
    path, report = fixture_db(tmp_path)
    now = [0.0]
    original = observation.fetch_audit_batch
    def delayed(*args):
        rows = original(*args)
        now[0] += 4.6
        return rows
    monkeypatch.setattr(observation, 'fetch_audit_batch', delayed)
    with pytest.raises(observation.AuditFailure, match='deadline'):
        audit(path, report, clock=lambda: now[0])


def test_many_fast_batches_cannot_expand_total_audit_deadline(tmp_path, monkeypatch):
    path, report = fixture_db(tmp_path)
    now = [0.0]
    original = observation.fetch_audit_batch
    def delayed(*args):
        rows = original(*args)
        now[0] += .6
        return rows
    monkeypatch.setattr(observation, 'fetch_audit_batch', delayed)
    with pytest.raises(observation.AuditFailure, match='deadline'):
        audit(path, report, batch_rows=1, total_seconds=1, clock=lambda: now[0])


@pytest.mark.parametrize('rowid', [0, -1, 2_000_001])
def test_nonpositive_rowids_and_huge_sparse_high_water_fail_closed(tmp_path, rowid):
    path, report = fixture_db(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO assets(rowid,state) VALUES(?,?)', (rowid, 'verified'))
    with pytest.raises(observation.AuditFailure, match='rowid|span'):
        audit(path, report)


def test_child_operation_production_contract_cannot_be_silently_ignored(tmp_path):
    path, report = fixture_db(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE operations SET result=?', (json.dumps({'child_operation_ids': ['child']}),))
    with pytest.raises(observation.AuditFailure, match='child-operation contract'):
        audit(path, report)


def test_sidecars_block_immutable_audit(tmp_path):
    path, report = fixture_db(tmp_path)
    path.with_name(path.name+'-wal').write_bytes(b'synthetic')
    with pytest.raises(observation.AuditFailure, match='sidecars'):
        audit(path, report)


class Response:
    def __init__(self, value, status=200):
        self.content, self.status_code = json.dumps(value).encode(), status
    def __enter__(self): return self
    def __exit__(self, *values): return False


def observe(tmp_path, monkeypatch, value=None, *, status=200, end=1.0, error=None):
    telemetry = Telemetry(tmp_path)
    coverage = ActivityCoverage(0, 0)
    reports = observation.MaintenanceReports()
    ticks = iter([0, end, end, end])
    monkeypatch.setattr(observation.time, 'monotonic', lambda: next(ticks, end))
    def request(url, **kwargs):
        assert url == 'http://127.0.0.1/api/tasks/maintenance?limit=1'
        assert kwargs == {'timeout': (5, 5)}
        if error: raise error
        return Response(value, status)
    result = observation.observe_maintenance(request, 'http://127.0.0.1', 'maintenance',
        began=0, deadline=10, wall_deadline='2026-10-08T00:00:10+00:00',
        reports=reports, telemetry=telemetry, coverage=coverage)
    failures = list(telemetry.failures)
    telemetry.close()
    return result, reports.snapshot(), coverage, failures


def body(**values):
    return dict(id='maintenance', state='finished', counts={'succeeded': 2},
                result={'outcome': 'succeeded'}, finished_at=FINISHED, **values)


@pytest.mark.parametrize('fault', ['non200', 'missingcounts', 'negative', 'wrongid', 'invalidbody', 'timeout', 'slow'])
def test_actual_task_http_failures_are_recorded_and_never_cover_maintenance(tmp_path, monkeypatch, fault):
    value, kwargs = body(), {}
    if fault == 'non200': kwargs['status'] = 503
    if fault == 'missingcounts': value.pop('counts')
    if fault == 'negative': value['counts'] = {'succeeded': -1}
    if fault == 'wrongid': value['id'] = 'other'
    if fault == 'invalidbody': value = []
    if fault == 'timeout': kwargs['error'] = TimeoutError('temporary synthetic transport fault')
    if fault == 'slow': kwargs['end'] = 5.1
    result, (_, completed), coverage, failures = observe(tmp_path, monkeypatch, value, **kwargs)
    assert result['ok'] is False and failures and completed is None
    assert coverage.counts['maintenance'] == 0
    assert 'maintenance_observation' in (tmp_path/'http.jsonl').read_text()


def test_non_json_error_response_retains_actual_status_and_size(tmp_path, monkeypatch):
    original = Response.__init__
    def non_json(self, value, status):
        original(self, value, status)
        self.content = b'synthetic service unavailable'
    monkeypatch.setattr(Response, '__init__', non_json)
    result, (_, completed), coverage, failures = observe(tmp_path, monkeypatch, body(), status=503)
    assert result['ok'] is False and result['status'] == 503
    assert result['bytes'] == len(b'synthetic service unavailable')
    assert failures and completed is None and coverage.counts['maintenance'] == 0


@pytest.mark.parametrize('end,finished', [(11, FINISHED), (1, '2026-10-08T00:00:11+00:00')])
def test_completion_response_and_actual_finish_must_both_be_in_window(tmp_path, monkeypatch, end, finished):
    value = body()
    value['finished_at'] = finished
    # end=11 also violates 5s and is correctly a hard HTTP failure.
    _, (_, completed), _, _ = observe(tmp_path, monkeypatch, value, end=end)
    assert completed is None


def test_http_precise_counts_and_settled_are_retained(tmp_path, monkeypatch):
    value = body()
    value['counts'] = {'succeeded': 4, 'skipped': 3}
    result, (reports, completed), coverage, failures = observe(tmp_path, monkeypatch, value)
    assert result['ok'] and not failures and coverage.counts['maintenance'] == 1
    assert completed['counts'] == value['counts'] and completed['settled'] == 7
    assert reports == [completed]


@pytest.mark.parametrize('counts', [{}, {'succeeded': 1, 'failed': 1}, {'succeeded': 1, 'not_executed': 1}])
def test_claimed_http_success_must_match_successful_item_semantics(tmp_path, monkeypatch, counts):
    value = body()
    value['counts'] = counts
    result, (_, completed), coverage, failures = observe(tmp_path, monkeypatch, value)
    assert result['ok'] is False and failures and completed is None
    assert coverage.counts['maintenance'] == 0


def test_response_during_close_cannot_rescue_early_workload_failure():
    report = {'completion_observed_in_window': True, 'elapsed_seconds': 9, 'finished_at': FINISHED}
    assert observation.completed_in_window([report], 0, 8, '2026-10-08T00:00:08Z') is None
    report = {'completion_observed_in_window': True, 'elapsed_seconds': 7, 'finished_at': '2026-10-08T00:00:09Z'}
    assert observation.completed_in_window([report], 0, 8, '2026-10-08T00:00:08Z') is None


def test_resource_sampling_does_not_take_db_read_and_survives_other_observer_fault(tmp_path):
    def prohibited(): raise AssertionError('resource observer must not read SQLite')
    database = NS(path=tmp_path/'archive.db', read=prohibited, reads=NS(active={'ui': 0}, pending={'ui': 0}),
        engine=NS(pool=NS(checkedout=lambda: 0)), writer_engine=NS(pool=NS(checkedout=lambda: 0)))
    runtime = NS(database=database, queue=NS(worker_error=None),
        wal_maintenance=NS(snapshot=lambda: {'running': True, 'error_count': 0}))
    process = NS(cpu_times=lambda: NS(user=1, system=2), memory_info=lambda: NS(rss=123), cpu_percent=lambda **kwargs: 0)
    samples, failures = [], []
    resource_seen = threading.Event()
    failed = threading.Event()
    began = time.monotonic()
    def resource():
        samples.append(observation.resource_sample(runtime, process, began))
        resource_seen.set()
    def fault(): raise RuntimeError('temporary maintenance failure')
    def failure(error): failures.append(type(error).__name__); failed.set()
    resources = observation.PeriodicObserver(resource, failure, began=began, deadline=began+1, interval=.02).start()
    maintenance = observation.PeriodicObserver(fault, failure, began=began, deadline=began+1, interval=.02).start()
    try:
        assert failed.wait(.5) and resource_seen.wait(.5)
        assert samples[0]['rss_bytes'] == 123 and failures
    finally:
        assert resources.stop() and maintenance.stop()
    # Neither the observation failure nor missing DB summary suppresses gates.
    args = NS(pilot_seconds=None, duration_hours=8)
    result = {'elapsed_seconds': 1, 'maintenance_completed': False, 'overlap': {},
              'activity_coverage': {'violations': ['sample coverage 1 < 2880', 'maintenance coverage 0 < 2880']}}
    observation.enforce_soak_gates(result, args, failures.append)
    assert any('duration' in failure for failure in failures)
    assert any('full 15000' in failure for failure in failures)
    assert sum('overlap' in failure for failure in failures) == 2
    assert sum('coverage' in failure for failure in failures) == 2


def test_resource_coverage_cannot_substitute_for_task_http_or_expand_original_gaps():
    args = NS(pilot_seconds=None, duration_hours=8, burst_seconds=45)
    coverage = ActivityCoverage(0, 0)
    for stamp in range(0, 28801, 5):
        coverage.observe('sample', stamp)
        coverage.heartbeat(stamp, stamp)
    for stamp in range(0, 28801, 45): coverage.observe('burst', stamp)
    result = coverage.finish(28800, 28800, args)
    assert result['gap_limits_seconds']['sample'] == 30
    assert result['gap_limits_seconds']['maintenance'] == 30
    assert not any('sample ' in failure for failure in result['violations'])
    assert any('maintenance coverage' in failure for failure in result['violations'])
    assert any('maintenance gap' in failure for failure in result['violations'])
