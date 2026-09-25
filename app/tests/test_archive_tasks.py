import pytest

from src.core.database import ArchiveDatabase
from src.core.models import Operation, OperationItem
from src.core.tasks import ItemResult, ItemSpec, TaskQueue


class Handler:
    def __init__(self):
        self.planned = []
        self.executed = []
        self.on_plan = None
        self.on_execute = None

    def plan(self, parameters, context):
        self.planned.append(parameters)
        if self.on_plan:
            self.on_plan(parameters, context)
        return [ItemSpec(str(index), "asset", {"index": index}, "downloading", "pages")
                for index in range(parameters.get("count", 2))]

    def execute(self, item, context):
        self.executed.append(item.key)
        if self.on_execute:
            return self.on_execute(item, context)
        return ItemResult()


@pytest.fixture
def tasks(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    handler = Handler()
    queue = TaskQueue(database, {"check": handler, "repair": handler, "metadata": handler})
    yield queue, handler, database
    queue.stop()
    database.close()


def test_dedupe_preserves_purpose_and_scope(tasks):
    queue, handler, _ = tasks
    first = queue.submit("check", {"artist": 1}, "检查")
    same = queue.submit("check", {"artist": 1}, "检查")
    repair = queue.submit("repair", {"artist": 1}, "补下载")
    metadata = queue.submit("metadata", {"artist": 1}, "刷新信息")
    other = queue.submit("check", {"artist": 2}, "检查")
    assert same["created"] is False and first["operation_id"] == same["operation_id"]
    assert len({op["operation_id"] for op in [first, repair, metadata, other]}) == 4
    assert queue.snapshot()["round"] is None
    assert queue.snapshot()["next_round_count"] == 4


def test_new_submission_does_not_change_frozen_denominator(tasks):
    queue, handler, _ = tasks
    queue.submit("check", {"count": 2}, "本轮")
    observations = []

    def execute(item, context):
        if item.key == "0":
            queue.submit("repair", {"count": 9}, "下一轮")
        observations.append(queue.snapshot())
        return ItemResult()

    handler.on_execute = execute
    assert queue.run_round()
    assert [state["round"]["total"] for state in observations] == [2, 2]
    assert [state["round"]["settled"] for state in observations] == [0, 1]
    state = queue.snapshot()
    assert state["round"]["ratio"] == 1 and state["round"]["outcome"] == "succeeded"
    assert state["next_round_count"] == 1
    handler.on_execute = None
    assert queue.run_round()
    assert queue.snapshot()["round"]["total"] == 9
    assert not queue.run_round()


def test_checking_has_no_fake_total_and_phase_update_is_atomic(tasks):
    queue, handler, _ = tasks
    queue.submit("check", {}, "检查")

    def plan(parameters, context):
        context.report("captions", 7, None, "works", "正在清点")
        state = queue.snapshot()
        assert state["round"]["total"] is None
        assert state["operations"][0]["phase"] == {
            "name": "captions", "done": 7, "total": None, "unit": "works", "detail": "正在清点"}

    handler.on_plan = plan
    queue.run_round()
    operation = queue.snapshot()["operations"][0]
    assert operation["phase"]["name"] == "downloading"
    assert operation["phase"]["unit"] == "pages" and operation["phase"]["total"] == 2


def test_partial_failure_and_unexecuted_dependency_are_not_success(tasks):
    queue, handler, _ = tasks
    submission = queue.submit("check", {}, "测试")
    handler.plan = lambda *_: [ItemSpec("zip", "zip", {}, "downloading"),
        ItemSpec("gif", "gif", {}, "converting", depends_on=("zip",)),
        ItemSpec("other", "page", {}, "downloading")]
    handler.on_execute = lambda item, _: ItemResult("failed", {"message": "下载失败"}) if item.key == "zip" else ItemResult()
    queue.run_round()
    state = queue.snapshot()
    assert state["round"]["ratio"] == 1
    assert state["round"]["outcome"] == "partial_failure"
    assert state["round"]["counts"] == {"failed": 1, "not_executed": 1, "succeeded": 1}
    assert handler.executed == ["zip", "other"]
    assert state["needs_attention_count"] == 1
    queue.acknowledge(submission["operation_id"])
    assert queue.snapshot()["needs_attention_count"] == 0
    assert len(queue.details(submission["operation_id"])["items"]) == 3


def test_empty_manifest_is_no_work_not_failure(tasks):
    queue, handler, _ = tasks
    queue.submit("check", {"count": 0}, "检查")
    queue.run_round()
    assert queue.snapshot()["round"]["outcome"] == "empty"
    assert queue.snapshot()["round"]["ratio"] is None


def test_restart_preserves_total_and_does_not_repeat_settled_items(tasks):
    queue, handler, database = tasks
    submission = queue.submit("check", {"count": 3}, "检查")

    def crash(item, _):
        if item.key == "1":
            raise SystemExit("process death")
        return ItemResult()

    handler.on_execute = crash
    with pytest.raises(SystemExit):
        queue.run_round()
    assert queue.snapshot()["round"]["settled"] == 1
    handler.on_execute = None
    restored = TaskQueue(database, {"check": handler})
    restored.recover()
    restored.run_round()
    assert len(handler.planned) == 1
    assert handler.executed == ["0", "1", "1", "2"]
    details = restored.details(submission["operation_id"])
    assert [item["attempts"] for item in details["items"]] == [1, 2, 1]
    assert restored.snapshot()["round"]["total"] == 3


def test_inventory_failure_is_persistent_and_not_empty_success(tasks):
    queue, handler, _ = tasks
    submission = queue.submit("check", {}, "检查")

    def fail(*_):
        raise ConnectionError("fake token must not appear in results")

    handler.on_plan = fail
    queue.run_round()
    details = queue.details(submission["operation_id"])
    assert details["result"]["inventory_complete"] is False
    assert details["result"]["outcome"] == "failed"
    assert "fake token" not in str(details)


def test_history_cursor_and_phase_records_survive_reopening(tasks):
    queue, handler, database = tasks
    ids = []
    for count in (1, 2, 3):
        ids.append(queue.submit("check", {"count": count}, "history")["operation_id"])
        queue.run_round()
    first = queue.history(limit=2)
    assert [entry["id"] for entry in first["items"]] == ids[::-1][:2]
    second = queue.history(before=first["items"][-1]["id"], limit=2)
    assert [entry["id"] for entry in second["items"]] == ids[:1]
    assert second["has_more"] is False
    restored = TaskQueue(database, {"check": handler})
    snapshots = restored.details(ids[-1])["result"]["phase_snapshots"]
    assert snapshots["downloading:pages"]["done"] == 3
    assert snapshots["downloading:pages"]["total"] == 3


def test_failed_items_can_be_reviewed_beyond_first_page(tasks):
    queue, handler, database = tasks
    operation = queue.submit("check", {"count": 103}, "many failures")
    handler.on_execute = lambda *_: ItemResult("failed", {"message": "synthetic failure"})
    queue.run_round()
    first = queue.details(operation["operation_id"], limit=100, problems_only=True)
    assert first["has_more_items"] and len(first["items"]) == 100
    second = queue.details(operation["operation_id"], offset=100, problems_only=True)
    assert len(second["items"]) == 3 and not second["has_more_items"]
