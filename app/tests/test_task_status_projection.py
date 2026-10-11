"""Synthetic atomic task summaries and complete bounded status/history reads."""
from contextlib import contextmanager
from datetime import timedelta
import json

import pytest
from sqlalchemy import event, insert, select, update

from src.core.database import ArchiveDatabase
from src.core.models import Operation, OperationItem, TaskRound, utcnow
from src.core.projection_maintenance import rebuild_projections
from src.core.read_projection import ProjectionUnavailable
from src.core import task_read_projection as task_reads
from src.core.tasks import TaskQueue


def add_operation(session, ident, *, state="ready", round_id=None, source="manual", attention=False, finished_at=None):
    operation = Operation(id=ident, kind="reconcile", label=ident, dedupe_key=ident,
        state=state, source=source, round_id=round_id, needs_attention=attention,
        finished_at=finished_at, result={"planning_complete": True})
    session.add(operation)
    session.flush()
    return operation


def item_row(ident, operation_id, *, state="pending", phase="inventory", unit="items", position=0):
    return dict(id=ident, operation_id=operation_id, item_key=ident, kind="checked",
        target={}, phase=phase, unit=unit, position=position, state=state,
        depends_on=[], result={})


def assert_counters(session):
    session.flush()
    task_reads.verify_task_read_projection(session.connection())


def test_task_counters_follow_source_transaction_round_moves_and_item_identity(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "task-status.db")
    try:
        with database.write() as session:
            session.add_all([TaskRound(id="old"), TaskRound(id="new")])
            session.flush()
            op = add_operation(session, "one", round_id="old")
            add_operation(session, "two", round_id="new", state="queued", attention=True)
            session.execute(insert(OperationItem), [item_row("item-" + str(i), "one", state=state,
                phase="inventory" if i % 2 else "", unit="items", position=i)
                for i, state in enumerate(("succeeded", "failed", "skipped", "not_executed", "pending", "future\"[state]", ""))])
            assert_counters(session)
            assert task_reads.status_counts(session) == {"active": 2, "queued": 2, "waiting": 0, "attention": 1}
            assert task_reads.operation_counts(session, "one")["future\"[state]"] == 1
            op.round_id = "new"
            assert_counters(session)
            assert task_reads.round_counts(session, "old") == {}
            op.round_id = None
            op.state, op.needs_attention = "queued", True
            assert_counters(session)
            assert task_reads.status_counts(session)["waiting"] == 1
            stored = session.get(OperationItem, "item-5")
            stored.operation_id, stored.state, stored.phase, stored.unit = "two", "running", "checking", "pages"
            assert_counters(session)
            assert task_reads.operation_counts(session, "two") == {"running": 1}
            assert task_reads.phase_counts(session, "two", "checking", "pages") == {"running": 1}
            assert task_reads.round_counts(session, "new") == {"running": 1}
            session.delete(stored)
            assert_counters(session)
            assert task_reads.operation_counts(session, "two") == {}
            assert task_reads.round_counts(session, "new") == {}
        with database.read() as session:
            assert_counters(session)
        # A source failure rolls back both the durable item and its counters.
        with pytest.raises(RuntimeError, match="injected"):
            with database.write() as session:
                session.get(OperationItem, "item-4").state = "succeeded"
                session.flush()
                assert task_reads.operation_counts(session, "one")["succeeded"] == 2
                raise RuntimeError("injected")
        with database.read() as session:
            assert task_reads.operation_counts(session, "one")["succeeded"] == 1
            assert_counters(session)
        # External SQL may update immediate counts, but cannot bless the
        # read model. Ordinary status reads still reject the dirty database.
        with database.writer_engine.begin() as connection:
            connection.exec_driver_sql("UPDATE task_rounds SET state='finished' WHERE id='new'")
        with pytest.raises(ProjectionUnavailable):
            with database.read():
                pass
        with database.writer_session() as session, session.begin():
            rebuild_projections(session)
            assert_counters(session)
        with database.read() as session:
            assert_counters(session)
    finally:
        database.close()


