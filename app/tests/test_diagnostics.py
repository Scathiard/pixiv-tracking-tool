"""Small observer checks: privacy, pending work, cancellation and real SQLite."""
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from src.core import diagnostics
from src.core.database import ArchiveDatabase


@pytest.fixture
def capture(tmp_path):
    path = diagnostics.start(tmp_path, build="synthetic", version="0.2.0", interval=60)
    assert path is not None
    yield path
    diagnostics.stop()


def records(path):
    diagnostics.stop()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_pending_sql_and_errors_are_correlated_without_payloads(capture):
    class BusinessCancellation(BaseException):
        pass
    with pytest.raises(BusinessCancellation):
        with diagnostics.span("synthetic.operation", state="before") as observation:
            diagnostics.emit("synthetic.private", refresh_token="SECRET_TOKEN", body="PRIVATE_BODY",
                             endpoint="https://example.invalid/private?token=HIDDEN")
            token = diagnostics.sql_start(object(), "/* JOIN COMMENT_SECRET */ SELECT 'FROM SQL_SECRET' FROM synthetic WHERE id=?")
            diagnostics._recorder.sample()
            diagnostics.sql_finish(token, error=RuntimeError("ERROR_SECRET"))
            observation["state"] = "after"
            raise BusinessCancellation("EXCEPTION_SECRET")
    rows = records(capture)
    encoded = json.dumps(rows)
    for secret in ("SECRET_TOKEN", "PRIVATE_BODY", "SQL_SECRET", "COMMENT_SECRET", "ERROR_SECRET", "EXCEPTION_SECRET", "HIDDEN"):
        assert secret not in encoded
    assert any(row["event"] == "watch.active" and any(span["event"] == "sql" for span in row["spans"]) for row in rows)
    ending = next(row for row in rows if row["event"] == "synthetic.operation.end")
    assert ending["error_type"] == "BusinessCancellation" and ending["state"] == "after"
    assert any(row["event"] == "sql.aggregate" and row["count"] == 1 and row["errors"] == 1 for row in rows)


def test_sqlite_commit_rollback_and_http_keep_behavior(capture, tmp_path):
    database = ArchiveDatabase.create(tmp_path / "synthetic.db")
    try:
        with pytest.raises(ValueError, match="business failure"):
            with database.write() as session:
                session.execute(text("INSERT INTO application_state(key,value) VALUES('synthetic', '{}')"))
                raise ValueError("business failure")
        with database.read() as session:
            assert session.scalar(text("SELECT count(*) FROM application_state WHERE key='synthetic'")) == 0
        with database.write() as session:
            session.execute(text("INSERT INTO application_state(key,value) VALUES('synthetic', '{}')"))
        app = FastAPI()
        app.add_middleware(diagnostics.DiagnosticHTTPMiddleware)
        @app.get("/api/works/{work_id}")
        def endpoint(work_id: int):
            with database.read() as session:
                # A slow result-consumption body remains individually correlated;
                # normal fast read phases now retain aggregate evidence instead.
                time.sleep(.03)
                return {"count": session.scalar(text("SELECT count(*) FROM application_state WHERE key='synthetic'"))}
        with TestClient(app) as client:
            response = client.get("/api/works/987654321?private=QUERY_SECRET", headers={"Authorization": "Bearer HEADER_SECRET"})
            assert response.status_code == 200 and response.json() == {"count": 1}
    finally:
        database.close()
    rows = records(capture)
    encoded = json.dumps(rows)
    assert "987654321" not in encoded and "QUERY_SECRET" not in encoded and "HEADER_SECRET" not in encoded
    assert any(row["event"] == "db.write.rollback.end" for row in rows)
    assert any(row["event"] == "db.write.commit.end" for row in rows)
    http = next(row for row in rows if row["event"] == "http.request.end")
    assert http["route"] == "/api/works/{work_id}" and http["status"] == 200 and http["bytes_sent"] > 0
    assert any(row["event"] == "db.read.body.end" and row["trace"] == http["trace"] for row in rows)


def test_capture_rotates_and_keeps_recording_with_existing_files_intact(tmp_path):
    old = tmp_path / "existing.jsonl"
    old.write_text("keep", encoding="utf-8")
    path = diagnostics.start(tmp_path, build="synthetic", version="0.2.0", interval=60, max_bytes=1400)
    recorder = diagnostics._recorder
    with diagnostics.span("synthetic.cross_volume"):
        for number in range(20):
            diagnostics.emit("synthetic.extra", number=number, padding="x" * 160)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(list(tmp_path.glob("diagnostic-*.jsonl"))) < 3:
            time.sleep(.02)
        assert diagnostics.enabled() and recorder.writer.is_alive()
        assert len(list(tmp_path.glob("diagnostic-*.jsonl"))) >= 3
        diagnostics.emit("synthetic.after_rotation")
    diagnostics.stop()
    volumes = sorted(tmp_path.glob("diagnostic-*.jsonl"))
    rows = [json.loads(line) for volume in volumes for line in volume.read_text(encoding="utf-8").splitlines()]
    assert not any(row["event"] == "diagnostic.limit_reached" for row in rows)
    assert sorted(row["number"] for row in rows if row["event"] == "synthetic.extra") == list(range(20))
    assert any(row["event"] == "synthetic.after_rotation" for row in rows)
    assert any(row["event"] == "diagnostic.stop" for row in rows)
    sequences = [row["seq"] for row in rows if "seq" in row]
    assert sequences == list(range(1, len(sequences) + 1))
    assert len(sequences) == len(set(sequences))
    beginning = next(row for row in rows if row["event"] == "synthetic.cross_volume.begin")
    ending = next(row for row in rows if row["event"] == "synthetic.cross_volume.end")
    assert beginning["trace"] == ending["trace"]
    assert all("utc" in row for row in rows)
    assert any(row["event"] == "diagnostic.part_start" for row in rows)
    assert all(volume.read_bytes().endswith(b"\n") for volume in volumes)
    assert path.exists()
    assert old.read_text(encoding="utf-8") == "keep"


def test_observer_failure_does_not_replace_business_error_or_prevent_stop(capture, monkeypatch):
    recorder = diagnostics._recorder
    def fail(*args, **kwargs):
        raise RuntimeError("observer failure")
    with monkeypatch.context() as patch:
        patch.setattr(diagnostics, "_clean", fail)
        with pytest.raises(ValueError, match="business failure"):
            with diagnostics.span("synthetic.fault"):
                raise ValueError("business failure")
    monkeypatch.setattr(recorder, "sample", fail)
    diagnostics.stop()
    assert recorder.stop_event.is_set()
    assert not recorder.writer.is_alive() and not recorder.watcher.is_alive()


def test_rotation_write_failure_is_visible_without_leaking_exception_text(tmp_path, monkeypatch, capsys):
    original_open = type(tmp_path).open
    def fail_second_part(path, *args, **kwargs):
        if path.name.endswith("part0002.jsonl"):
            raise OSError("PRIVATE_DISK_DETAIL")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(type(tmp_path), "open", fail_second_part)
    diagnostics.start(tmp_path, build="synthetic", version="0.2.0", interval=60, max_bytes=900)
    recorder = diagnostics._recorder
    for number in range(8):
        diagnostics.emit("synthetic.extra", number=number, padding="x" * 160)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not recorder.failed:
        time.sleep(.02)
    diagnostics.stop()
    assert recorder.failed and not recorder.writer.is_alive()
    assert not recorder.watcher.is_alive()
    warning = capsys.readouterr().err
    assert "OSError" in warning and "记录已停止" in warning
    assert "PRIVATE_DISK_DETAIL" not in warning
