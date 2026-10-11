"""Memory-only progress gate budgets; no real database, files or account."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
import psutil
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.models import Base, Operation, OperationItem
from src.core.tasks import TaskQueue
from projection_fixture import prepare_synthetic_projection


@contextmanager
def isolated_memory_database():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    prepare_synthetic_projection(engine)
    database = SimpleNamespace(engine=engine, read=sessionmaker(engine, expire_on_commit=False))
    try:
        yield database
    finally:
        engine.dispose()


@pytest.fixture
def memory_database():
    with isolated_memory_database() as database:
        yield database


@contextmanager
def remote_query_probe(database):
    evidence, active = [], []

    def finish():
        if active:
            driver, record = active.pop()
            driver.set_progress_handler(None, 0)
            record["elapsed_ms"] = (time.perf_counter() - record.pop("started")) * 1000

    def before(connection, cursor, statement, parameters, context, many):
        finish()
        if "operation_items.kind IN" not in statement and "ix_task_item_remote_inventory" not in statement:
            return
        cursor.execute("EXPLAIN QUERY PLAN " + statement, parameters)
        record = {"sql": statement, "parameters": parameters,
            "plan": [list(row) for row in cursor.fetchall()], "vm_steps": 0, "started": time.perf_counter()}
        evidence.append(record)
        driver = connection.connection.driver_connection
        active.append((driver, record))

        def progress():
            record["vm_steps"] += 100
            return 0
        driver.set_progress_handler(progress, 100)

    event.listen(database.engine, "before_cursor_execute", before)
    try:
        yield evidence
    finally:
        finish()
        event.remove(database.engine, "before_cursor_execute", before)


def operation(database, kind="reconcile"):
    with database.read() as session:
        session.add(Operation(id="progress-op", kind=kind, dedupe_key="synthetic-progress", label="synthetic",
            state="running", phase="verifying", phase_unit="assets", phase_done=12, phase_total=100000,
            result={"planning_complete": True}))
        session.commit()
    prepare_synthetic_projection(database.engine, install=False)
    return TaskQueue(database, {})


def remote_gate_memory_budget(database, evidence_path):
    queue = operation(database)
    # Generate the large target payload and all indexes solely in SQLite RAM.
    with database.engine.begin() as connection:
        connection.execute(text("""WITH RECURSIVE rows(i) AS (
            SELECT 0 UNION ALL SELECT i+1 FROM rows WHERE i<99999)
            INSERT INTO operation_items(id,operation_id,item_key,position,kind,target,phase,unit,depends_on,state,lane,attempts,result)
            SELECT 'unrelated-'||i,'progress-op','unrelated-'||i,i,'reconcile_asset',
                '{"asset_id":"synthetic","padding":"' || hex(zeroblob(128)) || '"}',
                CASE WHEN i%3=0 THEN 'reconciling' WHEN i%3=1 THEN 'verifying' ELSE 'checking' END,
                CASE WHEN i%3=2 THEN 'works' ELSE 'assets' END,'[]','pending',NULL,0,'{}' FROM rows"""))
    prepare_synthetic_projection(database.engine, install=False)
    with database.read() as session:
        with remote_query_probe(database) as baseline:
            assert session.scalar(select(OperationItem.id).where(OperationItem.operation_id == "progress-op",
                OperationItem.kind.in_(("discover_page", "bookmark_page", "bookmark_artist")),
                OperationItem.state.in_(("pending", "running"))).limit(1)) is None
        with remote_query_probe(database) as candidate:
            view = queue._operation_view(session.get(Operation, "progress-op"), session)
    assert view["phase"]["total"] == 100000
    assert len(baseline) == len(candidate) == 1
    memory = psutil.Process().memory_info()
    peak_bytes = getattr(memory, "peak_wset", memory.rss)
    evidence = {"memory_only_rows": 100000, "process_id": os.getpid(), "peak_process_mb": peak_bytes / 1024 / 1024,
        "baseline": baseline, "candidate": candidate}
    evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps({"baseline_vm": baseline[0]["vm_steps"], "candidate_vm": candidate[0]["vm_steps"],
        "peak_process_mb": evidence["peak_process_mb"]}))
    assert peak_bytes < 300 * 1024 * 1024
    assert baseline[0]["vm_steps"] > 500000
    assert candidate[0]["vm_steps"] < 5000
    assert any("ix_task_item_remote_inventory (operation_id=?)" in row[-1] for row in candidate[0]["plan"])


def test_progress_remote_gate_does_not_visit_unrelated_pending_targets(tmp_path):
    # peak_wset measures the process's lifetime peak, including earlier pytest
    # fixtures. Run the unchanged 100k RAM query and budgets in a fresh process.
    evidence_path = tmp_path / "remote-page-query.json"
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--memory-budget", str(evidence_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["process_id"] != os.getpid()
    assert evidence["memory_only_rows"] == 100000
    assert evidence["peak_process_mb"] < 300
    print(result.stdout.strip())


@pytest.mark.parametrize("kind,phase,unit,state,op_kind,unknown_total", [
    ("discover_page", "checking", "pages", "pending", "check", True),
    ("bookmark_page", "bookmarks", "pages", "pending", "bookmarks", True),
    ("bookmark_artist", "bookmarks", "artists", "pending", "reconcile", True),
    ("bookmark_artist", "bookmarks", "artists", "running", "reconcile", True),
    ("bookmark_artist", "bookmarks", "artists", "succeeded", "reconcile", False),
    ("bookmark_page", "bookmarks", "pages", "failed", "bookmarks", False),
    ("metadata_artist", "checking", "artists", "pending", "reconcile", False),
])
def test_progress_remote_gate_preserves_new_and_legacy_unknown_denominators(
        memory_database, kind, phase, unit, state, op_kind, unknown_total):
    database = memory_database
    queue = operation(database, op_kind)
    with database.read() as session:
        session.add(OperationItem(id="remote", operation_id="progress-op", item_key="arbitrary-legacy-key",
            position=0, kind=kind, phase=phase, unit=unit, state=state, lane=None, target={}, depends_on=[], result={}))
        session.commit()
    prepare_synthetic_projection(database.engine, install=False)
    with database.read() as session:
        view = queue._operation_view(session.get(Operation, "progress-op"), session)
    assert view["phase"]["total"] == (None if unknown_total else 100000)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--memory-budget":
        raise SystemExit("Expected --memory-budget EVIDENCE_PATH")
    with isolated_memory_database() as database:
        remote_gate_memory_budget(database, Path(sys.argv[2]))