def test_complete_task_reads_preserve_priority_history_null_dates_and_unknown_states(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "task-contract.db")
    now = utcnow().replace(microsecond=0)
    try:
        with database.write() as session:
            session.add(TaskRound(id="frozen", state="running", frozen_at=now))
            session.flush()
            for ident, source in (("manual", "manual"), ("auto", "automatic"), ("maint", "maintenance")):
                add_operation(session, ident, state="running", round_id="frozen", source=source)
            add_operation(session, "next", state="queued")
            for ident, attention, finished_at in (("done-c", True, now), ("done-b", False, now),
                    ("done-a", True, now-timedelta(seconds=1)), ("done-null", False, None)):
                add_operation(session, ident, state="finished", attention=attention, finished_at=finished_at)
            session.execute(insert(OperationItem), [item_row("step-"+str(i), "manual", state=state, position=i)
                for i, state in enumerate(("succeeded", "failed", "not_executed", "future-v2", "pending"))])
            session.execute(insert(OperationItem), [item_row("remote", "auto", phase="checking", unit="pages")])
            session.get(OperationItem, "remote").kind = "bookmark_artist"
        queue = TaskQueue(database, {"reconcile": object()})
        snapshot = queue.snapshot(history_limit=2)
        assert snapshot["active_operation_count"] == 4
        assert snapshot["queued_operation_count"] == 1
        assert snapshot["next_round_count"] == 1
        assert snapshot["needs_attention_count"] == 2
        assert [row["id"] for row in snapshot["operations"]] == ["manual", "auto", "maint", "next", "done-c", "done-b"]
        assert snapshot["round"]["counts"] == {"succeeded": 1, "failed": 1, "not_executed": 1, "future-v2": 1, "pending": 2}
        assert snapshot["round"]["total"] == 6
        assert snapshot["round"]["settled"] == 3
        assert next(row for row in snapshot["operations"] if row["id"] == "auto")["phase"]["total"] is None
        details = queue.details("manual", limit=2, problems_only=True)
        assert [row["state"] for row in details["items"]] == ["failed", "not_executed"]
        assert not details["has_more_items"]
        assert details["counts"]["future-v2"] == 1
        assert sum(row["count"] for row in details["phases"]) == 5
        assert queue.details("manual", offset=4, limit=1)["items"][0]["state"] == "pending"
        assert [row["id"] for row in queue.history(limit=1)["items"]] == ["done-c"]
        assert [row["id"] for row in queue.history(before="done-c", limit=10)["items"]] == ["done-b", "done-a"]
        assert [row["id"] for row in queue.history(attention_only=True)["items"]] == ["done-c", "done-a"]
        with pytest.raises(KeyError):
            queue.history(before="done-null")
    finally:
        database.close()


@contextmanager
def vm_probe(engine):
    """Count actual SQLite work through complete ORM fetching and response build."""
    records, drivers = [], set()
    def before(connection, cursor, statement, parameters, context, many):
        driver = connection.connection.driver_connection
        drivers.add(driver)
        driver.set_progress_handler(None, 0)
        plan = driver.execute("EXPLAIN QUERY PLAN " + statement, parameters[0] if many else parameters).fetchall()
        record = {"sql": statement, "vm": 0, "plan": [list(row) for row in plan]}
        records.append(record)
        def progress():
            record["vm"] += 1
            return 0
        driver.set_progress_handler(progress, 1)
    event.listen(engine, "before_cursor_execute", before)
    try:
        yield records
    finally:
        event.remove(engine, "before_cursor_execute", before)
        for driver in drivers:
            driver.set_progress_handler(None, 0)


