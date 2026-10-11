"""Bounded operation windows and committed pause inheritance; no network."""
import shutil
import threading
import time
from collections.abc import Mapping
from datetime import timedelta

import pytest
from sqlalchemy import select

from src.core.database import ArchiveDatabase
from src.core.models import Operation, OperationItem, TaskRound, utcnow
from src.core.tasks import ItemResult, ItemSpec, TaskQueue


class Handler:
    def __init__(self):
        self.executed = []
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def plan(self, parameters, context):
        raise AssertionError("Synthetic planning remains cooled")

    def execute(self, item, context):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(0.01)
        with self.lock:
            self.executed.append((context.operation_id, item.key))
            self.running -= 1
        return ItemResult()


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    path = tmp_path_factory.mktemp("operation-schema") / "template.db"
    database = ArchiveDatabase.create(path)
    database.close()
    return path


@pytest.fixture
def case(tmp_path, template):
    path = tmp_path / "synthetic.db"
    shutil.copyfile(template, path)
    db = ArchiveDatabase(path)
    handler = Handler()
    queue = TaskQueue(db, {"reconcile": handler, "verify": handler}, check_workers=2)
    yield db, queue, handler
    assert queue.stop()
    db.close()


def seed(db, queue, cold_count, ready=True, items=1):
    base = utcnow()
    cold = []
    with db.write() as session:
        admitted = TaskRound()
        session.add(admitted)
        session.flush()
        round_id = admitted.id
        for number in range(cold_count):
            ident = queue.enqueue_in_transaction(session, "reconcile", {"synthetic": number}, "cooled")["operation_id"]
            operation = session.get(Operation, ident)
            operation.state, operation.started_at = "planning", base
            operation.round_id = round_id
            operation.created_at = base + timedelta(milliseconds=number)
            operation.result = {"planning_complete": False,
                                "planning_not_before": (base + timedelta(hours=1)).isoformat()}
            cold.append(ident)
        late = None
        if ready:
            late = queue.enqueue_in_transaction(session, "verify", {"synthetic": "late"}, "late",
                specs=[ItemSpec(str(index), "checked", {}, "checking") for index in range(items)])["operation_id"]
            operation = session.get(Operation, late)
            operation.created_at, operation.round_id = base + timedelta(seconds=1), round_id
    return round_id, cold, late


@pytest.mark.parametrize("cold_count,steps", [(63, 1), (64, 2), (128, 3)])
def test_ready_operation_beyond_window_runs_in_bounded_steps(case, cold_count, steps):
    db, queue, handler = case
    _, cold, late = seed(db, queue, cold_count)
    for _ in range(steps):
        queue._step("check")
    assert handler.executed == [(late, "0")]
    with db.read() as session:
        assert session.get(Operation, late).state == "finished"
        assert all(session.get(Operation, ident).last_claim_seq == 0 for ident in cold)
        assert all(session.get(Operation, ident).result["planning_complete"] is False for ident in cold)


