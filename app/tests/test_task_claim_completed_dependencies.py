"""Bounded claim cost after a large synthetic reconciliation is partly settled."""
import json
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event, func, insert, select

from src.core.database import ArchiveDatabase
from src.core.models import Operation, OperationItem, TaskRound, utcnow
from src.core.tasks import ItemResult, ItemSpec, TaskQueue


class Checked:
    def __init__(self):
        self.executed = []

    def execute(self, item, context):
        self.executed.append(item.key)
        return ItemResult()


@contextmanager
def claim_probe(database, all_queries=False):
    """Count SQLite work through result fetching, rather than execute alone."""
    evidence, active = [], {}

    def finish(driver):
        record = active.pop(driver, None)
        if record is not None:
            record["elapsed_ms"] = (time.perf_counter() - record.pop("started")) * 1000
            driver.set_progress_handler(None, 0)

    def before(connection, cursor, statement, parameters, context, many):
        driver = connection.connection.driver_connection
        finish(driver)
        if not all_queries and "operation_items.position >" not in statement:
            return
        cursor.execute("EXPLAIN QUERY PLAN " + statement, parameters)
        record = {"sql": statement, "parameters": parameters,
            "plan": [list(row) for row in cursor.fetchall()], "vm_steps": 0, "started": time.perf_counter()}
        evidence.append(record)
        active[driver] = record

        def progress():
            record["vm_steps"] += 100
            return 0
        driver.set_progress_handler(progress, 100)

    event.listen(database.engine, "before_cursor_execute", before)
    try:
        yield evidence
    finally:
        for driver in list(active):
            finish(driver)
        event.remove(database.engine, "before_cursor_execute", before)


def seed_maintenance_shape(database):
    """Synthetic old NULL-lane manifest using the supplied sanitized counts.

    The unspecified 51,201 inventory rows are settled padding, not a claim
    about the user's inventory states. No real account, targets or files.
    """
    operation_id, round_id = "shape-maintenance", "shape-round"
    rows = []

    def add(kind, succeeded, pending=0, phase="verifying", unit="assets", lane=None, failed=0):
        for index in range(succeeded + pending + failed):
            state = "succeeded" if index < succeeded else "pending" if index < succeeded + pending else "failed"
            dependencies = []
            if kind == "reconcile_pair":
                dependencies = [f"reconcile_asset-{(index * 5 + offset) % 24174}" for offset in range(5)]
            elif kind == "reconcile_prepare":
                width = 196 if index == 0 else 6 if index % 5 < 2 else 5
                dependencies = [f"reconcile_asset-{(index * 7 + offset) % 24174}" for offset in range(width)]
            target = {"asset_id": f"synthetic-asset-{index}"} if kind == "reconcile_asset" else (
                {"record_id": f"synthetic-record-{index}"} if kind == "reconcile_derivative" else {})
            rows.append(dict(id=f"shape-{len(rows)}", operation_id=operation_id,
                item_key=f"{kind}-{index}", position=len(rows), kind=kind, target=target,
                phase=phase, unit=unit, lane=lane, depends_on=dependencies,
                state=state, attempts=0 if state == "pending" else 1, result={}))

    add("metadata_artist", 81, phase="checking", unit="artists", failed=2)
    add("reconcile_snapshot", 15280, phase="checking", unit="works")
    add("reconcile_asset", 24174, 24343)
    add("reconcile_derivative", 8123, 9858, unit="previews")
    add("reconcile_pair", 365, 1051, phase="inventory", unit="items")
    add("reconcile_prepare", 7394, 7886, phase="inventory", unit="items")
    add("inventory_padding", 51201, phase="inventory", unit="items")
    add("error", 0, phase="checking", unit="works", lane="check", failed=689)
    add("skip", 0, 57, phase="inventory", unit="items", lane="local")
    add("download", 8, phase="downloading", unit="assets", lane="download")
    assert len(rows) == 150512
    with database.write() as session:
        session.add(TaskRound(id=round_id, state="running"))
        session.flush()
        session.add(Operation(id=operation_id, round_id=round_id, kind="reconcile", label="synthetic shape",
            dedupe_key=operation_id, state="running", result={"planning_complete": True}))
        session.flush()
        for offset in range(0, len(rows), 1000):
            session.execute(insert(OperationItem), rows[offset:offset + 1000])
    return operation_id, round_id