def test_task_fixed_windows_and_single_item_delta_are_history_independent(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "task-scale.db")
    now = utcnow()
    measurements = []
    try:
        with database.write() as session:
            session.add(TaskRound(id="round", state="running", frozen_at=now))
            session.flush()
            add_operation(session, "probe", state="running", round_id="round")
        previous = 0
        for size in (100, 1000, 10000):
            with database.write() as session:
                session.execute(insert(Operation), [dict(id=f"history-{i:06}", kind="check", label="synthetic",
                    dedupe_key=f"history-{i:06}", state="finished", created_at=now, updated_at=now,
                    finished_at=now, needs_attention=i % 2 == 0, result={}) for i in range(previous, size)])
                for start in range(previous, size, 500):
                    session.execute(insert(OperationItem), [item_row(f"step-{i:06}", "probe",
                        state="failed" if i % 50 == 0 else "pending" if i == 1 else "succeeded", position=i)
                        for i in range(start, min(start+500, size))])
            with database.write() as session:
                probe = session.get(Operation, "probe")
                probe.parameters = {"saved_selection": list(range(size))}
                session.get(OperationItem, "step-000001").state = "pending"
                probe.result = {"planning_complete": True, "current_items": {str(i): {"old": True} for i in range(size)}}
                high = session.get(Operation, f"history-{size-1:06}")
                high.parameters = {"work_ids": list(range(size))}
                high.result = {"child_operation_ids": [str(i) for i in range(size)], "parent_operation_ids": [str(i) for i in range(size)]}
            queue = TaskQueue(database, {"reconcile": object()})
            reads = {}
            for name, call in (("snapshot", lambda: queue.snapshot(history_limit=20)),
                    ("details", lambda: queue.details("probe", limit=20)),
                    ("problems", lambda: queue.details("probe", limit=20, problems_only=True)),
                    ("history", lambda: queue.history(before="history-000025", limit=20)),
                    ("attention", lambda: queue.history(before="history-000050", limit=20, attention_only=True))):
                with vm_probe(database.engine) as records:
                    response = call()
                reads[name] = {"vm": sum(row["vm"] for row in records), "queries": len(records), "records": records}
                assert not any("TEMP B-TREE" in part[-1] for row in records for part in row["plan"]), (name, [row for row in records if any("TEMP B-TREE" in part[-1] for part in row["plan"])])
                assert reads[name]["vm"] < 20000
            with vm_probe(database.writer_engine) as records:
                with database.write() as session:
                    stored = session.get(OperationItem, "step-000001")
                    stored.state = "succeeded"
                    session.flush()
                    assert task_reads.operation_counts(session, "probe")["succeeded"] == size - size//50
            delta_vm = sum(row["vm"] for row in records)
            measurements.append({"history": size, "reads": reads, "delta_vm": delta_vm})
            previous = size
        for name in measurements[0]["reads"]:
            samples = [point["reads"][name]["vm"] for point in measurements]
            # Short first-page problems lists have fewer rows at H=100; this
            # output difference is bounded by K, not the skipped history.
            assert max(samples) < min(samples) * 4 + 1000, (name, samples)
        delta = [point["delta_vm"] for point in measurements]
        assert max(delta) < min(delta) * 2 + 1000
        (tmp_path / "task-status-vm.json").write_text(json.dumps(measurements, indent=2), encoding="utf-8")
        print(json.dumps({"task_vm": [{"H": point["history"], "reads": {name: value["vm"] for name, value in point["reads"].items()}, "delta": point["delta_vm"]} for point in measurements]}))
    finally:
        database.close()


