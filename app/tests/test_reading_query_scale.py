"""Isolated reading query budgets and authorisation semantics, no real library."""
import json
import os
from pathlib import Path
import time
import pytest

from sqlalchemy import event, select

from src.core.archive_state import _operation_window
from src.core.models import ArchiveIntent, Artist, Asset, Derivative, LocalPreference, Operation, OperationItem, Work, WorkRevision
from src.core.runtime import Runtime


def test_reading_queries_do_not_expand_unrelated_history(tmp_path):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    evidence = {}
    try:
        with runtime.database.write() as session:
            session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
            session.add_all([Work(id=i, pixiv_illust_id=str(i), artist_id=1, title="synthetic", type="illust", page_count=1) for i in range(1, 481)])
            session.flush()
            session.add(Operation(id="history", kind="verify", dedupe_key="history", label="synthetic", state="finished", parameters={"work_ids": list(range(1, 481))}))
            session.flush()
            session.execute(OperationItem.__table__.insert(), [dict(id=f"item-{i}", operation_id="history", item_key=str(i), kind="verify", phase="verifying", target={}, state="failed", result={"message": "synthetic failure"}, position=i) for i in range(5000)])
            session.execute(Asset.__table__.insert(), [dict(id=f"asset-{i}", work_id=2, role="image", page_index=i, state="verified", relative_path=f"unrelated/{i}.png") for i in range(10000)])
            session.add(Asset(id="target", work_id=1, role="image", page_index=0, state="verified", relative_path="1/1_p0.png"))
        file = runtime.images / "1/1_p0.png"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"synthetic")
        with runtime.database.read() as session:
            statement = select(_operation_window([1]))
            sql = str(statement.compile(session.bind, compile_kwargs={"literal_binds": True}))
            connection = session.connection().connection.driver_connection
            evidence["operation_plan"] = connection.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
            ticks = [0]
            def tick():
                ticks[0] += 1
                return 0
            connection.set_progress_handler(tick, 1000)
            started = time.perf_counter()
            rows = session.execute(statement).all()
            evidence.update(operation_seconds=time.perf_counter()-started, operation_vm_steps=ticks[0]*1000, operation_rows=len(rows))
            connection.set_progress_handler(None, 0)
            ticks[0] = 0
            connection.set_progress_handler(tick, 1000)
            started = time.perf_counter()
            window_rows = session.execute(select(_operation_window(list(range(1, 481))))).all()
            evidence.update(window480_seconds=time.perf_counter()-started, window480_vm_steps=ticks[0]*1000, window480_rows=len(window_rows))
            connection.set_progress_handler(None, 0)
        captured = []
        ticks = [0]
        media_connections = set()
        def record(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                captured.append((statement, parameters))
                raw = conn.connection.driver_connection
                raw.set_progress_handler(tick, 1000)
                media_connections.add(raw)
        event.listen(runtime.database.engine, "before_cursor_execute", record)
        started = time.perf_counter()
        assert runtime.media_path("1/1_p0.png") == file
        evidence.update(media_seconds=time.perf_counter()-started, media_vm_steps=ticks[0]*1000)
        for raw in media_connections:
            raw.set_progress_handler(None, 0)
        event.remove(runtime.database.engine, "before_cursor_execute", record)
        with runtime.database.engine.connect() as connection:
            evidence["media_plans"] = [(sql, connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, parameters).all()) for sql, parameters in captured]
        artifact = os.environ.get("READING_QUERY_ARTIFACTS")
        if artifact:
            Path(artifact).mkdir(parents=True, exist_ok=True)
            Path(artifact, "query-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=list), encoding="utf-8")
        assert evidence["operation_vm_steps"] < 400000, evidence
        assert evidence["window480_rows"] == 480, evidence
        assert evidence["window480_vm_steps"] < 2000000, evidence
        assert evidence["media_vm_steps"] < 10000, evidence
    finally:
        runtime.close()


@pytest.mark.parametrize("operation_state,states,expected", [
    ("running", ("failed", "pending", "running"), "running"),
    ("running", ("failed", "pending", "done"), "pending"),
    ("finished", ("done", "failed", "not_executed"), "failed"),
])
def test_scoped_history_reduction_preserves_winner(tmp_path, operation_state, states, expected):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    try:
        with runtime.database.write() as session:
            session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
            session.add(Work(id=1, pixiv_illust_id="1", artist_id=1, title="synthetic"))
            session.add(Operation(id="op", kind="repair", dedupe_key="op", label="synthetic", state=operation_state, parameters={"work_ids": [1]}))
            session.flush()
            session.add_all([OperationItem(id=f"item-{i}", operation_id="op", item_key=str(i), kind="error", phase="checking", target={}, state=state, position=i, result={"message": state}) for i, state in enumerate(states)])
        with runtime.database.read() as session:
            ranked = _operation_window([1])
            row = session.execute(select(ranked).where(ranked.c.rank == 1)).one()
            assert (row.item_state, row.reason) == (expected, expected)
    finally:
        runtime.close()


def test_media_hint_is_not_permission_and_historical_paths_stay_readable(tmp_path):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    try:
        paths = ["artist/1/revisions/old/1_p0.png", "artist/.thumbnails/1_p0_v1.webp", "arbitrary-old-name.png", "artist/2_p0.png", "artist/1_p4.png", ".profiles/1/avatar.webp"]
        for relative in paths:
            path = runtime.images / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic")
        with runtime.database.write() as session:
            session.add(Artist(id=1, pixiv_user_id="1", name="synthetic", avatar_path=paths[-1]))
            session.add_all([Work(id=1, pixiv_illust_id="1", artist_id=1, title="synthetic"), Work(id=2, pixiv_illust_id="2", artist_id=1, title="synthetic")])
            session.flush()
            session.add_all([WorkRevision(id="old", work_id=1, ordinal=1, title="old", type="illust", page_count=1, source_signature="old"), WorkRevision(id="new", work_id=1, ordinal=2, title="new", type="illust", page_count=1, source_signature="new")])
            session.flush()
            session.get(Work, 1).default_revision_id = "new"
            session.get(Work, 1).latest_revision_id = "new"
            session.add(LocalPreference(work_id=1, is_hidden=True))
            session.add_all([Asset(id="old", work_id=1, revision_id="old", role="image", page_index=0, relative_path=paths[0]),
                Asset(id="arbitrary", work_id=1, role="image", page_index=0, relative_path=paths[2]),
                Asset(id="mismatch", work_id=1, role="image", page_index=1, relative_path=paths[3]),
                Derivative(id="thumb", work_id=1, kind="thumbnail", relative_path=paths[1])])
        for relative in paths[:4] + paths[-1:]:
            assert runtime.media_path(relative) == runtime.images / relative
        assert runtime.media_path(paths[4]) is None
        assert runtime.media_path("../outside.png") is None
        assert runtime.media_path(".staging/not-registered.png") is None
        with runtime.database.write() as session:
            session.add(ArchiveIntent(pixiv_illust_id="1", mode="suppressed"))
        for relative in paths[:4]:
            assert runtime.media_path(relative) is None
        assert runtime.media_path(paths[-1]) == runtime.images / paths[-1]
        with runtime.database.write() as session:
            session.add(Asset(id="shared", work_id=2, role="image", page_index=0, relative_path=paths[0]))
        assert runtime.media_path(paths[0]) == runtime.images / paths[0]
    finally:
        runtime.close()
