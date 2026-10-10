"""Task lookup indexes: memory-only scale, historical semantics and DDL ownership."""
from datetime import datetime
import json
import time

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from src.core.archive_state import _operation_window
from src.core.models import Base, Artist, Asset, Derivative, Operation, OperationItem, Work
from src.core.task_indexes import TASK_INDEX_SQL, create_task_indexes


@pytest.fixture
def engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def seed(engine, size=0):
    with Session(engine) as session:
        session.add(Artist(id=1, pixiv_user_id="synthetic", name="synthetic"))
        session.add_all([Work(id=value, pixiv_illust_id=str(value), artist_id=1, title="synthetic")
            for value in (*range(1, 7), 99)])
        session.flush()
        session.add_all([Asset(id="asset-1", work_id=1, role="image", page_index=0),
            Asset(id="asset-2", work_id=2, role="image", page_index=0),
            Asset(id="7", work_id=4, role="image", page_index=0),
            Asset(id="bare-outside-window", work_id=99, role="image", page_index=0)])
        session.add_all([Derivative(id="record-1", work_id=1, kind="thumbnail", spec_key="v1", page_index=0),
            Derivative(id="record-3", work_id=3, kind="thumbnail", spec_key="v1", page_index=0),
            Derivative(id="8", work_id=5, kind="thumbnail", spec_key="v1", page_index=0),
            Derivative(id="record-6", work_id=6, kind="thumbnail", spec_key="v1", page_index=0)])
        session.add_all([Operation(id="active", kind="reconcile", dedupe_key="active", label="synthetic",
            state="running", created_at=datetime(2026, 1, 5), parameters={}),
            Operation(id="scope", kind="verify", dedupe_key="scope", label="synthetic",
                state="finished", created_at=datetime(2026, 1, 4), parameters={"work_ids": [str(i) for i in range(1, 7)]}),
            Operation(id="noise", kind="reconcile", dedupe_key="noise", label="synthetic",
                state="finished", created_at=datetime(2026, 1, 1), parameters={"work_ids": [6]}),
            Operation(id="dedupe", kind="repair", dedupe_key="dedupe", label="synthetic",
                state="finished", created_at=datetime(2026, 1, 2), parameters={})])
        session.flush()
        cases = [
            ("explicit", {"work_id": 1, "asset_id": "asset-2"}, "pending"),
            ("string-work-is-not-integer", {"work_id": "1", "asset_id": "asset-1"}, "running"),
            ("asset-wins", {"asset_id": "asset-2", "record_id": "record-1"}, "pending"),
            ("missing-asset-falls-back", {"asset_id": "absent", "record_id": "record-3"}, "failed"),
            ("numeric-asset", {"asset_id": 7}, "pending"),
            ("numeric-record", {"record_id": 8}, "pending"),
            ("outside-asset-shadows-record", {"asset_id": "bare-outside-window", "record_id": "record-6"}, "running"),
        ]
        for position, (key, target, state) in enumerate(cases):
            session.add(OperationItem(id=key, operation_id="active", item_key=key, position=position,
                kind="checked", target=target, phase="verifying", unit="items", state=state,
                result={"message": key}, depends_on=[]))
        session.add_all([OperationItem(id="legacy-success", operation_id="scope", item_key="old-success", position=0,
            kind="checked", target={}, phase="verifying", unit="items", state="succeeded", result={}, depends_on=[]),
            OperationItem(id="legacy-failure", operation_id="scope", item_key="old-failure", position=1,
                kind="error", target={"work_id": None, "asset_id": None, "record_id": None}, phase="verifying", unit="items",
                state="failed", result={"message": "legacy failure"}, depends_on=[]),
            OperationItem(id="two-matching-targets", operation_id="dedupe", item_key="both", position=0,
                kind="checked", target={"asset_id": "asset-1", "record_id": "record-1"}, phase="verifying", unit="items",
                state="succeeded", result={}, depends_on=[])])
        session.commit()
    if size:
        with engine.begin() as connection:
            connection.exec_driver_sql("""WITH RECURSIVE rows(i) AS (
                SELECT 0 UNION ALL SELECT i+1 FROM rows WHERE i<:last)
                INSERT INTO operation_items(id,operation_id,item_key,position,kind,target,phase,unit,depends_on,state,lane,attempts,result)
                SELECT 'noise-'||i,'noise','noise-'||i,i,'reconcile_asset',
                    CASE WHEN i%3=0 THEN '{"work_id":999,"asset_id":"noise-'||i||'"}'
                    WHEN i%3=1 THEN '{"asset_id":"noise-'||i||'"}'
                    ELSE '{"record_id":"noise-'||i||'"}' END,
                    'verifying','assets','[]','succeeded',NULL,1,'{}' FROM rows""", {"last": size - 1})


