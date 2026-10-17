"""Small synthetic HTTP checks for the settings/progress read boundary."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event

from src.core.database import ArchiveDatabase
from src.core.models import Artist, Operation, utcnow
from src.core.tasks import TaskQueue
from src.core import task_read_projection
from src.routes.archive_api import create_router
from src.routes.settings_api import create_settings_router


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import src.routes.settings_api as settings
    monkeypatch.setattr(settings, "lan_addresses", lambda port: [])
    monkeypatch.setattr(settings, "get_pending_login_url", lambda: None)
    database = ArchiveDatabase.create(tmp_path / "read-path.db")
    queue = TaskQueue(database, {})
    runtime = SimpleNamespace(database=database, queue=queue, client=None,
        recovery_issues=lambda: [])
    app = FastAPI()
    app.include_router(create_settings_router(lambda: runtime))
    app.include_router(create_router(lambda: runtime))
    with TestClient(app, raise_server_exceptions=False) as client:
        yield database, queue, client
    queue.stop()
    database.close()


def test_statistics_failure_cannot_block_settings_read_or_save(isolated):
    database, queue, client = isolated
    def reject_statistics(conn, cursor, statement, params, context, many):
        if "count(*)" in statement.lower() or "downloaded_pages" in statement:
            raise RuntimeError("synthetic statistics failure")
    event.listen(database.engine, "before_cursor_execute", reject_statistics)
    try:
        config = client.get("/api/settings")
        assert config.status_code == 200 and "stats" not in config.json()
        saved = client.patch("/api/settings", json={"automatic_paused": True})
        assert saved.status_code == 200 and saved.json()["ok"] is True
        assert saved.json()["automatic_paused"] is True and "stats" not in saved.json()
        assert queue.automatic_paused() is True
        assert client.get("/api/settings/stats").status_code == 500
        assert client.get("/api/settings").json()["automatic_paused"] is True
    finally:
        event.remove(database.engine, "before_cursor_execute", reject_statistics)


def test_statistics_short_cache_survives_writes_and_refresh_is_single_flight(isolated):
    database, queue, client = isolated
    first = client.get("/api/settings/stats").json()
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
    # A background change does not trigger another expensive computation.
    assert client.get("/api/settings/stats").json() == first
    entered, release = Event(), Event()
    computations = []
    def hold_totals(conn, cursor, statement, params, context, many):
        if "downloaded_pages" in statement:
            computations.append(statement)
            entered.set()
            assert release.wait(3)
    event.listen(database.engine, "before_cursor_execute", hold_totals)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            one = executor.submit(client.get, "/api/settings/stats?refresh=true")
            assert entered.wait(3)
            assert database.reads.active["frontend"] == 0
            database.reads.advance_generation()  # Different coalescing key.
            two = executor.submit(client.get, "/api/settings/stats?refresh=true")
            assert client.get("/api/settings").status_code == 200
            assert client.patch("/api/settings", json={"automatic_paused": True}).status_code == 200
            release.set()
            values = [one.result().json(), two.result().json()]
        assert len(computations) == 1
        assert values[0] == values[1]
        assert values[0]["stats"]["artists"] == 1
        assert values[0]["calculated_at"]
    finally:
        release.set()
        event.remove(database.engine, "before_cursor_execute", hold_totals)


def test_progress_keeps_watched_parallel_completions_without_loading_history(isolated, monkeypatch):
    database, queue, client = isolated
    now = utcnow()
    with database.write() as session:
        for index in range(25):
            session.add(Operation(id=f"finished-{index}", kind="check", label="synthetic",
                dedupe_key=f"done-{index}", state="finished", finished_at=now + timedelta(seconds=index),
                needs_attention=index == 1,
                result={"outcome": "partial_failure" if index == 1 else "succeeded",
                    "counts": {"failed": int(index == 1), "succeeded": 1},
                    "unrelated_manifest": ["synthetic-private-payload"] * 1000}))
        for index in range(2):
            session.add(Operation(id=f"active-{index}", kind="check", label="parallel",
                dedupe_key=f"active-{index}", state="running"))
    def reject_history(*args, **kwargs):
        raise AssertionError("progress must not read full history")
    monkeypatch.setattr(task_read_projection, "history_head", reject_history)
    response = client.get("/api/progress?watch_ids=finished-0,finished-2,active-0")
    assert response.status_code == 200, response.text
    value = response.json()
    by_id = {operation["id"]: operation for operation in value["operations"]}
    assert set(by_id) == {"active-0", "active-1", "finished-0", "finished-1", "finished-2", "finished-24"}
    assert value["active_operation_count"] == 2 and value["needs_attention_count"] == 1
    assert by_id["finished-1"]["result"]["outcome"] == "partial_failure"
    assert by_id["finished-1"]["result"]["counts"]["failed"] == 1
    assert "unrelated_manifest" not in response.text and "synthetic-private-payload" not in response.text
    assert client.get("/api/progress", params={"watch_ids": ",".join(str(i) for i in range(65))}).status_code == 422