def test_realistic_old_manifest_claims_and_empty_lanes_have_bounded_work(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "synthetic-shape.db")
    handler = Checked()
    queue = TaskQueue(database, {"reconcile": handler})
    operation_id, round_id = seed_maintenance_shape(database)
    try:
        with claim_probe(database) as evidence:
            started = time.perf_counter()
            assert queue._step("local", round_id)
            elapsed = (time.perf_counter() - started) * 1000
            # There is no pending check/download lane. Every poll examines at
            # most 32 light windows, and subsequent polls continue the cursor.
            empty_polls = []
            for lane in ("check", "download"):
                for _ in range(2):
                    marker = len(evidence)
                    assert not queue._step(lane, round_id)
                    assert queue._last_scan_more
                    empty_polls.append({"lane": lane, "queries": len(evidence)-marker,
                        "after": queue._candidate_after[operation_id, lane]})
        with claim_probe(database, all_queries=True) as full_evidence:
            started = time.perf_counter()
            assert queue._step("local", round_id)
            warm_elapsed = (time.perf_counter() - started) * 1000
        assert handler.executed == ["reconcile_asset-24174", "reconcile_asset-24175"]
        assert len(evidence) <= 2 + 4 * 64
        assert all(row["vm_steps"] < 2500 for row in evidence)
        assert all(any("ix_items_candidate" in part[-1] for part in row["plan"]) for row in evidence)
        assert not any("TEMP B-TREE" in part[-1] for row in evidence for part in row["plan"])
        assert empty_polls[1]["after"] > empty_polls[0]["after"]
        assert empty_polls[3]["after"] > empty_polls[2]["after"]
        assert sum(row["vm_steps"] for row in full_evidence) < 1_000_000
        with database.read() as session:
            operation = session.get(Operation, operation_id)
            assert operation.result["phase_snapshots"]["verifying:assets"] == {
                "phase": "verifying", "unit": "assets", "done": 24176, "total": 48517}
            assert operation.state == "running"
        (tmp_path / "shape-evidence.json").write_text(json.dumps({"step_ms": elapsed,
            "warm_step_ms": warm_elapsed, "empty_polls": empty_polls, "claims": evidence,
            "full_step_queries": full_evidence}, indent=2), encoding="utf-8")
        print(json.dumps({"shape_rows": 150512, "step_ms": elapsed,
            "candidate_vm": sum(row["vm_steps"] for row in evidence), "empty_polls": empty_polls}))
    finally:
        database.close()


def test_indexed_state_counts_retain_unknown_saved_states_and_completion_gate(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "synthetic-states.db")
    queue = TaskQueue(database, {"reconcile": Checked()})
    specs = [ItemSpec(str(index), "checked", {}, "inventory") for index in range(7)]
    with database.write() as session:
        operation_id = queue.enqueue_in_transaction(session, "reconcile", {}, "synthetic states", specs=specs)["operation_id"]
        items = list(session.scalars(select(OperationItem).where(OperationItem.operation_id == operation_id).order_by(OperationItem.position)))
        for item, state in zip(items, ("succeeded", "failed", "skipped", "not_executed", "pending", "future_state", "")):
            item.state = state
    try:
        with database.write() as session:
            expected = dict(session.execute(select(OperationItem.state, func.count()).where(
                OperationItem.operation_id == operation_id, OperationItem.phase == "inventory",
                OperationItem.unit == "items").group_by(OperationItem.state)).all())
            assert queue._phase_counts(session, operation_id, "inventory", "items") == expected
            assert queue._phase_counts(session, operation_id, "no_such_phase", "items") == {}
            assert queue._phase_counts(session, "no_such_operation", "inventory", "items") == {}
            queue._finalize(session, [operation_id])
            assert session.get(Operation, operation_id).state != "finished"
            for item in session.scalars(select(OperationItem).where(OperationItem.operation_id == operation_id)):
                if item.state != "":
                    item.state = "succeeded"
            session.flush()
            queue._finalize(session, [operation_id])
            assert session.get(Operation, operation_id).state != "finished"
            for item in session.scalars(select(OperationItem).where(OperationItem.operation_id == operation_id)):
                item.state = "succeeded"
            session.flush()
            queue._finalize(session, [operation_id])
            assert session.get(Operation, operation_id).state == "finished"
            assert session.get(Operation, operation_id).result["counts"] == {"succeeded": 7}
            empty_id = queue.enqueue_in_transaction(session, "reconcile", {}, "synthetic empty", specs=[])["operation_id"]
            assert queue._phase_counts(session, empty_id, "inventory", "items") == {}
            queue._finalize(session, [empty_id])
            assert session.get(Operation, empty_id).state == "finished"
            assert session.get(Operation, empty_id).result["counts"] == {}
    finally:
        database.close()


