"""Count semantics and covering plans on a tiny in-memory library only."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.core.library import Library, LibraryFilter
from src.core.models import ArchiveIntent, Artist, Base, LocalPreference, RemoteBookmark, Work, WorkTag
from sqlalchemy import select


REFERENCE = datetime(2026, 10, 4, 12)


@pytest.fixture
def library():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add_all([Artist(id=i, pixiv_user_id=str(i), name=f"synthetic artist {i}") for i in (1, 2)])
        for i in range(1, 31):
            session.add(Work(id=i, pixiv_illust_id=str(100+i), artist_id=1 if i <= 15 else 2,
                title=f"synthetic {i}", caption="x" * 4096, source_metadata={"synthetic": "x" * 4096},
                type="manga" if i % 2 else "illust", posted_at=REFERENCE-timedelta(days=i),
                downloaded_at=REFERENCE-timedelta(days=i*2), time_basis="legacy_unknown" if i == 3 else "utc"))
        session.add_all([LocalPreference(work_id=i, is_hidden=True) for i in (1, 2, 15)])
        session.add_all([ArchiveIntent(pixiv_illust_id=str(i), mode="suppressed") for i in (102, 105, 999)])
        session.add_all([WorkTag(work_id=i, tag="A" if i % 2 else "B") for i in range(1, 31)])
        session.add_all([RemoteBookmark(work_id=i, value=True, confirmed_at=REFERENCE) for i in (3, 15, 20)])
    class MemoryDatabase:
        @contextmanager
        def read(self):
            with Session(engine) as session:
                session.connection().exec_driver_sql("BEGIN")
                yield session
    result = Library(MemoryDatabase())
    result.test_engine = engine
    try:
        yield result
    finally:
        engine.dispose()


def test_default_query_and_count_endpoints_use_covering_work_index(library):
    statements = []
    def capture(conn, cursor, sql, params, context, many):
        if sql.startswith("SELECT count(*)"):
            statements.append((sql, params))
    event.listen(library.test_engine, "before_cursor_execute", capture)
    try:
        assert library.query(ceiling=30, compact=True)["total"] == 26
        assert library.counts(ceiling=30)["total"] == 26
    finally:
        event.remove(library.test_engine, "before_cursor_execute", capture)
    plans = []
    with library.test_engine.connect() as connection:
        work_root = connection.exec_driver_sql("SELECT rootpage FROM sqlite_master WHERE type='table' AND name='works'").scalar()
        # First query total, then counts all/recent/favorites (total reuses all). Only default
        # totals can be covered without another filter's columns or relations.
        for position in (0, 1):
            sql, params = statements[position]
            plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params).all()
            vm = connection.exec_driver_sql("EXPLAIN " + sql, params).all()
            plans.append({"plan": [tuple(row) for row in plan], "opens_work_table": any(row[1] == "OpenRead" and row[3] == work_root for row in vm)})
    artifact = os.environ.get("LIBRARY_COUNT_ARTIFACTS")
    if artifact:
        Path(artifact).mkdir(parents=True, exist_ok=True)
        Path(artifact, "memory-count-plan.json").write_text(json.dumps(plans, indent=2), encoding="utf-8")
    assert len(statements) == 4
    assert all(any("works USING COVERING INDEX" in row[3] for row in item["plan"]) for item in plans), plans
    assert all(not item["opens_work_table"] for item in plans), plans


@pytest.mark.parametrize("favorites,recent,expected_queries", [
    (False, False, 3), (True, False, 3), (False, True, 3), (True, True, 4),
])
def test_counts_reuse_identical_scope_with_one_reference_time(library, monkeypatch,
        favorites, recent, expected_queries):
    import src.core.library as library_module
    calls, statements = [], []
    def clock():
        calls.append(None)
        return REFERENCE + timedelta(seconds=len(calls) - 1)
    monkeypatch.setattr(library_module, "utcnow", clock)
    def capture(conn, cursor, sql, params, context, many):
        if sql.startswith("SELECT count(*)"):
            statements.append((sql, params))
    event.listen(library.test_engine, "before_cursor_execute", capture)
    try:
        filters = LibraryFilter(favorites=favorites, recent=recent,
            tags=("A",), posted_before=REFERENCE, tz_offset_minutes=-480)
        result = library.counts(filters, ceiling=15)
    finally:
        event.remove(library.test_engine, "before_cursor_execute", capture)
    assert len(calls) == 1
    assert result["as_of"] == REFERENCE.isoformat()
    assert len(statements) == expected_queries
    if favorites and not recent:
        assert result["total"] == result["favorites"]
    elif recent and not favorites:
        assert result["total"] == result["recent"]
    elif not favorites and not recent:
        assert result["total"] == result["all"]


@pytest.mark.parametrize("filters", [
    LibraryFilter(), LibraryFilter(show_hidden=True), LibraryFilter(artist_ids=(1,)),
    LibraryFilter(artist_ids=()), LibraryFilter(artist_ids_exclude=(1,)),
    LibraryFilter(types=("manga",)), LibraryFilter(types=()), LibraryFilter(tags=("A",)),
    LibraryFilter(tags=()), LibraryFilter(tags_exclude=("A",)), LibraryFilter(q="synthetic 2"),
    LibraryFilter(q="synthetic%"), LibraryFilter(favorites=True, show_hidden=True),
    LibraryFilter(recent=True, as_of=REFERENCE),
    LibraryFilter(posted_after=REFERENCE-timedelta(days=10), tz_offset_minutes=-480),
    LibraryFilter(posted_before=REFERENCE-timedelta(days=10), tz_offset_minutes=-480),
    LibraryFilter(types=("manga",), tags=("A",), favorites=True, recent=True,
                  show_hidden=True, as_of=REFERENCE, sort="oldest"),
])
def test_count_scope_equals_gallery_and_reading_scope(library, filters):
    for ceiling in (4, 15, 30):
        page = library.query(filters, ceiling=ceiling, compact=True)
        manifest = library.reading_manifest(filters, ceiling=ceiling)
        assert page["total"] == len(page["items"]) == len(manifest)
        assert [item["id"] for item in page["items"]] == [item["work_id"] for item in manifest]
        assert library.counts(filters, ceiling=ceiling)["total"] == page["total"]


def test_default_window_selects_only_ids_without_opening_work_table(library):
    queries = []
    def capture(conn, cursor, sql, params, context, many):
        if sql.startswith("SELECT works.id"):
            queries.append((sql, params))
    event.listen(library.test_engine, "before_cursor_execute", capture)
    try:
        page = library.query(offset=15, limit=3, ceiling=30, compact=True)
        assert len(page["items"]) == 3
    finally:
        event.remove(library.test_engine, "before_cursor_execute", capture)
    sql, params = queries[0]
    with library.test_engine.connect() as connection:
        work_root = connection.exec_driver_sql("SELECT rootpage FROM sqlite_master WHERE type='table' AND name='works'").scalar()
        plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + sql, params).all()
        vm = connection.exec_driver_sql("EXPLAIN " + sql, params).all()
        evidence = {"plan":[tuple(row) for row in plan], "opens_work_table":any(row[1] == "OpenRead" and row[3] == work_root for row in vm), "sql":sql}
    artifact = os.environ.get("LIBRARY_COUNT_ARTIFACTS")
    if artifact:
        Path(artifact, "memory-window-plan.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert sql.startswith("SELECT works.id \nFROM works"), evidence
    assert any("works USING COVERING INDEX ix_works_order" in row[3] for row in plan), evidence
    assert not evidence["opens_work_table"], evidence


@pytest.mark.parametrize("sort", ["newest", "oldest", "added", "title"])
def test_two_phase_window_matches_direct_sql_order_with_filters(library, sort):
    with Session(library.test_engine) as session, session.begin():
        session.get(Work, 3).title = "Alpha"
        session.get(Work, 4).title = "alpha"
        session.get(Work, 4).posted_at = session.get(Work, 3).posted_at
        session.get(Work, 6).posted_at = None
    for filters in (LibraryFilter(sort=sort), LibraryFilter(sort=sort, show_hidden=True),
                    LibraryFilter(sort=sort, artist_ids=(2,)), LibraryFilter(sort=sort, types=("manga",)),
                    LibraryFilter(sort=sort, tags_exclude=("A",)), LibraryFilter(sort=sort, q="alpha"),
                    LibraryFilter(sort=sort, favorites=True, show_hidden=True),
                    LibraryFilter(sort=sort, recent=True, as_of=REFERENCE),
                    LibraryFilter(sort=sort, artist_ids=())):
        with library.database.read() as session:
            expected = list(session.scalars(library._statement(filters, 20).with_only_columns(Work.id).order_by(*library._order(filters))))
        assert 2 not in expected and 5 not in expected
        for offset in (0, 2, 15, 999):
            for compact in (True, False):
                page = library.query(filters, offset=offset, limit=3, ceiling=20, compact=compact)
                assert [item["id"] for item in page["items"]] == expected[offset:offset+3]
                assert page["total"] == len(expected)
                assert page["has_more"] == (offset + len(page["items"]) < len(expected))