def measured_window(engine, ids):
    evidence = []
    driver = None

    def before(connection, cursor, statement, parameters, context, many):
        nonlocal driver
        cursor.execute("EXPLAIN QUERY PLAN " + statement, parameters)
        record = {"sql": statement, "parameters": parameters,
            "plan": [list(row) for row in cursor.fetchall()], "vm_steps": 0}
        evidence.append(record)
        driver = connection.connection.driver_connection

        def progress():
            record["vm_steps"] += 1000
            return 0
        driver.set_progress_handler(progress, 1000)

    window = _operation_window(ids)
    statement = select(window).order_by(window.c.work_id, window.c.rank)
    event.listen(engine, "before_cursor_execute", before)
    started = time.perf_counter()
    try:
        with Session(engine) as session:
            rows = [dict(row) for row in session.execute(statement).mappings()]
    finally:
        if driver is not None:
            driver.set_progress_handler(None, 0)
        event.remove(engine, "before_cursor_execute", before)
    evidence[0]["elapsed_ms"] = (time.perf_counter() - started) * 1000
    return rows, evidence[0]


def winners(rows):
    return {row["work_id"]: (row["operation_id"], row["item_state"], row["concern"], row["reason"])
        for row in rows if row["rank"] == 1}


def test_direct_task_window_uses_rowid_lookup_with_identical_public_rows(engine):
    seed(engine)
    with Session(engine) as session:
        # A legacy collision can have the same operation/state/position but a
        # UUID that sorts before rows inserted earlier in physical order.
        session.add(OperationItem(id="aaa-tie", operation_id="active", item_key="aaa-tie", kind="checked",
            target={"work_id": 1}, phase="verifying", state="pending", position=0,
            result={"message": "tie first by old UUID selection"}))
        session.commit()
    with engine.begin() as connection:
        create_task_indexes(connection)
    window = _operation_window([1, 2, 3, 4, 5, 6])
    statement = select(window).order_by(window.c.work_id, window.c.rank)
    sql = str(statement.compile(engine, compile_kwargs={"literal_binds": True}))
    # Compare the same selection, union, fallback and ranking with the previous
    # UUID lookup. Only this transient lookup identity changes.
    uuid_sql = sql.replace("operation_items.rowid", "operation_items.id")
    assert uuid_sql != sql
    with engine.connect() as connection:
        expected = connection.exec_driver_sql(uuid_sql).all()
        actual = connection.exec_driver_sql(sql).all()
        assert actual == expected
        plan = [row[-1] for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql)]
    assert "SEARCH operation_items USING INTEGER PRIMARY KEY (rowid=?)" in plan
    assert all(isinstance(row.operation_id, str) for row in actual)


def test_indexed_task_window_bounds_null_prefix_and_legacy_scope(engine, tmp_path):
    seed(engine, 150000)
    before, baseline = measured_window(engine, list(range(1, 7)))
    assert winners(before) == {
        1: ("active", "pending", "original", "explicit"),
        2: ("active", "pending", "original", "asset-wins"),
        3: ("active", "failed", "original", "missing-asset-falls-back"),
        4: ("active", "pending", "original", "numeric-asset"),
        5: ("active", "pending", "local", "numeric-record"),
        6: ("scope", "failed", "work", "legacy failure"),
    }
    with engine.begin() as connection:
        for name in ("ix_items_target_work_asset", "ix_items_target_work_record"):
            connection.exec_driver_sql(TASK_INDEX_SQL[name])
    middle, two_indexes = measured_window(engine, list(range(1, 7)))
    with engine.begin() as connection:
        assert create_task_indexes(connection) == ("ix_items_target_scope",)
    after, candidate = measured_window(engine, list(range(1, 7)))
    assert before == middle == after
    # Both identity paths hit one old step; it must not gain duplicate ranks.
    assert sum(row["operation_id"] == "dedupe" for row in after) == 1
    (tmp_path / "task-index-evidence.json").write_text(json.dumps({"memory_only_rows": 150000,
        "baseline": baseline, "two_indexes": two_indexes, "candidate": candidate}, indent=2), encoding="utf-8")
    print(json.dumps({"baseline_vm": baseline["vm_steps"], "two_indexes_vm": two_indexes["vm_steps"],
        "candidate_vm": candidate["vm_steps"]}))
    assert two_indexes["vm_steps"] > 500000  # Scoped legacy gate justifies the third index.
    assert candidate["vm_steps"] < 25000
    plan = [row[-1] for row in candidate["plan"]]
    assert any("ix_items_target_work_asset (<expr>=? AND <expr>=?)" in value for value in plan)
    assert any("ix_items_target_work_record (<expr>=? AND <expr>=?)" in value for value in plan)
    assert any("ix_items_target_scope" in value for value in plan)


