"""Bounded log work, resumable sparse errors, and narrow safe projections."""
import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.core.activity_log import processing_log
from src.core.models import Base, Operation, OperationItem


class MemoryLog:
    def __init__(self):
        self.engine = create_engine("sqlite://", poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.vm = 0

        def progress():
            self.vm += 100
            return 0

        connection = self.engine.raw_connection()
        try:
            connection.driver_connection.set_progress_handler(progress, 100)
        finally:
            connection.close()

    @contextmanager
    def read(self):
        with Session(self.engine) as session:
            yield session


def seed(database, count, state):
    identity = uuid4().hex
    with database.engine.begin() as connection:
        connection.execute(insert(Operation), [{"id": identity, "kind": "reconcile", "dedupe_key": identity,
            "label": "synthetic", "state": "finished", "phase": "verifying", "phase_done": count,
            "phase_total": count, "parameters": {"private": "SECRET" * 500}}])
        for start in range(0, count, 2000):
            connection.execute(insert(OperationItem), [{"operation_id": identity, "id": uuid4().hex,
                "item_key": str(index), "position": index, "kind": "verify", "phase": "verifying",
                "state": state, "lane": (None, "check", "local", "download")[index % 4],
                "target": {"private": "SECRET" * 400}, "depends_on": [],
                "result": {"code": "ReadTimeout"} if state == "failed" else {}}
                for index in range(start, min(count, start + 2000))])
    return identity


@pytest.mark.parametrize("state", ["failed", "skipped"])
def test_dense_150512_steps_have_constant_first_window_work(state, tmp_path):
    database = MemoryLog()
    try:
        seed(database, 150512, state)
        database.vm = 0
        first = processing_log(database)
        assert database.vm < 100000  # Input history is 150k rows, never scanned to fill 200 lines.
        first_vm = database.vm
        assert first["details_complete"] is False and first["next_cursor"]
        assert first["truncated"] is False  # All task summaries are present; only details continue.
        assert "SECRET" not in json.dumps(first)
        if state == "failed":
            assert len(first["lines"]) == 201
            assert first["lines"][1].startswith("  第 1 项")
            assert first["lines"][-1].startswith("  第 200 项")
        else:
            assert len(first["lines"]) == 1  # Scanned ordinary skips still give a continuation.
        database.vm = 0
        second = processing_log(database, cursor=first["next_cursor"])
        assert database.vm < 100000
        (tmp_path / "log-window-budget.json").write_text(json.dumps({"steps": 150512, "state": state,
            "target_bytes_min": 2400, "first_vm_steps": first_vm, "second_vm_steps": database.vm,
            "first_lines": len(first["lines"]), "second_lines": len(second["lines"]),
            "private_payload_returned": "SECRET" in json.dumps((first, second))}, indent=2), encoding="utf-8")
        if state == "failed":
            assert second["lines"][1].startswith("  第 201 项")
    finally:
        database.engine.dispose()


def test_sparse_skipped_reason_is_reachable_without_unbounded_search():
    database = MemoryLog()
    try:
        identity = seed(database, 501, "skipped")
        with Session(database.engine) as session:
            row = session.query(OperationItem).filter_by(operation_id=identity, position=500).one()
            row.result = {"failure_code": "work_unavailable", "message": "SECRET"}
            session.commit()
        pages = [processing_log(database)]
        while pages[-1]["next_cursor"]:
            pages.append(processing_log(database, cursor=pages[-1]["next_cursor"]))
        assert len(pages) == 3 and pages[-1]["details_complete"]
        assert all(not page["truncated"] for page in pages)
        lines = "\n".join(line for page in pages for line in page["lines"])
        assert "第 501 项" in lines and "未确认删除" in lines and "SECRET" not in lines
    finally:
        database.engine.dispose()


@pytest.mark.parametrize("value", ["???", "W10=", "e30=", "A" * 16001])
def test_invalid_cursor_is_a_safe_input_error(value):
    database = MemoryLog()
    try:
        with pytest.raises(ValueError, match="日志读取位置无效"):
            processing_log(database, cursor=value)
    finally:
        database.engine.dispose()
