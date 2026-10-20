"""Complete frozen manifests from covering indexes; synthetic data only."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

from sqlalchemy import case, event, select

from src.core.database import ArchiveDatabase
from src.core.library import Library, LibraryFilter
from src.core.models import (
    ArchiveIntent, Artist, LocalPreference, RemoteBookmark, Work, WorkRevision, WorkTag,
)
from src.core.reading import ReadingSessions


REFERENCE = datetime(2026, 10, 21, 12)


def legacy_manifest(library, filters, ceiling):
    with library.database.read() as session:
        statement = library._statement(filters, ceiling).with_only_columns(
            Work.id, Work.default_revision_id,
            case((Work.type == "ugoira", 1), else_=Work.page_count))
        return [{"work_id": row[0], "revision_id": row[1], "page_count": max(1, row[2])}
                for row in session.execute(statement.order_by(*library._order(filters)))]


def seed(path, count=501):
    database = ArchiveDatabase.create(path)
    with database.write() as session:
        session.add_all([Artist(id=i, pixiv_user_id=str(i), name=f"Artist {i}") for i in (1, 2)])
        session.flush()
        for identity in range(1, count + 1):
            session.add(Work(id=identity, pixiv_illust_id=str(identity), artist_id=1 if identity % 2 else 2,
                title=("Alpha" if identity % 3 else "alpha") + str(identity % 9),
                type="ugoira" if identity % 7 == 0 else "manga" if identity % 3 == 0 else "illust",
                page_count=identity % 5 + 1, default_revision_id="frozen-old" if identity == 1 else None,
                latest_revision_id="frozen-old" if identity == 1 else None,
                posted_at=None if identity % 11 == 0 else REFERENCE - timedelta(days=identity // 4),
                downloaded_at=REFERENCE - timedelta(days=identity // 6),
                time_basis="legacy_unknown" if identity == 3 else "utc",
                source_metadata={"synthetic_padding": "x" * 8192}))
        session.flush()
        session.add(WorkRevision(id="frozen-old", work_id=1, ordinal=1, title="Alpha1", type="illust",
            page_count=2, source_signature="old", state="complete"))
        session.add_all([WorkTag(work_id=i, tag="A" if i % 2 else "B") for i in range(1, count + 1)])
        session.add_all([LocalPreference(work_id=i, is_hidden=True) for i in (2, 15)])
        session.add(ArchiveIntent(pixiv_illust_id="5", mode="suppressed"))
        session.add_all([RemoteBookmark(work_id=i, value=True, confirmed_at=REFERENCE) for i in (1, 2, 11, 25)])
    return database, Library(database)


def test_full_manifest_order_shapes_fallback_and_covering_plans(tmp_path):
    database, library = seed(tmp_path / "synthetic.db")
    observations = []
    plans = {}
    try:
        cases = []
        for sort in ("newest", "oldest", "added", "title"):
            for scope in ({}, {"artist_ids": (1,)}, {"types": ("manga",)}, {"favorites": True}):
                cases.append(LibraryFilter(sort=sort, **scope))
        for scope in (
            {"show_hidden": True}, {"recent": True, "as_of": REFERENCE}, {"q": "alpha"},
            {"tags": ("A",)}, {"tags": ()}, {"tags_exclude": ("A",)}, {"tags_exclude": ()},
            {"artist_ids_exclude": (1,)}, {"artist_ids": ()}, {"artist_ids": (1, 2)},
            {"types": ()}, {"types": ("illust", "manga")},
            {"artist_ids": (1,), "favorites": True}, {"types": ("illust",), "favorites": True},
            {"artist_ids": (1,), "types": ("illust",)},
            {"posted_after": REFERENCE - timedelta(days=3), "tz_offset_minutes": -480},
            {"posted_before": REFERENCE, "tz_offset_minutes": -480},
        ):
            filters = LibraryFilter(**scope)
            assert library._fast_scope(filters) is None
            cases.append(filters)

        for filters in cases:
            statements = []
            def record(_connection, _cursor, sql, parameters, _context, _many):
                statements.append((sql, parameters))
            began = perf_counter()
            expected = legacy_manifest(library, filters, 500)
            legacy_ms = (perf_counter() - began) * 1000
            event.listen(database.engine, "before_cursor_execute", record)
            try:
                began = perf_counter()
                actual = library.reading_manifest(filters, ceiling=500)
                projected_ms = (perf_counter() - began) * 1000
            finally:
                event.remove(database.engine, "before_cursor_execute", record)
            assert actual == expected
            shape_queries = [(sql, params) for sql, params in statements if "ix_works_settings_shape" in sql]
            fast = library._fast_scope(filters) is not None
            if fast:
                assert len(shape_queries) == 1
                source_reads = [sql for sql, _ in statements if "FROM works" in sql]
                assert len(source_reads) == 1 and "ix_works_settings_shape" in source_reads[0]
                with database.engine.connect() as connection:
                    for sql, params in statements:
                        if "FROM read_work_order" not in sql and "ix_works_settings_shape" not in sql:
                            continue
                        details = [row[3] for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params)]
                        assert all("COVERING INDEX" in detail for detail in details), details
                        assert not any("TEMP B-TREE" in detail for detail in details)
                        plans[sql] = details
            else:
                assert not shape_queries
            observations.append({"sort": filters.sort, "scope": str(filters), "fast": fast,
                "members": len(actual), "complete_legacy_fetch_ms": legacy_ms,
                "complete_projected_fetch_ms": projected_ms})
        assert library.reading_manifest(LibraryFilter(artist_ids=(999,)), ceiling=500) == []
        assert library.reading_manifest(ceiling=0) == []
        evidence = os.environ.get("PIXIV_MANIFEST_EVIDENCE")
        if evidence:
            Path(evidence).write_text(json.dumps({"synthetic_works": 501, "metadata_bytes_per_work": 8192,
                "timing_limit": "Small synthetic database; legacy measured first, no OS cache eviction; not physical HDD performance.",
                "cases": observations, "query_plans": plans}, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        database.close()


def test_manifest_keeps_one_snapshot_across_order_and_shape_reads(tmp_path, monkeypatch):
    database, library = seed(tmp_path / "snapshot.db", count=25)
    try:
        expected = legacy_manifest(library, LibraryFilter(), 25)
        seek = library._seek_rows
        changed = False
        def change_after_order(*args, **kwargs):
            nonlocal changed
            rows = seek(*args, **kwargs)
            if not changed:
                changed = True
                with database.write() as session:
                    session.add(WorkRevision(id="frozen-new", work_id=1, ordinal=2, title="Changed", type="manga",
                        page_count=9, source_signature="new", state="complete"))
                    session.flush()
                    work = session.get(Work, 1)
                    work.default_revision_id = work.latest_revision_id = "frozen-new"
                    work.type, work.page_count = "manga", 9
            return rows
        monkeypatch.setattr(library, "_seek_rows", change_after_order)
        assert library.reading_manifest(ceiling=25) == expected
        later = library.reading_manifest(ceiling=25)
        assert next(row for row in later if row["work_id"] == 1)["revision_id"] == "frozen-new"
        assert next(row for row in later if row["work_id"] == 1)["page_count"] == 9
    finally:
        database.close()


def test_create_prepares_first_random_order_without_page_hydration():
    members = [{"work_id": i, "revision_id": f"r-{i}", "page_count": i} for i in range(1, 5)]
    sessions = ReadingSessions(SimpleNamespace(reading_manifest=lambda *_args, **_kwargs: members))
    result = sessions.create(LibraryFilter())
    visit = sessions._visits[result["session_id"]]
    assert ("random", 0) in visit.orders
    assert result["page_total"] == 10 and result["total"] == 4
    assert [sessions.entry(result["session_id"], i)["work_id"] for i in range(4)] == [1, 2, 3, 4]
    order, starts = visit.order("random", 0)
    assert {member.work_id for member in order} == {1, 2, 3, 4} and starts[-1] == 10