def test_newer_completed_success_and_unplanned_scope_keep_original_priority(engine):
    seed(engine)
    with engine.begin() as connection:
        create_task_indexes(connection)
    with Session(engine) as session:
        session.add(Operation(id="later-success", kind="verify", dedupe_key="later-success", label="synthetic",
            state="finished", parameters={"work_ids": [6]}, created_at=datetime(2026, 1, 7)))
        session.add(Operation(id="unplanned", kind="repair", dedupe_key="unplanned", label="synthetic",
            state="planning", parameters={"work_ids": [3]}, created_at=datetime(2026, 1, 8)))
        session.flush()
        session.add(OperationItem(id="late-item", operation_id="later-success", item_key="late", position=0,
            kind="checked", target={}, phase="verifying", unit="items", state="succeeded", result={}, depends_on=[]))
        session.commit()
    rows, _ = measured_window(engine, list(range(1, 7)))
    assert winners(rows)[6] == ("later-success", "succeeded", "work", None)
    assert winners(rows)[3] == ("unplanned", "pending", "work", None)


def test_create_task_indexes_is_idempotent_and_transaction_owned_by_caller(engine):
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        assert create_task_indexes(connection) == tuple(TASK_INDEX_SQL)
        assert connection.in_transaction()
        connection.rollback()
        present = set(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index'").scalars())
        assert not set(TASK_INDEX_SQL) & present
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        assert create_task_indexes(connection) == tuple(TASK_INDEX_SQL)
        connection.commit()
        assert create_task_indexes(connection) == ()


def test_create_task_indexes_accepts_existing_case_insensitive_names(engine):
    first_name = next(iter(TASK_INDEX_SQL))
    with engine.begin() as connection:
        connection.exec_driver_sql(TASK_INDEX_SQL[first_name].replace(first_name, first_name.upper(), 1))
        assert create_task_indexes(connection) == tuple(name for name in TASK_INDEX_SQL if name != first_name)
        assert create_task_indexes(connection) == ()


@pytest.mark.parametrize("target,expected_work", [
    ({"work_id": 1.0}, 1),
    ({"work_id": "1"}, None),
    ({"work_id": "01"}, None),
    ({"work_id": None, "asset_id": "asset-1"}, 1),
    ({"asset_id": 7}, 4),
    ({"asset_id": "7"}, 4),
    ({"asset_id": 7.0}, 7),
    ({"asset_id": "07"}, None),
    ({"record_id": 8.0}, 8),
    ({"record_id": "08"}, None),
])
def test_indexed_target_affinity_matches_old_numeric_and_text_rules(engine, target, expected_work):
    seed(engine)
    with Session(engine) as session:
        session.add_all([Work(id=7, pixiv_illust_id="7", artist_id=1, title="synthetic"),
            Work(id=8, pixiv_illust_id="8", artist_id=1, title="synthetic")])
        session.flush()
        session.add(Asset(id="7.0", work_id=7, role="image", page_index=0))
        session.add(Derivative(id="8.0", work_id=8, kind="thumbnail", spec_key="v1", page_index=0))
        session.add(Operation(id="typed", kind="repair", dedupe_key="typed", label="synthetic",
            state="running", created_at=datetime(2026, 1, 10), parameters={}))
        session.flush()
        session.add(OperationItem(id="typed-item", operation_id="typed", item_key="typed", position=0,
            kind="checked", target=target, phase="verifying", unit="items", state="running", result={}, depends_on=[]))
        session.commit()
    before, _ = measured_window(engine, list(range(1, 9)))
    with engine.begin() as connection:
        create_task_indexes(connection)
    after, _ = measured_window(engine, list(range(1, 9)))
    assert before == after
    matches = [row["work_id"] for row in after if row["operation_id"] == "typed"]
    assert matches == ([] if expected_work is None else [expected_work])