@pytest.mark.parametrize("cold_count", [64, 128])
def test_exhausted_cycle_terminates_run_round_and_resets_each_lane(case, cold_count):
    db, queue, _ = case
    round_id, _, _ = seed(db, queue, cold_count, ready=False)
    original, calls = queue._step, []

    def bounded(lane, active_round=None):
        calls.append((lane, active_round))
        assert len(calls) <= 12, "An exhausted window must not busy-loop"
        return original(lane, active_round)

    queue._step = bounded
    assert queue.run_round() is False
    assert len(calls) == 3 * (cold_count // 64 + 1)
    assert all((lane, round_id) not in queue._operation_after for lane in ("check", "local", "download"))
    assert not queue._last_scan_more


def test_new_submission_invalidates_old_epoch_and_restarts_priority_order(case):
    db, queue, handler = case
    _, _, _ = seed(db, queue, 128, ready=False)
    assert not queue._step("check")
    key = ("check", None)
    stale_epoch, stale_after = queue._operation_epochs[key], queue._operation_after[key]
    with db.write() as session:
        late = queue.enqueue_in_transaction(session, "verify", {"synthetic": "new"}, "new",
            specs=[ItemSpec("new", "checked", {}, "checking")])["operation_id"]
        # A newly submitted higher priority/older selection must be reconsidered
        # immediately rather than waiting for the stale suffix to finish.
        session.get(Operation, late).created_at = utcnow() - timedelta(days=1)
    assert key not in queue._operation_after
    assert not queue._advance_operation_cursor(key, stale_epoch, stale_after)
    assert queue._step("check")
    assert handler.executed == [(late, "new")]


def test_new_request_in_later_window_runs_after_bounded_rescan(case):
    db, queue, handler = case
    _, _, _ = seed(db, queue, 128, ready=False)
    assert not queue._step("check")
    with db.write() as session:
        late = queue.enqueue_in_transaction(session, "verify", {"synthetic": "new-suffix"}, "new suffix",
            specs=[ItemSpec("new-suffix", "checked", {}, "checking")])["operation_id"]
        session.get(Operation, late).created_at = utcnow() + timedelta(seconds=2)
    for _ in range(3):
        queue._step("check")
    assert handler.executed == [(late, "new-suffix")]


def test_repeated_unchanged_dedupe_cannot_reset_window_or_pause_generation(case):
    db, queue, handler = case
    _, cold, late = seed(db, queue, 128)
    generation = queue._pause_generation
    for _ in range(3):
        assert queue.submit("reconcile", {"synthetic": 0}, "same request")["operation_id"] == cold[0]
        queue._step("check")
    assert handler.executed == [(late, "0")]
    assert queue._pause_generation == generation


def test_dedupe_with_actual_manual_priority_promotion_resets_window(case):
    db, queue, handler = case
    _, _, _ = seed(db, queue, 64, ready=False)
    with db.write() as session:
        late = queue.enqueue_in_transaction(session, "verify", {"synthetic": "promoted"}, "automatic", source="automatic",
            specs=[ItemSpec("promoted", "checked", {}, "checking")])["operation_id"]
        session.get(Operation, late).created_at = utcnow() - timedelta(days=1)
    assert not queue._step("check")
    assert ("check", None) in queue._operation_after
    assert queue.submit("verify", {"synthetic": "promoted"}, "manual")["operation_id"] == late
    assert ("check", None) not in queue._operation_after
    assert queue._step("check")
    assert handler.executed == [(late, "promoted")]


@pytest.mark.parametrize("mutation", ["delete", "pause"])
def test_missing_or_paused_anchor_cannot_hide_ready_suffix(case, mutation):
    db, queue, handler = case
    _, cold, late = seed(db, queue, 64)
    assert not queue._step("check")
    if mutation == "delete":
        with db.write() as session:
            session.delete(session.get(Operation, cold[-1]))
    else:
        queue.set_manual_paused(cold[-1], True)
    assert queue._step("check")
    assert handler.executed == [(late, "0")]


def test_cursor_is_scoped_to_lane_and_round(case):
    db, queue, handler = case
    round_id, _, late = seed(db, queue, 64)
    assert not queue._step("local", round_id)
    assert ("local", round_id) in queue._operation_after
    assert ("check", round_id) not in queue._operation_after
    assert not queue._step("check", round_id)
    assert queue._step("check", round_id)
    assert handler.executed == [(late, "0")]
    assert ("local", round_id) in queue._operation_after
    assert ("check", round_id) not in queue._operation_after


def test_real_two_check_workers_reach_suffix_without_same_operation_overlap(case):
    db, queue, handler = case
    _, _, late = seed(db, queue, 64, items=4)
    queue.start()
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        with db.read() as session:
            if session.get(Operation, late).state == "finished":
                break
        time.sleep(0.02)
    assert queue.stop()
    assert queue.worker_error is None
    assert sorted(handler.executed) == sorted((late, str(index)) for index in range(4))
    assert handler.peak == 1


class CopyCounter(Mapping):
    def __init__(self, values):
        self.values, self.copies = values, 0

    def __getitem__(self, key):
        self.copies += 1
        return self.values[key]

    def __iter__(self):
        return iter(self.values)

    def __len__(self):
        return len(self.values)


def test_pause_graph_one_linear_snapshot_shared_parents_deep_chain_and_cycles(case):
    _, queue, _ = case
    # Exact entry-copy work for the original red probe: N queries now copy N
    # entries once, rather than N entries for every query.
    for count in (4, 8, 16, 32, 64):
        graph = CopyCounter({str(index): () for index in range(count)})
        with queue._scheduler_lock:
            queue._control_parents = graph
            queue._paused_operations = {"unrelated-paused"}
            queue._pause_generation += 1
            queue._pause_cache = None
        for index in range(count):
            assert str(index) not in queue._effective_paused()
        assert graph.copies == count
        with queue._scheduler_lock:
            queue._paused_operations.clear()
            queue._pause_generation += 1
            queue._pause_cache = None
        assert "0" not in queue._effective_paused()
        assert graph.copies == count
    parents = {str(index): (str(index - 1),) for index in range(1, 3000)}
    parents.update({"a": ("root", "free"), "b": ("root",), "cycle-a": ("a", "cycle-b"),
                    "cycle-b": ("cycle-a",), "free-cycle-a": ("free-cycle-b",),
                    "free-cycle-b": ("free-cycle-a",)})
    paused = queue._paused_closure({"root", "0"}, parents)
    assert {"2999", "a", "b", "cycle-a", "cycle-b"} <= paused
    assert not {"free", "free-cycle-a", "free-cycle-b"} & paused


def test_pause_generation_change_during_unlocked_bfs_retries_snapshot(case, monkeypatch):
    _, queue, _ = case
    parent = queue.submit("reconcile", {"synthetic": "parent"}, "parent")["operation_id"]
    # Start from a non-empty pause set so this exercises lock-free BFS rather
    # than the no-pause shortcut.
    with queue._scheduler_lock:
        queue._paused_operations.add("unrelated-paused")
        queue._pause_generation += 1
    original, calls = queue._paused_closure, []

    def closure(paused, parents):
        calls.append(1)
        if len(calls) == 1:
            # This write would deadlock if the BFS still held the scheduler lock.
            queue.set_manual_paused(parent, True)
        return original(paused, parents)

    monkeypatch.setattr(queue, "_paused_closure", closure)
    assert parent in queue._effective_paused()
    assert len(calls) == 2
    assert parent in queue._effective_paused()
    assert len(calls) == 2


def test_pause_snapshot_and_warm_checkpoints_never_borrow_database(case, monkeypatch):
    db, queue, _ = case
    graph = CopyCounter({"child": ("parent",), "grandchild": ("child",)})
    with queue._scheduler_lock:
        queue._control_parents = graph
        queue._paused_operations = {"parent"}
        queue._pause_generation += 1
        queue._pause_cache = None

    def forbidden(*args, **kwargs):
        raise AssertionError("Pause checks must not wait for a database connection")

    monkeypatch.setattr(db, "read", forbidden)
    monkeypatch.setattr(db, "write", forbidden)
    for _ in range(20):
        assert "grandchild" in queue._effective_paused()
    assert graph.copies == 2


def test_parent_relationship_publishes_only_after_commit_and_recovers_ancestry(case):
    db, queue, _ = case
    parent = queue.submit("reconcile", {"synthetic": "parent"}, "parent")["operation_id"]
    child = queue.submit("verify", {"synthetic": "child"}, "child")["operation_id"]
    other = queue.submit("reconcile", {"synthetic": "other"}, "other")["operation_id"]
    queue.set_manual_paused(parent, True)
    assert not queue.operation_paused(child)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with db.write() as session:
            existing = queue.enqueue_in_transaction(session, "verify", {"synthetic": "child"}, "child", parent_id=parent)
            assert existing["created"] is False
            assert not queue.operation_paused(child)
            raise RuntimeError("synthetic rollback")
    assert not queue.operation_paused(child)
    with db.read() as session:
        assert session.get(Operation, child).result.get("parent_operation_ids", []) == []
        assert session.get(Operation, parent).result.get("child_operation_ids", []) == []
    with db.write() as session:
        queue.enqueue_in_transaction(session, "verify", {"synthetic": "child"}, "child", parent_id=parent)
        queue.enqueue_in_transaction(session, "verify", {"synthetic": "child"}, "child", parent_id=other)
        grandchild = queue.enqueue_in_transaction(session, "verify", {"synthetic": "grandchild"}, "grandchild", parent_id=child,
            specs=[ItemSpec("grandchild", "checked", {}, "checking")])["operation_id"]
        assert not queue.operation_paused(child)
    assert queue.operation_paused(child)
    assert queue.operation_paused(grandchild)
    generation, epochs = queue._pause_generation, dict(queue._operation_epochs)
    with db.write() as session:
        queue.enqueue_in_transaction(session, "verify", {"synthetic": "child"}, "same parent", parent_id=parent)
    assert queue._pause_generation == generation
    assert queue._operation_epochs == epochs
    with db.write() as session:
        session.get(Operation, child).state = "finished"
    restored = TaskQueue(db, queue.handlers, allowed_kinds=lambda: {"verify"})
    assert restored.operation_paused(grandchild)
    assert not restored._step("check")
    restored.set_manual_paused(parent, False)
    assert not restored.operation_paused(grandchild)
    # Restored operation windows and parent closure are transient; durable
    # pending work remains executable after the ancestor resumes.
    assert restored._step("check")
    with db.read() as session:
        assert session.scalar(select(OperationItem.state).where(OperationItem.operation_id == grandchild)) == "succeeded"


def test_restart_restores_only_referenced_deep_ancestors_and_tolerates_missing_cycle(case):
    db, queue, _ = case
    parent = queue.submit("reconcile", {"synthetic": "root"}, "root")["operation_id"]
    with db.write() as session:
        previous = parent
        for index in range(24):
            ident = "historical-" + str(index)
            session.add(Operation(id=ident, kind="verify", source="manual", label="synthetic history",
                dedupe_key=ident, state="finished", result={"parent_operation_ids": [previous]}))
            previous = ident
        session.add(Operation(id="unrelated-history", kind="verify", source="manual", label="unrelated history",
            dedupe_key="unrelated-history", state="finished", result={}))
        leaf = queue.enqueue_in_transaction(session, "verify", {"synthetic": "deep-leaf"}, "leaf",
            specs=[ItemSpec("leaf", "checked", {}, "checking")])["operation_id"]
        session.get(Operation, leaf).result = {**session.get(Operation, leaf).result,
                                             "parent_operation_ids": [previous, "missing-ancestor"]}
        # A durable cycle must not make restart traversal loop forever.
        session.get(Operation, parent).result = {"parent_operation_ids": [leaf]}
    queue.set_manual_paused(parent, True)
    restored = TaskQueue(db, queue.handlers, allowed_kinds=lambda: {"verify"})
    assert restored.operation_paused(leaf)
    assert "historical-0" in restored._control_parents
    assert "unrelated-history" not in restored._control_parents
    assert "missing-ancestor" not in restored._control_parents
    assert not restored._step("check")
    restored.set_manual_paused(parent, False)
    assert not restored.operation_paused(leaf)
    assert restored._step("check")