def test_legacy_windows_revisit_dependencies_and_restart_without_recount(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "synthetic-windows.db")
    handler = Checked()
    queue = TaskQueue(database, {"reconcile": handler})
    specs = [ItemSpec("root", "download", {}, "downloading")]
    specs += [ItemSpec(f"waiting-{index}", "checked", {}, "inventory", depends_on=("root",)) for index in range(4100)]
    specs.append(ItemSpec("tail", "checked", {}, "inventory"))
    with database.write() as session:
        operation_id = queue.enqueue_in_transaction(session, "reconcile", {}, "synthetic windows", specs=specs)["operation_id"]
        for item in session.scalars(select(OperationItem).where(OperationItem.operation_id == operation_id)):
            item.lane = None
        root = session.scalar(select(OperationItem).where(OperationItem.operation_id == operation_id, OperationItem.item_key == "root"))
        root.not_before = utcnow() + timedelta(days=1)
    try:
        # The synchronous isolated driver must keep scanning past a bounded
        # empty window, but eventually stop when every lane is truly blocked.
        assert not queue.run_round()
        assert handler.executed == ["tail"]
        with database.write() as session:
            root = session.scalar(select(OperationItem).where(OperationItem.operation_id == operation_id, OperationItem.item_key == "root"))
            root.state, root.attempts = "succeeded", 1
        queue.set_manual_paused(operation_id, True)
        assert not queue._step("local")
        queue = TaskQueue(database, {"reconcile": handler})
        queue.set_manual_paused(operation_id, False)
        assert queue._step("local")
        assert handler.executed == ["tail", "waiting-0"]
        with database.read() as session:
            states = {item.item_key: (item.state, item.attempts) for item in session.scalars(
                select(OperationItem).where(OperationItem.operation_id == operation_id,
                    OperationItem.item_key.in_(("root", "tail", "waiting-0", "waiting-1"))))}
        assert states == {"root": ("succeeded", 1), "tail": ("succeeded", 1),
            "waiting-0": ("succeeded", 1), "waiting-1": ("pending", 0)}
    finally:
        database.close()


@pytest.mark.parametrize("size,width,stored_lane", [(4000, 16, None), (16000, 64, None), (16000, 64, "local")])
def test_completed_dependency_claim_does_not_recheck_entire_remaining_manifest(tmp_path, size, width, stored_lane):
    database = ArchiveDatabase.create(tmp_path / "synthetic.db")
    queue = TaskQueue(database, {"reconcile": Checked()})
    with database.write() as session:
        session.add(TaskRound(id="synthetic-round", state="running"))
        session.flush()
        session.add(Operation(id="synthetic-maintenance", round_id="synthetic-round", kind="reconcile",
            label="synthetic", dedupe_key="synthetic", state="running", result={"planning_complete": True}))
        session.flush()
        rows = []
        # Completed roots are shared in this minimized reproducer: each gate
        # still has a legitimate settled dependency barrier, as in page batches.
        for index in range(size):
            rows.append(dict(id=f"settled-{index}", operation_id="synthetic-maintenance", item_key=f"root-{index}",
                position=index, kind="checked", target={}, phase="verifying", unit="assets", lane=stored_lane,
                depends_on=[], state="succeeded", attempts=1, result={}))
        for index in range(size):
            rows.append(dict(id=f"pending-{index}", operation_id="synthetic-maintenance", item_key=f"gate-{index}",
                position=size+index, kind="checked", target={}, phase="inventory", unit="items", lane=stored_lane,
                depends_on=[f"root-{value}" for value in range(width)], state="pending", attempts=0, result={}))
        for start in range(0, len(rows), 1000):
            session.execute(insert(OperationItem), rows[start:start+1000])
    try:
        durations = []
        with claim_probe(database) as evidence:
            for _ in range(3):
                started = time.perf_counter()
                assert queue._step("local", "synthetic-round")
                durations.append((time.perf_counter() - started) * 1000)
        with database.read() as session:
            processed = list(session.scalars(select(OperationItem).where(OperationItem.item_key.in_(
                ["gate-0", "gate-1", "gate-2"]))))
            assert all(item.state == "succeeded" and item.attempts == 1 for item in processed)
        result = {"size": size, "dependency_width": width, "stored_lane": stored_lane, "step_ms": durations, "claims": evidence}
        (tmp_path / "claim-evidence.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({"size": size, "width": width, "step_ms": durations,
            "claim_ms": [record["elapsed_ms"] for record in evidence],
            "vm_steps": [record["vm_steps"] for record in evidence]}))
        assert evidence
        # Deterministic work bound: three claims must not reevaluate every
        # remaining gate's 64 settled references merely to select one step.
        assert max(record["vm_steps"] for record in evidence) < 1_000_000
    finally:
        database.close()
