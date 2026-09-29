"""Read-only log projection and artifact identity use synthetic data only."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import ArchiveDatabase
from src.core.models import Operation, OperationItem
from src.core.tasks import TaskQueue
from src.routes.settings_api import create_settings_router
from src.version import EXE_NAME, __build__, __version__


def test_logs_keep_failure_reason_without_exposing_private_payloads(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "synthetic.db")
    private = "synthetic-private-token-do-not-show"
    with database.write() as session:
        operation = Operation(kind="verify", label=private, dedupe_key="one", state="finished",
                              phase="verifying", phase_unit="pages", phase_done=2, phase_total=2,
                              parameters={"token": private}, detail=private,
                              result={"outcome": "partial_failure", "token": private}, needs_attention=True)
        session.add(operation)
        session.flush()
        session.add_all([
            OperationItem(operation_id=operation.id, item_key="one", kind="test", target={"token": private},
                          phase="verifying", state="failed", result={"code": "ReadTimeout", "message": private}),
            OperationItem(operation_id=operation.id, item_key="two", position=1, kind="test", target={},
                          phase="verifying", state="failed", result={"message": "原件缺失或损坏，可选择补下载"}),
            OperationItem(operation_id=operation.id, item_key="three", position=2, kind="test", target={},
                          phase="verifying", state="failed", result={"code": "PermissionError", "message": "此项未完成，请重试"})])
    runtime = SimpleNamespace(database=database, queue=TaskQueue(database, {}), client=None)
    app = FastAPI()
    app.include_router(create_settings_router(lambda: runtime))
    with TestClient(app) as client:
        response = client.get("/api/logs")
        assert response.status_code == 200
        assert private not in response.text
        assert "部分失败" in response.text and "2 / 2 页" in response.text
        assert "读取主站回应超时" in response.text
        assert "原件缺失或损坏" in response.text
        assert "没有文件访问权限" in response.text
        assert client.get("/api/logs").json() == response.json()
        settings = client.get("/api/settings").json()
        assert settings["version"] == __version__ == "0.2.0"
        assert settings["build"] == __build__ and __build__ in EXE_NAME
    with database.read() as session:
        assert session.query(Operation).count() == 1
        assert session.query(OperationItem).count() == 3
    database.close()


def test_log_is_bounded_and_empty_archive_is_usable(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "bounded.db")
    runtime = SimpleNamespace(database=database)
    app = FastAPI()
    app.include_router(create_settings_router(lambda: runtime))
    with TestClient(app) as client:
        assert client.get("/api/logs").json() == {"lines": ["暂无后台处理记录。"], "truncated": False}
        with database.write() as session:
            session.add_all([Operation(kind="check", dedupe_key=str(i), label="check", state="queued") for i in range(110)])
        result = client.get("/api/logs").json()
        assert len(result["lines"]) == 100 and result["truncated"] is True
    database.close()
