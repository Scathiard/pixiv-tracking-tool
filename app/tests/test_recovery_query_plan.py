"""Startup recovery seeks each parent's state prefix; synthetic data only."""
import json

import pytest
from sqlalchemy import create_engine, event, insert, select

from src.core.database import ArchiveDatabase
from src.core.models import Base, Operation, OperationItem
from src.core.tasks import TaskQueue


@pytest.fixture
def recovery_case(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "recovery.db")
    queue = TaskQueue(database, {})
    try:
        yield database, queue
    finally:
        queue.stop()
        database.close()


def test_empty_recovery_does_not_visit_settled_item_history(recovery_case):
    database, queue = recovery_case
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("SELECT operation_items."):
            statements.append((statement, parameters))

    event.listen(database.writer_engine, "before_cursor_execute", capture)
    try:
        queue.recover()
    finally:
        event.remove(database.writer_engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    statement, parameters = statements[0]

    # Replay the actual startup SELECT against the same model indexes without
    # unrelated projection writes. This isolates historical-row visitation.
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(insert(Operation), [dict(id="finished", kind="verify",
                label="synthetic", dedupe_key="finished", state="finished")])
            connection.execute(insert(OperationItem), [dict(id=f"item-{i}",
                operation_id="finished", item_key=str(i), kind="verify", target={},
                phase="verifying", state="succeeded", position=i) for i in range(16000)])
        with engine.connect() as connection:
            driver = connection.connection.driver_connection
            plan = driver.execute("EXPLAIN QUERY PLAN " + statement, parameters).fetchall()
            steps = [0]

            def count_steps():
                steps[0] += 100
                return 0

            driver.set_progress_handler(count_steps, 100)
            try:
                assert driver.execute(statement, parameters).fetchall() == []
            finally:
                driver.set_progress_handler(None, 0)
        print(json.dumps(dict(history_items=16000, selected_items=0,
            vm_steps_floor=steps[0], plan=plan)))
        assert steps[0] < 2000, "empty recovery must seek states, not scan settled history"
    finally:
        engine.dispose()


@pytest.mark.parametrize("fail_after_item_flush", [False, True])
def test_recovery_preserves_finished_parents_and_atomic_retry(recovery_case, monkeypatch,
                                                            fail_after_item_flush):
    database, queue = recovery_case
    with database.write() as session:
        session.add_all([Operation(id=key, kind="verify", label=key, dedupe_key=key,
            state=state, result={"planning_complete": False})
            for key, state in (("finished", "finished"), ("active", "running"))])
        session.flush()
        session.add_all([OperationItem(id=key, operation_id=parent, item_key=key,
            kind="verify", target={}, phase="verifying", state=state, attempts=3,
            result={"checkpoint": "saved"}) for key, parent, state in (
                ("finished-running", "finished", "running"),
                ("active-running", "active", "running"),
                ("settled", "finished", "succeeded"),
                ("pending", "finished", "pending"))])

    if fail_after_item_flush:
        original = queue._set_operation_state

        def fail(operation, state):
            # The operation SELECT autoflushes the recovered items first.
            raise RuntimeError("synthetic recovery interruption")

        monkeypatch.setattr(queue, "_set_operation_state", fail)
        with pytest.raises(RuntimeError, match="synthetic recovery interruption"):
            queue.recover()
        with database.read() as session:
            assert session.get(OperationItem, "finished-running").state == "running"
            assert session.get(OperationItem, "active-running").state == "running"
            assert session.get(Operation, "active").state == "running"
        monkeypatch.setattr(queue, "_set_operation_state", original)

    observations = {}
    queue._recover_observed(observations)
    assert observations == dict(item_count=2, operation_count=1, automatic_check_count=0)
    with database.read() as session:
        assert session.get(Operation, "finished").state == "finished"
        assert session.get(Operation, "active").state == "planning"
        items = list(session.scalars(select(OperationItem).order_by(OperationItem.id)))
        assert {item.id: item.state for item in items} == {
            "finished-running": "pending", "active-running": "pending",
            "settled": "succeeded", "pending": "pending"}
        assert all(item.attempts == 3 and item.result == {"checkpoint": "saved"}
                   for item in items)
    queue._recover_observed(observations)
    assert observations["item_count"] == 0
