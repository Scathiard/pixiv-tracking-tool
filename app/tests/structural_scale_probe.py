"""Full Library/HTTP fixed-K scale evidence using one retained synthetic DB.

Run from app/: python tests/structural_scale_probe.py --artifacts NEW_DIRECTORY
The probe never opens a production library, downloads media, or removes files.
N, H, P, R, S, and E are grown independently. SQLite VM instructions and actual query
plans are recorded for the complete product calls, including row hydration.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from hashlib import sha256
from pathlib import Path
import platform
import sqlite3
import sys
import time
from types import SimpleNamespace

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.core.database import ArchiveDatabase
from src.core.library import Library, LibraryFilter
from src.core.models import ApplicationState, ArchiveIntent, Artist, Asset, LocalPreference, Operation, OperationItem, RemoteBookmark, TaskRound, Work, WorkRevision, RevisionAsset
from src.core.read_control import current_read
from src.core.reading import ReadingSessions
from src.core.tasks import TaskQueue
from src.routes.archive_api import create_router


K = 20
REFERENCE = datetime(2026, 10, 4, 12)
BYTE_BUDGET = 1_500_000_000


def artifact_bytes(root):
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def source_fingerprints():
    app = Path(__file__).resolve().parents[1]
    # Include lifecycle writers and future projection modules as well as the
    # complete read/API implementation, without relying on a stale hand list.
    paths = sorted(set((app / "src/core").rglob("*.py")) | set((app / "src/routes").rglob("*.py")) |
                   {app / "src/data_migration.py"})
    return {str(path.relative_to(app)): sha256(path.read_bytes()).hexdigest() for path in paths}


class Probe:
    """Instrument the actual DB driver without replacing product read code.

    The injected progress callback preserves the current product read deadline
    and cancellation checks. EXPLAIN work is outside the measured instruction
    count. VM counts use an interval of one, including result-row consumption.
    """
    def __init__(self, database):
        self.database = database

    def measure(self, label, action):
        statements, drivers = [], set()

        def before(connection, cursor, sql, parameters, context, many):
            if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
                return
            raw = connection.connection.driver_connection
            raw.set_progress_handler(None, 0)
            cursor.execute("EXPLAIN QUERY PLAN " + sql, parameters)
            record = {"sql": sql, "parameters": parameters,
                      "plan": [list(row) for row in cursor.fetchall()], "vm_steps": 0}
            statements.append(record)
            drivers.add(raw)
            budget = current_read.get()

            def progress():
                record["vm_steps"] += 1
                return int(budget.stopped()) if budget is not None else 0
            raw.set_progress_handler(progress, 1)

        event.listen(self.database.engine, "before_cursor_execute", before)
        started = time.perf_counter()
        try:
            result = action()
        finally:
            elapsed = time.perf_counter() - started
            event.remove(self.database.engine, "before_cursor_execute", before)
            for driver in drivers:
                driver.set_progress_handler(None, 0)
        evidence = {"label": label, "vm_steps": sum(row["vm_steps"] for row in statements),
                    "elapsed_seconds": elapsed, "statement_count": len(statements),
                    "statements": statements}
        assert evidence["vm_steps"] > 0, evidence
        return result, evidence


def grow_works(database, previous, target):
    for start in range(previous + 1, target + 1, 1000):
        end = min(target + 1, start + 1000)
        with database.write() as session:
            session.execute(Work.__table__.insert(), [dict(id=i, pixiv_illust_id=str(i), artist_id=1,
                title="synthetic equal title", type="illust", page_count=1,
                posted_at=None, downloaded_at=REFERENCE,
                source_metadata={}) for i in range(start, end)])
            session.execute(RemoteBookmark.__table__.insert(), [dict(work_id=i, value=True, confirmed_at=REFERENCE)
                for i in range(start, end) if i % 10 == 0])


def seed_source_anomalies(database):
    value = {"state": "private", "policy": "source-v2", "confirmed": True,
             "account_id": None, "reason": "synthetic source unavailable", "checked_at": REFERENCE.isoformat()}
    with database.write() as session:
        for work_id in range(21, 41):
            session.get(Work, work_id).source_metadata = {"remote_availability": {
                **value, "account_id": "42" if work_id % 2 else None}}
        session.add(ApplicationState(key="artist_source:synthetic", value={**value, "account_id": "42"}))


def grow_history(database, previous, target):
    """Relevant direct AND identity-free scope history, not just unrelated rows."""
    for start in range(previous, target, 500):
        end = min(target, start+500)
        with database.write() as session:
            if start == 0:
                session.add(TaskRound(id="scale-round", state="frozen", frozen_at=REFERENCE))
                session.flush()
                session.add(Operation(id="status-probe", round_id="scale-round", kind="reconcile",
                    dedupe_key="status-probe", label="synthetic task status", state="running",
                    parameters={"work_ids": list(range(1, K+1))}, created_at=REFERENCE+timedelta(days=2),
                    updated_at=REFERENCE+timedelta(days=2), phase="inventory", phase_unit="items"))
                session.flush()
            session.execute(Operation.__table__.insert(), [dict(id=f"history-{i:06}",
                dedupe_key=f"history-{i:06}", kind="verify", label="synthetic history",
                state="finished", created_at=REFERENCE + timedelta(seconds=i),
                updated_at=REFERENCE + timedelta(seconds=i),
                finished_at=REFERENCE + timedelta(seconds=i),
                parameters={"work_ids": list(range(1, K+1))}) for i in range(start, end)])
            session.execute(OperationItem.__table__.insert(), [dict(id=f"history-item-{i:06}",
                operation_id=f"history-{i:06}", item_key="one", kind="verify", phase="verifying",
                state="failed", position=0,
                target={"work_id": i % K + 1} if i % 2 else {},
                result={"message": f"synthetic history {i}"}) for i in range(start, end)])
            session.execute(Operation.__table__.insert(), [dict(id=f"unrelated-{i:06}",
                dedupe_key=f"unrelated-{i:06}", kind="verify", label="synthetic unrelated history",
                state="finished", created_at=REFERENCE + timedelta(seconds=i),
                updated_at=REFERENCE + timedelta(seconds=i), finished_at=REFERENCE + timedelta(seconds=i),
                parameters={}) for i in range(start, end)])
            session.execute(OperationItem.__table__.insert(), [dict(id=f"unrelated-item-{i:06}",
                operation_id=f"unrelated-{i:06}", item_key="one", kind="verify", phase="verifying",
                state="failed", position=0, target={"work_id": 25000}, result={}) for i in range(start, end)])
            session.execute(OperationItem.__table__.insert(), [dict(id=f"status-item-{i:06}",
                operation_id="status-probe", item_key=f"status-{i}", kind="verify", phase="inventory", unit="items",
                state="pending" if i % 2 == 0 else "failed", position=i,
                target={"work_id": i % K + 1}, result={}) for i in range(start, end)])
    large_result = {"planning_complete": True,
        "current_items": [{"id": f"synthetic-{i}", "unused": "x"*128} for i in range(target)],
        "child_operation_ids": [f"synthetic-child-{i}" for i in range(target)],
        "parent_operation_ids": [f"synthetic-parent-{i}" for i in range(target)]}
    with database.write() as session:
        session.get(Operation, "status-probe").result = large_result
        session.get(Operation, f"unrelated-{target-1:06}").result = large_result
    return len(json.dumps(large_result).encode("utf-8"))*2


def set_scope(database, size):
    with database.write() as session:
        operation = session.get(Operation, "large-scope")
        if operation is None:
            operation = Operation(id="large-scope", kind="verify", dedupe_key="large-scope",
                label="synthetic scope", state="planning", created_at=REFERENCE+timedelta(days=1),
                parameters={"work_ids": list(range(1, size+1))})
            session.add(operation)
        else:
            operation.parameters = {"work_ids": list(range(1, size+1))}
        historical = session.get(Operation, "scope-history")
        if historical is None:
            historical = Operation(id="scope-history", kind="verify", dedupe_key="scope-history",
                label="synthetic historical scope", state="finished", finished_at=REFERENCE+timedelta(days=3),
                parameters={"work_ids": list(range(1, size+1))})
            session.add(historical)
        else:
            historical.parameters = {"work_ids": list(range(1, size+1))}


def grow_revision_history(database, previous, target):
    for start in range(previous, target, 500):
        end = min(target, start+500)
        with database.write() as session:
            session.execute(WorkRevision.__table__.insert(), [dict(id=f"history-revision-{i:06}",
                work_id=2, ordinal=i+2, title=f"synthetic version {i}", type="illust", page_count=1,
                source_signature=f"history-revision-{i:06}", state="saved", created_at=REFERENCE)
                for i in range(start, end)])


def grow_exclusions(database, n, previous, target):
    for start in range(previous, target, 1000):
        end = min(target, start+1000)
        with database.write() as session:
            hidden = [dict(work_id=n-i, is_hidden=True) for i in range(start, end) if i % 2 == 0]
            suppressed = [dict(pixiv_illust_id=str(n-i), mode="suppressed") for i in range(start, end) if i % 2]
            if hidden:
                session.execute(LocalPreference.__table__.insert(), hidden)
            if suppressed:
                session.execute(ArchiveIntent.__table__.insert(), suppressed)


def grow_pages(database, previous, target):
    metadata = {"meta_pages": [{"page": i, "image_urls": {"original": f"https://synthetic.invalid/{i:06}.png"},
        "unused": "x"*80} for i in range(target)]}
    for start in range(previous, target, 1000):
        end = min(target, start + 1000)
        with database.write() as session:
            work = session.get(Work, 1)
            work.page_count = target
            work.source_metadata = metadata
            pinned = session.get(Work, 2)
            pinned.page_count = target
            pinned.type = "manga"
            pinned.source_metadata = metadata
            pinned.default_revision_id = pinned.latest_revision_id = "long-revision"
            revision = session.get(WorkRevision, "long-revision")
            if revision is None:
                revision = WorkRevision(id="long-revision", work_id=2, ordinal=1,
                    title="synthetic long revision", type="manga", page_count=target,
                    source_signature="synthetic-long", source_metadata=metadata)
                session.add(revision)
                session.flush()
            else:
                revision.page_count = target
                revision.source_metadata = metadata
            session.execute(Asset.__table__.insert(), [dict(id=f"long-page-{i:06}", work_id=1,
                role="image", page_index=i, state="verified", relative_path=f"synthetic/1_p{i}.png")
                for i in range(start, end)])
            session.execute(Asset.__table__.insert(), [dict(id=f"pinned-page-{i:06}", work_id=2,
                revision_id="long-revision", role="image", page_index=i, state="verified",
                relative_path=f"synthetic/2_p{i}.png") for i in range(start, end)])
            session.execute(RevisionAsset.__table__.insert(), [dict(revision_id="long-revision",
                role="image", page_index=i, asset_id=f"pinned-page-{i:06}") for i in range(start, end)])
    return len(json.dumps(metadata).encode("utf-8")) * 3


def compact_ids(page):
    assert len(page["items"]) == K, page.keys()
    assert page["boundaries"] == [item["cursor"] for item in page["items"]]
    return [item["id"] for item in page["items"]]


def assert_bounded(rows, key):
    values = [row[key]["vm_steps"] for row in rows]
    assert max(values) < 200_000, (key, values)
    assert max(values) <= min(values) * 1.35 + 1000, (key, values)


def assert_no_bulk_metadata(evidence):
    for statement in evidence["statements"]:
        sql = statement["sql"]
        if sql.startswith("SELECT works.") or sql.startswith("SELECT work_revisions."):
            columns = sql.split("\nFROM ", 1)[0]
            assert "works.source_metadata" not in columns, columns
            assert "work_revisions.source_metadata" not in columns, columns


def _save(root, evidence):
    evidence["artifact_bytes"] = artifact_bytes(root)
    assert evidence["artifact_bytes"] < BYTE_BUDGET, evidence["artifact_bytes"]
    (root / "structural-scale-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _open_synthetic_database(root, reuse):
    if not reuse:
        root.mkdir(parents=True, exist_ok=False)
        return ArchiveDatabase.create(root / "synthetic-scale.db")
    marker = root / "structural-scale-evidence.json"
    existing = json.loads(marker.read_text(encoding="utf-8"))
    if existing.get("synthetic_only") is not True or existing.get("K") != K:
        raise ValueError("reuse requires this probe's existing synthetic marker")
    retained = root / f"preliminary-scale-{time.time_ns()}.json"
    retained.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    # The ordinary owner correctly rejects a stale exploratory read schema.
    # Use an explicit candidate transaction on this marked, probe-owned DB to
    # reset source rows and recreate its empty read schema before reopening it.
    # Neither the DB file nor any previous evidence is removed or replaced.
    from src.core.projection_maintenance import _expected_projection_schema, install_projections, rebuild_projections
    engine = create_engine("sqlite:///" + str(root / "synthetic-scale.db"))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            for table in ("operation_items", "operations", "task_rounds", "archive_intents", "revision_assets", "revision_derivatives",
                          "assets", "derivatives", "work_revisions", "work_tags", "local_preferences",
                          "remote_bookmarks", "bookmark_operations", "works", "artists", "application_state"):
                connection.exec_driver_sql(f"DELETE FROM {table}")
            names = connection.exec_driver_sql("SELECT type,name FROM sqlite_master "
                "WHERE type IN ('trigger','table') AND name LIKE 'read_%' ORDER BY type DESC").all()
            for kind, name in names:
                safe = name.replace('"', '""')
                connection.exec_driver_sql(f'DROP {kind.upper()} "{safe}"')
            expected = _expected_projection_schema()
            indexes = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index'").scalars().all()
            for name in indexes:
                if name.casefold() in expected and expected[name.casefold()][0] == "index":
                    safe = name.replace('"', '""')
                    connection.exec_driver_sql(f'DROP INDEX "{safe}"')
            install_projections(connection)
            with Session(bind=connection) as session:
                rebuild_projections(session)
    finally:
        engine.dispose()
    return ArchiveDatabase(root / "synthetic-scale.db")


def run(root, *, n_sizes=(1000, 10000, 100000), h_sizes=(100, 1000, 10000),
        p_sizes=(100, 1000, 10000), s_sizes=(100, 1000, 10000),
        r_sizes=(100, 1000, 10000), e_sizes=(100, 1000, 10000), reuse=False):
    root = Path(root).resolve()
    database = _open_synthetic_database(root, reuse)
    library = Library(database)
    probe = Probe(database)
    queue = TaskQueue(database, handlers={})
    runtime = SimpleNamespace(database=database, library=library, reading=ReadingSessions(library), queue=queue,
                              recovery_issues=lambda: [])
    app = FastAPI()
    app.include_router(create_router(lambda: runtime))
    evidence = {"synthetic_only": True, "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version, "K": K, "N": [], "H": [], "P": [], "S": [], "R": [], "E": [],
                "source_sha256": source_fingerprints(), "measurement": "warm product calls; cold startup recorded separately"}
    try:
        with database.write() as session:
            session.add(Artist(id=1, pixiv_user_id="synthetic", name="synthetic"))
        with TestClient(app, base_url="http://localhost") as client:
            previous = 0
            for n in n_sizes:
                grow_works(database, previous, n)
                if previous == 0:
                    seed_source_anomalies(database)
                row = {"N": n, "H": 0, "P": 0}
                if previous == 0:
                    _, evidence["cold_library_first"] = probe.measure("cold_library_first",
                        lambda: library.list_cursor(limit=K))
                else:
                    library.list_cursor(limit=K)
                page, row["library_first"] = probe.measure("library_first",
                    lambda: library.list_cursor(limit=K))
                assert compact_ids(page) == list(range(n, n-K, -1))
                response, row["api_first"] = probe.measure("api_first",
                    lambda: client.get("/api/library/cursor", params={"limit": K, "compact": "true"}))
                assert response.status_code == 200, response.text
                assert compact_ids(response.json()) == list(range(n, n-K, -1))
                anchor = n // 2
                deep, row["library_deep"] = probe.measure("library_deep",
                    lambda: library.list_cursor(limit=K, anchor_work_id=anchor))
                assert compact_ids(deep) == list(range(anchor, anchor-K, -1))
                deep_api, row["api_deep"] = probe.measure("api_deep",
                    lambda: client.get("/api/library/cursor", params={"limit": K, "compact": "true", "anchor_work_id": anchor}))
                assert deep_api.status_code == 200, deep_api.text
                assert compact_ids(deep_api.json()) == compact_ids(deep)
                neighbors, row["neighbors"] = probe.measure("neighbors",
                    lambda: library.cursor_neighbors(anchor, cursor=deep["items"][0]["cursor"]))
                assert neighbors["previous"]["work_id"] == anchor + 1
                assert neighbors["next"]["work_id"] == anchor - 1
                neighbor_api, row["api_neighbors"] = probe.measure("api_neighbors",
                    lambda: client.get(f"/api/works/{anchor}/cursor-neighbors",
                        params={"cursor": deep["items"][0]["cursor"]}))
                assert neighbor_api.status_code == 200, neighbor_api.text
                assert neighbor_api.json()["previous"]["work_id"] == anchor + 1
                assert neighbor_api.json()["next"]["work_id"] == anchor - 1
                feed_api, row["api_feed_deep"] = probe.measure("api_feed_deep",
                    lambda: client.get("/api/library/cursor/feed", params={"limit": K, "anchor_work_id": anchor}))
                assert feed_api.status_code == 200, feed_api.text
                assert [item["work_id"] for item in feed_api.json()["items"]] == compact_ids(deep)
                next_page, row["library_deep_next"] = probe.measure("library_deep_next",
                    lambda: library.list_cursor(limit=K, cursor=deep["next_cursor"]))
                assert compact_ids(next_page) == list(range(anchor-K, anchor-2*K, -1))
                backwards, row["library_deep_previous"] = probe.measure("library_deep_previous",
                    lambda: library.list_cursor(limit=K, cursor=next_page["previous_cursor"], direction="previous"))
                assert compact_ids(backwards) == compact_ids(deep)
                for name, selected, expected in (
                    ("artist", LibraryFilter(artist_ids=(1,)), list(range(n, n-K, -1))),
                    ("type", LibraryFilter(types=("illust",)), list(range(n, n-K, -1))),
                    ("favorites", LibraryFilter(favorites=True), list(range(n, n-K*10, -10))),
                    ("title", LibraryFilter(sort="title"), list(range(1, K+1))),
                    ("oldest", LibraryFilter(sort="oldest"), list(range(1, K+1))),
                    ("added", LibraryFilter(sort="added"), list(range(n, n-K, -1))),
                ):
                    scoped, row["library_"+name] = probe.measure("library_"+name,
                        lambda selected=selected: library.list_cursor(selected, limit=K))
                    assert compact_ids(scoped) == expected
                    assert scoped["performance"] == "indexed_seek"
                title_deep, row["library_title_deep"] = probe.measure("library_title_deep",
                    lambda: library.list_cursor(LibraryFilter(sort="title"), limit=K, anchor_work_id=anchor))
                assert compact_ids(title_deep) == list(range(anchor, anchor+K))
                source, row["source_works"] = probe.measure("source_works",
                    lambda: library.source_overview(kind="works", limit=K))
                assert source["total"] == K and len(source["items"]) == K
                source_artist, row["source_artists"] = probe.measure("source_artists",
                    lambda: library.source_overview(kind="artists", limit=K))
                assert source_artist["total"] == len(source_artist["items"]) == 1
                library.account_id = "42"
                try:
                    scoped_source, row["source_works_account42"] = probe.measure("source_works_account42",
                        lambda: library.source_overview(kind="works", limit=K))
                    assert scoped_source["total"] == len(scoped_source["items"]) == K
                    scoped_artist, row["source_artists_account42"] = probe.measure("source_artists_account42",
                        lambda: library.source_overview(kind="artists", limit=K))
                    assert scoped_artist["total"] == 1
                finally:
                    library.account_id = None
                source_api, row["api_source_works"] = probe.measure("api_source_works",
                    lambda: client.get("/api/library/source-status", params={"kind": "works", "limit": K}))
                assert source_api.status_code == 200, source_api.text
                assert source_api.json()["total"] == K
                artists, row["artist_page"] = probe.measure("artist_page",
                    lambda: library.artist_page(limit=K))
                assert artists["items"][0]["work_count"] == n
                artist, row["artist_detail"] = probe.measure("artist_detail",
                    lambda: library.artist_detail(1))
                assert artist["work_count"] == n and len(artist["recent_works"]) == 12
                for key in ("library_first", "api_first", "library_deep"):
                    plans = [detail for statement in row[key]["statements"] for *_, detail in statement["plan"]]
                    assert any("ix_read_work_order" in detail for detail in plans), (key, plans)
                    assert not any("SCAN read_work_order" in detail and "USING" not in detail for detail in plans), plans
                    assert not any("operation_items" in statement["sql"] for statement in row[key]["statements"])
                evidence["N"].append(row)
                _save(root, evidence)
                print(json.dumps({"N": n, **{key: row[key]["vm_steps"] for key in row if isinstance(row[key], dict)} }), flush=True)
                previous = n
            for key in ("library_first", "api_first", "library_deep", "api_deep", "neighbors", "api_neighbors",
                        "api_feed_deep", "library_deep_next", "library_deep_previous"):
                assert_bounded(evidence["N"], key)
            for key in ("source_works", "source_artists", "source_works_account42", "source_artists_account42",
                        "api_source_works", "artist_page", "artist_detail"):
                assert_bounded(evidence["N"], key)
            for key in ("library_artist", "library_type", "library_favorites", "library_title",
                        "library_added", "library_title_deep"):
                assert_bounded(evidence["N"], key)
            assert_bounded(evidence["N"], "library_oldest")

            previous = 0
            for h in h_sizes:
                result_bytes = grow_history(database, previous, h)
                row = {"N": n_sizes[-1], "H": h, "P": 0, "source_result_bytes": result_bytes}
                # H names total source history for each class, not the number
                # attached to every selected work. Record the actual fanout.
                with database.read() as session:
                    candidates = session.connection().exec_driver_sql(
                        "SELECT work_id,count(*),sum(substr(candidate_id,1,6)='scope:') "
                        "FROM read_task_candidates WHERE operation_id GLOB 'history-*' "
                        "AND work_id<=? GROUP BY work_id ORDER BY work_id", (K,)).all()
                row["history_source_counts"] = {"relevant_operations": h, "relevant_items": h,
                    "unrelated_operations": h, "unrelated_items": h, "active_status_items": h}
                row["selected_work_history_candidates"] = [dict(work_id=work_id,
                    total=count, scope=scope, direct=count-scope) for work_id, count, scope in candidates]
                assert len(candidates) == K
                assert all(scope == h//2 and count == h//2 + (h//K if work_id % 2 == 0 else 0)
                           for work_id, count, scope in candidates)
                page, row["library"] = probe.measure("history_library",
                    lambda: library.list_cursor(limit=K, anchor_work_id=K))
                assert compact_ids(page) == list(range(K, 0, -1))
                response, row["api"] = probe.measure("history_api",
                    lambda: client.get("/api/library/cursor", params={"limit": K, "anchor_work_id": K}))
                assert response.status_code == 200, response.text
                assert compact_ids(response.json()) == list(range(K, 0, -1))
                unrelated, row["library_unrelated"] = probe.measure("unrelated_history_library",
                    lambda: library.list_cursor(limit=K))
                assert compact_ids(unrelated) == list(range(n_sizes[-1], n_sizes[-1]-K, -1))
                unrelated_api, row["api_unrelated"] = probe.measure("unrelated_history_api",
                    lambda: client.get("/api/library/cursor", params={"limit": K}))
                assert unrelated_api.status_code == 200, unrelated_api.text
                assert any(card["processing"]["state"] == "attention" for card in page["items"])
                task, row["task_details"] = probe.measure("task_details",
                    lambda: queue.details("status-probe", limit=K))
                assert sum(task["counts"].values()) == h
                assert len(task["items"]) == K
                task_api, row["api_task_details"] = probe.measure("api_task_details",
                    lambda: client.get("/api/tasks/status-probe", params={"limit": K}))
                assert task_api.status_code == 200, task_api.text
                assert sum(task_api.json()["counts"].values()) == h
                fresh_queue = TaskQueue(database, handlers={})
                snapshot, row["task_snapshot"] = probe.measure("task_snapshot",
                    lambda: fresh_queue.snapshot())
                row["task_snapshot_response_bytes"] = len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
                history, row["task_history"] = probe.measure("task_history",
                    lambda: queue.history(limit=K))
                assert len(history["items"]) == K
                before = f"history-{h//2:06}"
                deep_history, row["task_history_deep"] = probe.measure("task_history_deep",
                    lambda: queue.history(before=before, limit=K))
                assert len(deep_history["items"]) == K
                history_api, row["api_task_history_deep"] = probe.measure("api_task_history_deep",
                    lambda: client.get("/api/tasks", params={"limit": K, "before": before}))
                assert history_api.status_code == 200, history_api.text
                progress_api, row["api_progress"] = probe.measure("api_progress",
                    lambda: client.get("/api/progress"))
                assert progress_api.status_code == 200, progress_api.text
                for key in ("library", "api"):
                    assert not any("operation_items" in statement["sql"] or "json_each" in statement["sql"]
                                   for statement in row[key]["statements"])
                evidence["H"].append(row)
                _save(root, evidence)
                print(json.dumps({"H": h, "library": row["library"]["vm_steps"], "api": row["api"]["vm_steps"]}), flush=True)
                previous = h
            for key in ("library", "api", "library_unrelated", "api_unrelated", "task_details",
                        "api_task_details", "task_snapshot", "task_history"):
                assert_bounded(evidence["H"], key)
            for key in ("task_history_deep", "api_task_history_deep", "api_progress"):
                assert_bounded(evidence["H"], key)
            sizes = [row["task_snapshot_response_bytes"] for row in evidence["H"]]
            assert max(sizes) <= min(sizes)*1.1 + 1000, ("task_snapshot_response_bytes", sizes)

            for s in s_sizes:
                started = time.perf_counter()
                set_scope(database, s)
                row = {"N": n_sizes[-1], "H_relevant": h_sizes[-1], "H_unrelated": h_sizes[-1],
                       "P": 0, "S": s, "source_write_seconds": time.perf_counter()-started}
                page, row["library"] = probe.measure("scope_library",
                    lambda: library.list_cursor(limit=K, anchor_work_id=K))
                assert compact_ids(page) == list(range(K, 0, -1))
                response, row["api"] = probe.measure("scope_api",
                    lambda: client.get("/api/library/cursor", params={"limit": K, "anchor_work_id": K}))
                assert response.status_code == 200, response.text
                assert all(card["processing"]["state"] == "queued" for card in page["items"])
                fresh_queue = TaskQueue(database, handlers={})
                status, row["task_snapshot"] = probe.measure("scope_task_snapshot",
                    lambda: fresh_queue.snapshot())
                row["task_snapshot_response_bytes"] = len(json.dumps(status, ensure_ascii=False).encode("utf-8"))
                evidence["S"].append(row)
                _save(root, evidence)
                print(json.dumps({"S": s, "library": row["library"]["vm_steps"], "api": row["api"]["vm_steps"],
                    "source_write_seconds": row["source_write_seconds"]}), flush=True)
            for key in ("library", "api", "task_snapshot"):
                assert_bounded(evidence["S"], key)

            previous = 0
            for p in p_sizes:
                metadata_bytes = grow_pages(database, previous, p)
                row = {"N": n_sizes[-1], "H": h_sizes[-1], "P": p, "source_metadata_bytes": metadata_bytes}
                if previous == 0:
                    _, evidence["cold_pinned_library_first"] = probe.measure("cold_pinned_library_first",
                        lambda: library.list_cursor(limit=K, anchor_work_id=K))
                else:
                    library.list_cursor(limit=K, anchor_work_id=K)
                page, row["library"] = probe.measure("page_library",
                    lambda: library.list_cursor(limit=K, anchor_work_id=K))
                assert compact_ids(page) == list(range(K, 0, -1))
                assert page["items"][-1]["archive"]["total"] == p
                assert page["items"][-1]["archive"]["verified"] == p
                assert page["items"][-2]["archive"]["total"] == p
                assert page["items"][-2]["archive"]["verified"] == p
                card_api, row["api"] = probe.measure("longwork_api_cards",
                    lambda: client.get("/api/library/cursor", params={"limit": K, "compact": "true", "anchor_work_id": K}))
                assert card_api.status_code == 200, card_api.text
                row["api_card_response_bytes"] = len(card_api.content)
                detail, row["detail"] = probe.measure("page_detail",
                    lambda: library.detail(1, page_offset=p-1, page_limit=1))
                assert len(detail["pages"]) == 1
                assert detail["archive"]["verified"] == p
                row["detail_response_bytes"] = len(json.dumps(detail, ensure_ascii=False).encode("utf-8"))
                revision_detail, row["revision_detail"] = probe.measure("revision_page_detail",
                    lambda: library.detail(2, page_offset=p-1, page_limit=1, revision_id="long-revision"))
                assert len(revision_detail["pages"]) == 1
                assert revision_detail["archive"]["verified"] == p
                row["revision_detail_response_bytes"] = len(json.dumps(revision_detail, ensure_ascii=False).encode("utf-8"))
                menu, row["revision_menu"] = probe.measure("longwork_revision_menu",
                    lambda: library.revision_page(2, limit=K))
                assert menu["items"][0]["id"] == "long-revision"
                feed_call = lambda: client.get("/api/library/cursor/feed", params={"limit": K, "anchor_work_id": 1,
                        "page": p-K, "index": p-K})
                if previous == 0:
                    cold_feed, evidence["cold_longwork_api_feed"] = probe.measure("cold_longwork_api_feed", feed_call)
                    assert cold_feed.status_code == 200, cold_feed.text
                else:
                    assert feed_call().status_code == 200
                feed, row["api_feed_deep"] = probe.measure("longwork_api_feed", feed_call)
                assert feed.status_code == 200, feed.text
                assert [item["page"] for item in feed.json()["items"]] == list(range(p-K, p))
                row["api_feed_response_bytes"] = len(feed.content)
                for key in ("library", "api", "detail", "revision_detail", "revision_menu", "api_feed_deep"):
                    assert_no_bulk_metadata(row[key])
                evidence["P"].append(row)
                _save(root, evidence)
                print(json.dumps({"P": p, "library": row["library"]["vm_steps"], "detail": row["detail"]["vm_steps"]}), flush=True)
                previous = p
            for key in ("library", "api", "detail", "revision_detail", "revision_menu", "api_feed_deep"):
                assert_bounded(evidence["P"], key)
            for key in ("api_card_response_bytes", "detail_response_bytes", "revision_detail_response_bytes", "api_feed_response_bytes"):
                values = [row[key] for row in evidence["P"]]
                assert max(values) <= min(values)*1.1 + 1000, (key, values)

            previous = 0
            for r in r_sizes:
                grow_revision_history(database, previous, r)
                row = {"N": n_sizes[-1], "R": r}
                page, row["library"] = probe.measure("revision_history_library",
                    lambda: library.list_cursor(limit=K, anchor_work_id=K))
                assert compact_ids(page) == list(range(K, 0, -1))
                detail, row["detail"] = probe.measure("revision_history_detail",
                    lambda: library.detail(2, page_offset=0, page_limit=1))
                assert len(detail["pages"]) == 1
                assert len(detail["revisions"]) <= 101
                assert detail["revisions_has_more"] is True
                versions, row["revision_page"] = probe.measure("revision_history_page",
                    lambda: library.revision_page(2, before_ordinal=r//2, limit=K))
                assert len(versions["items"]) == K
                assert versions["items"][0]["ordinal"] == r//2 - 1
                evidence["R"].append(row)
                _save(root, evidence)
                print(json.dumps({"R": r, "library": row["library"]["vm_steps"], "detail": row["detail"]["vm_steps"],
                    "revision_page": row["revision_page"]["vm_steps"]}), flush=True)
                previous = r
            for key in ("library", "detail", "revision_page"):
                assert_bounded(evidence["R"], key)

            previous = 0
            for e in e_sizes:
                grow_exclusions(database, n_sizes[-1], previous, e)
                row = {"N": n_sizes[-1], "E": e, "hidden": (e+1)//2, "suppressed": e//2}
                page, row["library"] = probe.measure("excluded_library",
                    lambda: library.list_cursor(limit=K))
                expected = list(range(n_sizes[-1]-e, n_sizes[-1]-e-K, -1))
                assert compact_ids(page) == expected
                assert page["total"] == n_sizes[-1]-e
                response, row["api"] = probe.measure("excluded_api",
                    lambda: client.get("/api/library/cursor", params={"limit": K, "compact": "true"}))
                assert response.status_code == 200, response.text
                assert compact_ids(response.json()) == expected
                evidence["E"].append(row)
                _save(root, evidence)
                print(json.dumps({"E": e, "library": row["library"]["vm_steps"], "api": row["api"]["vm_steps"]}), flush=True)
                previous = e
            for key in ("library", "api"):
                assert_bounded(evidence["E"], key)
        evidence["passed"] = True
        assert evidence["source_sha256"] == source_fingerprints(), "source changed while probe was running"
    except BaseException as exc:
        evidence["passed"] = False
        evidence["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        database.close()
        evidence["source_sha256_after"] = source_fingerprints()
        _save(root, evidence)
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--reuse-artifacts", action="store_true",
                        help="reset only this probe's marked synthetic rows and reuse its DB file")
    arguments = parser.parse_args()
    run(arguments.artifacts, reuse=arguments.reuse_artifacts)
