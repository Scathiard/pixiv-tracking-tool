"""History reasons are read after ranking, without dropping historical rows."""
import sqlite3

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.core.archive_state import _operation_window, raw_work_states as work_states
from src.core.models import ApplicationState, Artist, Asset, Base, Derivative, Operation, OperationItem, Work
from src.core.task_indexes import create_task_indexes


def test_reason_hydration_is_bounded_to_winners_and_preserves_history():
    engine = create_engine("sqlite:///:memory:")
    native = sqlite3.connect(":memory:")
    Base.metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            create_task_indexes(connection)
        with Session(engine) as session:
            session.add(Artist(id=1, pixiv_user_id="synthetic", name="synthetic"))
            works = [Work(id=index, artist_id=1, pixiv_illust_id=str(index), title="synthetic")
                     for index in range(1, 11)]
            session.add_all(works)
            for index in range(30):
                operation = Operation(id=f"history-{index:03}", kind="verify", dedupe_key=str(index),
                                      label="synthetic", state="finished", parameters={"work_ids": list(range(1, 5001))})
                operation.items = [OperationItem(id=f"item-{index:03}-{work.id}", item_key=str(work.id),
                    kind="verify", target={"work_id": work.id}, phase="verifying", state="failed",
                    result={"message": f"reason-{index}", "padding": "x" * 65536}) for work in works]
                session.add(operation)
            session.commit()
            calls, scope_calls = [], []
            raw = session.connection().connection.driver_connection
            def json_extract(value, *paths):
                if any("message" in path for path in paths):
                    calls.append(paths)
                placeholders = ",".join("?" for _ in (value, *paths))
                return native.execute(f"SELECT json_extract({placeholders})", (value, *paths)).fetchone()[0]
            def json_type(value, *paths):
                if "$.work_ids" in paths:
                    scope_calls.append(paths)
                placeholders = ",".join("?" for _ in (value, *paths))
                return native.execute(f"SELECT json_type({placeholders})", (value, *paths)).fetchone()[0]
            raw.create_function("json_extract", -1, json_extract, deterministic=True)
            raw.create_function("json_type", -1, json_type, deterministic=True)
            states, _ = work_states(session, works)
            assert len(calls) == 10
            assert len(scope_calls) <= 1
            assert all(value["processing"]["reason"] == "reason-29" for value in states.values())
            calls.clear(); scope_calls.clear()
            rows = session.execute(select(_operation_window([work.id for work in works]))).all()
            assert len(rows) == 300
            assert len(calls) == 300
            assert {row.reason for row in rows} == {f"reason-{index}" for index in range(30)}
            assert len(scope_calls) >= 30
            for work in works:
                session.add(Asset(id=f"asset-{work.id}", work_id=work.id, role="image", page_index=0,
                    state="verified", relative_path=f"synthetic/{work.id}.png"))
                session.add(Derivative(id=f"thumbnail-{work.id}", work_id=work.id, kind="thumbnail",
                    page_index=0, spec_key="v1", state="verified", relative_path=f"synthetic/{work.id}.webp"))
            session.commit()
            calls.clear(); scope_calls.clear()
            states, _ = work_states(session, works)
            assert calls == [] and scope_calls == []
            assert all(value["processing"]["state"] == "idle" and value["recovery"] is None
                       for value in states.values())
            session.add(ApplicationState(key="maintenance_work:1", value={"status": "ready", "kind": "verify"}))
            works[1].source_metadata = {"source_updates": {"metadata": {"reason": "synthetic update"}}}
            session.commit()
            calls.clear(); scope_calls.clear()
            states, _ = work_states(session, works)
            assert len(calls) == 2 and len(scope_calls) <= 1
            assert states[1]["recovery"]["kind"] == "verify"
            assert states[2]["processing"]["reason"] == "reason-29"
            from datetime import datetime
            session.add(Operation(id="active-old", kind="verify", dedupe_key="active-old", label="synthetic",
                state="running", parameters={}, created_at=datetime(2000, 1, 1), updated_at=datetime(2000, 1, 1),
                items=[OperationItem(id="active-old-item", item_key="one", kind="verify",
                    target={"work_id": 3}, phase="verifying", state="running")]))
            session.commit()
            calls.clear(); scope_calls.clear()
            states, _ = work_states(session, works)
            assert len(calls) == 10
            assert len(scope_calls) <= 3
            assert states[3]["processing"]["state"] == "running"
            assert states[3]["processing"]["operation_id"] == "active-old"
    finally:
        engine.dispose()
        native.close()
