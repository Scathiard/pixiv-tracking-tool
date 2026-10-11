"""Bounded automatic inventory; synthetic records only, without remote calls."""
from datetime import timedelta
import re

import pytest
from sqlalchemy import event, select

from src.core.maintenance import LEDGER_PREFIX, SCAN_WINDOW
from src.core.models import ApplicationState, Asset, Derivative, Operation
from test_missing_media_maintenance import archive


def complete(database):
    with database.write() as session:
        for asset in session.scalars(select(Asset)):
            asset.state, asset.relative_path = "verified", "synthetic.png"
            session.add(Derivative(work_id=asset.work_id, kind="thumbnail", page_index=0,
                                   spec_key="v1", state="verified", relative_path="thumb.webp"))


def test_empty_windows_advance_across_inspected_ids(tmp_path):
    database, _, _, _, maintenance, _ = archive(tmp_path, count=SCAN_WINDOW * 2 + 1)
    try:
        complete(database)
        assert maintenance.tick(force=True) == []
        assert set(maintenance._cursors.values()) == {SCAN_WINDOW + 2}
        assert maintenance.tick(force=True) == []
        assert set(maintenance._cursors.values()) == {2}
        assert maintenance.tick(force=True) == []
        assert set(maintenance._cursors.values()) == {1}
        assert maintenance.tick(force=True) == []
        assert set(maintenance._cursors.values()) == {SCAN_WINDOW + 2}
    finally:
        database.close()


def test_due_work_behind_waiting_window_and_cooldown_is_revisited(tmp_path):
    database, _, queue, _, maintenance, clock = archive(tmp_path, count=SCAN_WINDOW + 2)
    try:
        future = (clock[0] + timedelta(days=1)).isoformat()
        with database.write() as session:
            for work_id in range(3, SCAN_WINDOW + 3):
                session.add(ApplicationState(key=LEDGER_PREFIX + str(work_id), value={
                    "tracks": {"remote": {"status": "waiting", "next_retry_at": future}}}))
        assert maintenance.tick(force=True) == []
        assert maintenance._cursors["remote"] == 3
        maintenance._next_remote = 60
        assert maintenance.tick(force=True) == []
        assert maintenance._cursors["remote"] == 3
        maintenance._next_remote = 0
        result = maintenance.tick(force=True)
        with database.read() as session:
            assert session.get(Operation, result[0]["operation_id"]).parameters == {"work_ids": [2]}
        assert maintenance._cursors["remote"] == 2  # pending ID 1 remains in the next window
        queue.run_round()
        clock[0] += timedelta(days=2)
        maintenance._next_remote = 0
        result = maintenance.tick(force=True)
        with database.read() as session:
            assert session.get(Operation, result[0]["operation_id"]).parameters == {"work_ids": [1]}
        queue.run_round()
        maintenance._next_remote = 0
        result = maintenance.tick(force=True)
        with database.read() as session:
            assert session.get(Operation, result[0]["operation_id"]).parameters == {"work_ids": [SCAN_WINDOW + 2]}
    finally:
        database.close()


@pytest.mark.parametrize("failure", ["submit", "ledger"])
def test_failed_submission_or_ledger_commit_preserves_cursors(tmp_path, monkeypatch, failure):
    database, _, queue, _, maintenance, _ = archive(tmp_path, count=2)
    installed = False
    try:
        previous = dict(maintenance._cursors)
        original = queue.submit

        def fail_commit(connection):
            raise RuntimeError("synthetic ledger transaction failure")

        def fail_submit(*args, **kwargs):
            nonlocal installed
            if failure == "submit":
                raise RuntimeError("synthetic submit failure")
            result = original(*args, **kwargs)
            event.listen(database.writer_engine, "commit", fail_commit)
            installed = True
            return result

        monkeypatch.setattr(queue, "submit", fail_submit)
        with pytest.raises(RuntimeError, match="synthetic"):
            maintenance.tick(force=True)
        assert maintenance._cursors == previous
        assert maintenance._next_remote == 0
        with database.read() as session:
            assert session.get(ApplicationState, LEDGER_PREFIX + "2") is None
    finally:
        if installed:
            event.remove(database.writer_engine, "commit", fail_commit)
        database.close()


def test_gap_detection_searches_only_primary_key_window(tmp_path):
    database, _, _, _, maintenance, _ = archive(tmp_path, count=SCAN_WINDOW * 4)
    try:
        complete(database)
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("SELECT") and "FROM works" in statement:
                statements.append((statement, parameters))

        event.listen(database.engine, "before_cursor_execute", capture)
        try:
            assert maintenance._candidates()[:3] == ([], [], set())
        finally:
            event.remove(database.engine, "before_cursor_execute", capture)
        bounded = [(sql, params) for sql, params in statements if "works.id IN (" in sql]
        assert len(bounded) == 6
        with database.engine.connect() as connection:
            for sql, params in bounded:
                plan = [row[3] for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params)]
                assert any("SEARCH works USING INTEGER PRIMARY KEY" in line for line in plan), plan
                assert not any(line.startswith("SCAN works") for line in plan), plan
                assert len(params) < SCAN_WINDOW + 45
                projection = sql.split("\nFROM works", 1)[0]
                assert not re.search(r"(?:SELECT(?: DISTINCT)?|,)\s*works\.source_metadata(?:\s+AS\s+\w+)?(?:,|\s*$)", projection)
    finally:
        database.close()
