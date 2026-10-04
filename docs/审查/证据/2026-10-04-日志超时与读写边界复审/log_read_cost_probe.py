"""Diagnostic only: synthetic in-memory SQLite log reads, no application edits.

Files in this run are retained as evidence, never cleaned automatically.
Run from the repository root with its isolated Python runtime. JSON output is
exclusive-create, so each evidence filename must be new. No real configuration,
account, database, images, or environment files are read.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import inspect
import json
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "app"))
from sqlalchemy import and_, create_engine, event, insert, or_, select, union_all
from sqlalchemy.orm import load_only, sessionmaker
from src.core import activity_log
from src.core.models import Base, Operation, OperationItem
from src.core.task_indexes import create_task_indexes


class ProbeCursor(sqlite3.Cursor):
    def fetchall(self):
        rows = super().fetchall()
        active = self.connection.probe_active
        if active is not None:
            active["fetched_rows"] += len(rows)
            active["fetched_text_bytes"] += sum(len(v.encode("utf-8")) for r in rows for v in r if isinstance(v, str))
        return rows


class ProbeConnection(sqlite3.Connection):
    probe_active = None
    def cursor(self, *args, **kwargs):
        return super().cursor(factory=ProbeCursor)


class SyntheticDatabase:
    def __init__(self):
        self.engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(":memory:", factory=ProbeConnection))
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            create_task_indexes(connection)
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def read(self):
        with self.session() as session:
            session.connection().exec_driver_sql("BEGIN")
            yield session

    @contextmanager
    def write(self):
        with self.session() as session:
            yield session
            session.commit()


@contextmanager
def probe(database):
    records, active = [], {}
    def finish(driver):
        record = active.pop(driver, None)
        if record is not None:
            record["elapsed_ms"] = round((time.perf_counter() - record.pop("started"))*1000, 3)
            driver.set_progress_handler(None, 0)
            driver.probe_active = None
    def before(connection, cursor, statement, parameters, context, many):
        driver = connection.connection.driver_connection
        finish(driver)
        if not statement.lstrip().upper().startswith("SELECT"):
            return
        driver.probe_active = None
        cursor.execute("EXPLAIN QUERY PLAN " + statement, parameters)
        plan = [list(row) for row in cursor.fetchall()]
        record = dict(sql=statement, parameters=parameters, plan=plan, vm_steps=0,
                      fetched_rows=0, fetched_text_bytes=0, started=time.perf_counter())
        records.append(record)
        active[driver] = record
        driver.probe_active = record
        def progress():
            record["vm_steps"] += 100
            return 0
        driver.set_progress_handler(progress, 100)
    event.listen(database.engine, "before_cursor_execute", before)
    try:
        yield records
    finally:
        for driver in list(active):
            finish(driver)
        event.remove(database.engine, "before_cursor_execute", before)


def candidate_failures(session, operations):
    """Split mutually exclusive indexed states; project only log fields.

    Same global operation_id/position order and 201-row truncation witness as
    production. Existing indexes only; JSON is read only for skipped records.
    This deliberately does not promise bounded work for a huge failed/skipped
    population: the state indexes contain lane before position.
    """
    ids = [op.id for op in operations]
    cols = (OperationItem.operation_id, OperationItem.position, OperationItem.state, OperationItem.result)
    branches = [select(*cols).where(OperationItem.operation_id.in_(ids), OperationItem.state.in_(("failed", "not_executed"))),
        select(*cols).where(OperationItem.operation_id.in_(ids), OperationItem.state == "skipped",
            OperationItem.result["failure_code"].as_string().in_(("work_unavailable", "work_private")))]
    combined = union_all(*branches).subquery()
    stmt = select(combined).order_by(combined.c.operation_id, combined.c.position).limit(201)
    return [SimpleNamespace(**dict(row._mapping)) for row in session.execute(stmt)]


def candidates():
    source = inspect.getsource(activity_log.processing_log)
    start = source.index("        failures = list(")
    end = source.index("        by_operation =", start)
    narrow_source = source.replace("select(Operation).order_by(",
        "select(Operation).options(load_only(Operation.kind, Operation.state, Operation.phase, Operation.phase_unit, Operation.phase_total, Operation.phase_done, Operation.result, Operation.needs_attention, Operation.updated_at, Operation.finished_at)).order_by(")
    narrow_source = narrow_source.replace("select(OperationItem).where(",
        "select(OperationItem).options(load_only(OperationItem.operation_id, OperationItem.position, OperationItem.state, OperationItem.result)).where(")
    split_source = source[:start] + "        failures = candidate_failures(session, operations)\n" + source[end:]
    split_source = split_source.replace("select(Operation).order_by(",
        "select(Operation).options(load_only(Operation.kind, Operation.state, Operation.phase, Operation.phase_unit, Operation.phase_total, Operation.phase_done, Operation.result, Operation.needs_attention, Operation.updated_at, Operation.finished_at)).order_by(")
    funcs = {}
    for name, text in (("narrow_same_or", narrow_source), ("split_narrow", split_source)):
        namespace = {**activity_log.__dict__, "load_only": load_only, "candidate_failures": candidate_failures}
        exec(compile(text, __file__ + ":" + name, "exec"), namespace)
        funcs[name] = namespace["processing_log"]
    return funcs


def simple_seed(database, rows, width, mode):
    with database.write() as session:
        op = Operation(id="a0000000-0000-0000-0000-000000000000", kind="verify", dedupe_key="synthetic",
            label="synthetic", state="running", parameters={"synthetic": "p"*65536},
            phase="verifying", phase_unit="assets", phase_done=rows//2, phase_total=rows)
        session.add(op)
        session.flush()
        for first in range(0, rows, 1000):
            batch = []
            for i in range(first, min(first+1000, rows)):
                if mode == "mostly_pending":
                    state = "failed" if i % 1000 == 997 else "pending" if i % 2 else "succeeded"
                elif mode == "all_failed":
                    state = "failed"
                else:
                    state = "skipped"
                batch.append(dict(id=f"item-{i}", operation_id=op.id, item_key=str(i), position=i,
                    kind="synthetic", target={"synthetic": "x"*width}, phase="verifying", lane=None,
                    depends_on=[f"synthetic-dependency-{i}"], state=state,
                    result={"code": "ReadTimeout"} if state == "failed" else {}))
            session.execute(insert(OperationItem), batch)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--width", type=int, default=0)
    parser.add_argument("--mode", choices=("mostly_pending", "all_failed", "all_skipped", "existing_shape"), default="mostly_pending")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    database = SyntheticDatabase()
    started = time.perf_counter()
    if args.mode == "existing_shape":
        sys.path.insert(0, str(ROOT / "app" / "tests"))
        from test_task_claim_completed_dependencies import seed_maintenance_shape
        seed_maintenance_shape(database)
        args.rows = 150512
    else:
        simple_seed(database, args.rows, args.width, args.mode)
    seed_ms = (time.perf_counter()-started)*1000
    print(json.dumps({"seed_ready": True, "rows": args.rows, "seed_ms": round(seed_ms, 2)}), flush=True)
    evidence = {"synthetic_only": True, "sqlite_version": sqlite3.sqlite_version,
        "rows": args.rows, "target_padding_bytes": args.width, "mode": args.mode, "seed_ms": seed_ms, "runs": []}
    methods = {"production": activity_log.processing_log, **candidates()}
    baseline = None
    # Warm every statement before fair timing; report VM work through fetchall.
    for function in methods.values():
        function(database)
    order = list(methods.items()) + list(reversed(list(methods.items())))
    for round_index, (name, function) in enumerate(order):
        with probe(database) as queries:
            started = time.perf_counter()
            result = function(database)
            elapsed_ms = (time.perf_counter()-started)*1000
        if baseline is None:
            baseline = result
        run = dict(name=name, round_index=round_index, elapsed_ms=round(elapsed_ms,3), same_result=result == baseline,
            output_lines=len(result["lines"]), truncated=result["truncated"], queries=queries,
            total_vm_steps=sum(q["vm_steps"] for q in queries), total_fetched_rows=sum(q["fetched_rows"] for q in queries),
            total_fetched_text_bytes=sum(q["fetched_text_bytes"] for q in queries))
        evidence["runs"].append(run)
        print(json.dumps({k:v for k,v in run.items() if k != "queries"}), flush=True)
        assert run["same_result"]
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=2)
    database.engine.dispose()


if __name__ == "__main__":
    main()