def test_nullable_legacy_phase_groups_and_null_state_gate_semantics():
    """A legacy-shaped synthetic source keeps NULL groups distinct from ''."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE read_model_dirty(entity TEXT,identity TEXT,PRIMARY KEY(entity,identity)) WITHOUT ROWID")
            connection.exec_driver_sql("CREATE TABLE task_rounds(id TEXT PRIMARY KEY,created_at TEXT)")
            connection.exec_driver_sql("CREATE TABLE operations(id TEXT PRIMARY KEY,round_id TEXT,state TEXT,source TEXT,created_at TEXT,finished_at TEXT,needs_attention INTEGER,result TEXT)")
            connection.exec_driver_sql("CREATE TABLE operation_items(id TEXT PRIMARY KEY,operation_id TEXT,state TEXT,phase TEXT,unit TEXT,kind TEXT,position INTEGER)")
            task_reads.install_task_read_projection(connection)
            connection.exec_driver_sql("INSERT INTO task_rounds VALUES('r','2026-10-10')")
            connection.exec_driver_sql("INSERT INTO operations VALUES('o','r','running','manual','2026-10-10',NULL,0,'{}')")
            for ident, state, phase, unit in (("one", "succeeded", None, None), ("two", "future", None, None),
                    ("three", None, None, None), ("four", "succeeded", "", "")):
                connection.exec_driver_sql("INSERT INTO operation_items VALUES(?,?,?,?,?,'checked',0)", (ident, "o", state, phase, unit))
        with Session(engine) as session:
            assert task_reads.operation_counts(session, "o") == {"succeeded": 2, "future": 1, None: 1}
            # Old phase baseline enumerated only non-NULL item states.
            assert task_reads.phase_counts(session, "o", None, None) == {"succeeded": 1, "future": 1}
            assert task_reads.phase_counts(session, "o", "", "") == {"succeeded": 1}
            assert task_reads.operation_phases(session, "o") == [(None, None, None, 1), (None, None, "future", 1), (None, None, "succeeded", 1), ("", "", "succeeded", 1)]
            task_reads.verify_task_read_projection(session.connection())
            task_reads.rebuild_task_read_projection(session.connection())
            task_reads.verify_task_read_projection(session.connection())
    finally:
        engine.dispose()


def test_visible_task_pause_check_only_visits_its_ancestors_after_invalidation(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "task-paused-graph.db")
    try:
        queue = TaskQueue(database, {})
        class ParentMap(dict):
            def __init__(self, values):
                super().__init__(values)
                self.lookups = 0
            def get(self, key, default=None):
                self.lookups += 1
                return super().get(key, default)
            def items(self):
                raise AssertionError("visible task scanned unrelated parent graph")
            def __iter__(self):
                raise AssertionError("visible task scanned unrelated parent graph")
        for size in (100, 1000, 10000):
            parents = {f"unrelated-{i}": (f"unrelated-parent-{i}",) for i in range(size)}
            parents.update({"visible": ("middle",), "middle": ("root",),
                "cycle-a": ("cycle-b",), "cycle-b": ("cycle-a",)})
            queue._control_parents = ParentMap(parents)
            queue._paused_operations = {"root"}
            queue._pause_cache = None
            queue._pause_generation += 1
            assert queue.operation_paused("visible")
            assert queue._control_parents.lookups == 2
            assert not queue.operation_paused("cycle-a")
            assert queue._control_parents.lookups == 4
            assert not queue.operation_paused("missing")
            assert queue._control_parents.lookups == 5
    finally:
        database.close()


def test_missing_task_result_projection_fails_closed_without_source_fallback(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "task-missing-view.db")
    try:
        with database.write() as session:
            add_operation(session, "visible", state="finished", finished_at=utcnow())
        queue = TaskQueue(database, {})
        with database.write() as session:
            session.connection().exec_driver_sql("DELETE FROM read_task_operation_view WHERE operation_id='visible'")
        for read in (lambda: queue.snapshot(), lambda: queue.history(), lambda: queue.details("visible")):
            with pytest.raises(ProjectionUnavailable, match="result projection is missing"):
                read()
    finally:
        database.close()


def test_status_and_detail_item_hydration_skips_discarded_manifest_json(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "task-item-hydration.db")
    try:
        with database.write() as session:
            add_operation(session, "visible", state="running")
            row = item_row("running", "visible", state="running")
            row.update(target={"padding": "x" * 10000}, depends_on=[str(i) for i in range(10000)])
            session.execute(insert(OperationItem), [row])
        queue = TaskQueue(database, {})
        with vm_probe(database.engine) as records:
            assert queue.snapshot()["operations"][0]["phase"]["total"] == 1
            assert queue.details("visible")["items"][0]["id"] == "running"
        hydration = [row["sql"] for row in records if "SELECT operation_items.id," in row["sql"]]
        assert hydration
        assert all("operation_items.target" not in sql and "operation_items.depends_on" not in sql for sql in hydration)
    finally:
        database.close()
