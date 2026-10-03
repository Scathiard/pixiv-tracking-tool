"""Disconnected runtime queue boundaries; all data and clients are synthetic."""
import shutil
import time

import pytest
from sqlalchemy import func, insert, select

from src.core.database import ArchiveDatabase
from src.core.models import Operation, OperationItem, TaskRound, utcnow
from src.core.runtime import Runtime
from src.core.tasks import ItemResult, ItemSpec, TaskQueue


class Handler:
    def __init__(self):
        self.executed = []
    def plan(self, parameters, context):
        return [ItemSpec("only", "checked", {}, "checking")]
    def execute(self, item, context):
        self.executed.append((context.operation_id, item.key))
        return ItemResult()


@pytest.fixture(scope="module")
def schema(tmp_path_factory):
    path = tmp_path_factory.mktemp("offline-schema") / "archive.db"
    database = ArchiveDatabase.create(path)
    database.close()
    return path


@pytest.fixture
def runtime(tmp_path, schema):
    path = tmp_path / "archive.db"
    shutil.copyfile(schema, path)
    instance = Runtime(path, tmp_path / "images")
    instance.maintenance.tick = lambda: None
    yield instance
    assert instance.close(timeout=15)


def wait_for(predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("synthetic queue did not make expected progress")


@pytest.mark.parametrize("state,has_round", [("queued", True), ("ready", True), ("planning", True),
                                            ("interrupted", True), ("planning", False), ("interrupted", False)])
def test_offline_old_operation_keeps_state_cursor_attempts_then_reconnects(runtime, state, has_round):
    handler = Handler()
    runtime.queue.handlers["metadata"] = runtime.queue.handlers["verify"] = handler
    with runtime.database.write() as session:
        ident = runtime.queue.enqueue_in_transaction(session, "metadata", {"work_ids": [1]}, "old remote",
            specs=[ItemSpec("only", "checked", {}, "checking")])["operation_id"]
        round_record = TaskRound(state="running", frozen_at=utcnow())
        session.add(round_record)
        session.flush()
        operation = session.get(Operation, ident)
        operation.state, operation.round_id = state, round_record.id if has_round else None
        operation.result = {"planning_complete": state != "planning", "planning_cursor": {"after": 64}}
        item = session.scalar(select(OperationItem).where(OperationItem.operation_id == ident))
        item.attempts = 2
        expected_result = dict(operation.result)
    runtime.start()
    local = runtime.queue.submit("verify", {"work_ids": [2]}, "local verify")["operation_id"]
    wait_for(lambda: runtime.queue.details(local)["state"] == "finished")
    with runtime.database.read() as session:
        operation = session.get(Operation, ident)
        assert operation.state == state and operation.result == expected_result
        item = session.scalar(select(OperationItem).where(OperationItem.operation_id == ident))
        assert item.state == "pending" and item.attempts == 2
    waiting = runtime.queue.details(ident)
    assert waiting["state"] == "queued" and waiting["lane"] is None
    assert waiting["phase"]["name"] == "waiting_connection"
    assert waiting["phase"]["detail"] == "等待连接 Pixiv，任务进度已保留"
    assert all(operation != ident for operation, _ in handler.executed)
    runtime.client = object()  # Dynamic callback resumes without rebuilding the queue.
    wait_for(lambda: runtime.queue.details(ident)["state"] == "finished")
    assert runtime.queue.details(ident)["result"]["outcome"] == "succeeded"
    assert [operation for operation, _ in handler.executed].count(ident) == 1


def test_large_offline_download_queue_and_full_blocked_window_do_not_hide_local_work(runtime):
    class ForbiddenDownloader:
        def download(self, *_):
            raise AssertionError("offline queue must never call downloader")
    runtime.queue.handlers["reconcile"].downloader = ForbiddenDownloader()
    local_handler = Handler()
    for kind in ("verify", "thumbnails", "delete"):
        runtime.queue.handlers[kind] = local_handler
    size = 98552
    with runtime.database.write() as session:
        session.add(TaskRound(id="remote-round", state="running", frozen_at=utcnow()))
        session.flush()
        session.add(Operation(id="large-offline", kind="reconcile", dedupe_key="large", label="old maintenance",
            state="ready", round_id="remote-round", result={"planning_complete": True, "planning_cursor": {"after": 64}}))
        session.flush()
        for start in range(0, size, 1000):
            session.execute(insert(OperationItem), [{"id": f"old-{i}", "operation_id": "large-offline",
                "item_key": str(i), "position": i, "kind": "download" if i == 0 else "checked",
                "target": {"url": "https://i.pximg.net/synthetic"} if i == 0 else {},
                "phase": "downloading" if i == 0 else "checking", "unit": "items",
                "lane": "download" if i == 0 else "check", "state": "pending", "attempts": 0,
                "result": {}, "depends_on": []} for i in range(start, min(start + 1000, size))])
        for i in range(64):
            runtime.queue.enqueue_in_transaction(session, "artist_source", {"artist_ids": [i]}, "old source",
                specs=[ItemSpec("only", "checked", {}, "checking")])
    runtime.start()
    for kind in ("verify", "thumbnails", "delete"):
        ident = runtime.queue.submit(kind, {"work_ids": [1]}, kind)["operation_id"]
        wait_for(lambda: runtime.queue.details(ident)["state"] == "finished")
    with runtime.database.read() as session:
        assert session.scalar(select(func.count()).select_from(OperationItem).where(
            OperationItem.operation_id == "large-offline", OperationItem.state == "pending")) == size
        assert session.scalar(select(func.sum(OperationItem.attempts)).where(OperationItem.operation_id == "large-offline")) == 0
        assert session.get(Operation, "large-offline").state == "ready"
        assert session.scalar(select(func.count()).select_from(Operation).where(Operation.kind == "artist_source",
            Operation.state == "ready")) == 64
    assert len(local_handler.executed) == 3


def test_manual_and_automatic_pauses_still_hold_after_connection_returns(runtime):
    handler = Handler()
    runtime.queue.handlers["reconcile"] = runtime.queue.handlers["check"] = handler
    manual = runtime.queue.submit("reconcile", {}, "manual")["operation_id"]
    automatic = runtime.queue.submit("check", {}, "automatic", source="automatic")["operation_id"]
    runtime.queue.set_manual_paused(manual, True)
    runtime.queue.set_automatic_paused(True)
    assert runtime.queue.details(manual)["state"] == "paused"
    runtime.client = object()
    assert not runtime.queue._step("check")
    runtime.queue.set_manual_paused(manual, False)
    assert runtime.queue.run_round()
    assert runtime.queue.details(manual)["state"] == "finished"
    assert runtime.queue.details(automatic)["state"] == "queued"
    runtime.queue.set_automatic_paused(False)
    assert runtime.queue.run_round()
    assert runtime.queue.details(automatic)["state"] == "finished"


def test_disconnect_after_claim_refunds_attempt_without_failure(runtime):
    class Disconnecting(Handler):
        def execute(self, item, context):
            runtime.client = None
            context.checkpoint()
            raise AssertionError("no remote operation after disconnect checkpoint")
    handler = Disconnecting()
    runtime.queue.handlers["repair"] = handler
    runtime.client = object()
    ident = runtime.queue.submit("repair", {}, "remote")["operation_id"]
    assert runtime.queue._step("check")
    assert runtime.queue._step("check")
    details = runtime.queue.details(ident)
    assert details["state"] == "queued" and details["counts"] == {"pending": 1}
    assert details["items"][0]["attempts"] == 0
    assert details["items"][0]["result"] == {}
    runtime.queue.handlers["repair"] = Handler()
    runtime.client = object()
    assert runtime.queue.run_round()
    assert runtime.queue.details(ident)["result"]["outcome"] == "succeeded"


def test_plain_unit_queue_has_no_implicit_connection_gate(runtime):
    handler = Handler()
    queue = TaskQueue(runtime.database, {"repair": handler})
    ident = queue.submit("repair", {}, "unit queue")["operation_id"]
    assert queue.run_round()
    assert queue.details(ident)["state"] == "finished"
