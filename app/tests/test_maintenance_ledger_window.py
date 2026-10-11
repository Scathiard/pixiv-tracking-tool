"""Maintenance examines bounded indexed keys, not every waiting ledger."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from src.core.database import ArchiveDatabase
from src.core.maintenance import LEDGER_PREFIX, SCAN_WINDOW, MissingMediaMaintenance
from src.core.models import ApplicationState, Operation, utcnow


def create_maintenance(tmp_path, count):
    database = ArchiveDatabase.create(tmp_path / "ledgers.db")
    with database.write() as session:
        session.add(Operation(id="done", kind="repair", dedupe_key="done", label="synthetic",
                              state="finished", result={"outcome": "succeeded"}))
        for index in range(count):
            session.add(ApplicationState(key=f"{LEDGER_PREFIX}{index + 1:04}", value={
                "status": "queued", "tracks": {"remote": {
                    "status": "queued", "kind": "repair", "operation_id": "done"}}}))
        session.add(ApplicationState(key="unrelated-large", value={"payload": "x" * 200_000}))
    maintenance = MissingMediaMaintenance(database, SimpleNamespace(), remote_available=lambda: False)
    return database, maintenance


def values(database):
    with database.read() as session:
        return {row[0]: row[1] for row in session.connection().exec_driver_sql(
            "SELECT key,json_extract(value,'$.status') FROM application_state WHERE key>=? AND key<? ORDER BY key",
            (LEDGER_PREFIX, LEDGER_PREFIX[:-1] + ";"))}


def test_ledger_pages_advance_and_wrap_without_starving_later_keys(tmp_path):
    database, maintenance = create_maintenance(tmp_path, SCAN_WINDOW + 1)
    statements = []
    def capture(connection, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT") and "application_state" in statement:
            statements.append(statement)
    event.listen(database.writer_engine, "before_cursor_execute", capture)
    try:
        maintenance._reconcile(utcnow())
        snapshot = values(database)
        assert sum(value == "ready" for value in snapshot.values()) == SCAN_WINDOW
        assert snapshot[f"{LEDGER_PREFIX}{SCAN_WINDOW + 1:04}"] == "queued"
        assert "LIKE" not in statements[0].upper()
        assert "LIMIT" in statements[0].upper()
        maintenance._reconcile(utcnow())
        assert set(values(database).values()) == {"ready"}
        # A previously visited key changing again is found after reaching the end.
        with database.write() as session:
            setting = session.get(ApplicationState, f"{LEDGER_PREFIX}0001")
            setting.value = {"status": "queued", "kind": "repair", "operation_id": "done"}
        maintenance._reconcile(utcnow())
        assert values(database)[f"{LEDGER_PREFIX}0001"] == "ready"
    finally:
        event.remove(database.writer_engine, "before_cursor_execute", capture)
        database.close()


def test_failed_ledger_transaction_keeps_cursor_and_retryable_records(tmp_path, monkeypatch):
    database, maintenance = create_maintenance(tmp_path, 1)
    original = database.write
    @contextmanager
    def rejected_commit():
        with original() as session:
            yield session
            raise RuntimeError("synthetic commit failure")
    try:
        monkeypatch.setattr(database, "write", rejected_commit)
        with pytest.raises(RuntimeError, match="synthetic commit failure"):
            maintenance._reconcile(utcnow())
        assert getattr(maintenance, "_ledger_cursor", None) is None
        assert set(values(database).values()) == {"queued"}
        monkeypatch.setattr(database, "write", original)
        maintenance._reconcile(utcnow())
        assert set(values(database).values()) == {"ready"}
    finally:
        database.close()
