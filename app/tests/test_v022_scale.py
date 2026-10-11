"""Bounded queries against a generated 5,000-work library, never real data."""
import json
from time import perf_counter

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from client_fixture import make_library
from src.core.library import LibraryFilter
from src.core.models import Asset, Derivative, OperationItem, Work, utcnow
from src.routes.archive_api import create_router


def test_large_library_filters_positions_and_bounded_artist_queries(tmp_path):
    started = perf_counter()
    runtime, _ = make_library(tmp_path / "generated-library", works=5000)
    metrics = {"fixture_seconds": round(perf_counter() - started, 3)}
    try:
        library = runtime.library
        statements, loaded = [], []

        def count_statement(connection, cursor, sql, parameters, context, many):
            statements.append(sql)

        def load(record, context):
            loaded.append(type(record).__name__)

        event.listen(runtime.database.engine, "before_cursor_execute", count_statement)
        for model in (Work, Asset, Derivative, OperationItem):
            event.listen(model, "load", load)
        try:
            began = perf_counter()
            artists = library.artist_page(offset=0, limit=5, q="本地")
            metrics["artist_page_seconds"] = round(perf_counter() - began, 3)
            metrics["artist_page_queries"] = len(statements)
            # Five indexed top-12 author heads plus fixed projection/media
            # batches replace the old N-wide aggregation. Resource hydration
            # remains bounded to this page, including the 2,000-page work.
            assert len(statements) <= 16
            assert sum("SELECT work_id FROM read_source_work_local" in sql for sql in statements) == 5
            assert sum(sql.startswith("SELECT assets.id") for sql in statements) == 1
            assert sum(sql.startswith("SELECT derivatives.id") for sql in statements) == 1
            assert loaded.count("Work") == 60
            assert loaded.count("Asset") <= 60 and loaded.count("Derivative") <= 60
            assert "OperationItem" not in loaded
        finally:
            event.remove(runtime.database.engine, "before_cursor_execute", count_statement)
            for model in (Work, Asset, Derivative, OperationItem):
                event.remove(model, "load", load)
        assert artists["total"] == 12 and len(artists["items"]) == 5
        assert all(item["work_count"] > 0 and len(item["recent_works"]) == 12 for item in artists["items"])
        assert all(item["avatar"].startswith("/images/") for item in artists["items"])

        selected = LibraryFilter(q="本地", favorites=True, recent=True, as_of=utcnow(),
                                 artist_ids=(3,), tags=("插画",), sort="title")
        began = perf_counter()
        page = library.query(selected, limit=100)
        metrics["filtered_query_seconds"] = round(perf_counter() - began, 3)
        assert page["total"] == len(page["items"])
        assert page["total"] > 0
        ids = [item["id"] for item in page["items"]]
        assert 4994 in ids
        assert ids == sorted(ids)
        position = library.locate(4994, selected, ceiling=page["ceiling"])
        neighbors = library.neighbors(4994, selected, ceiling=page["ceiling"])
        assert neighbors["index"] == ids.index(4994)
        assert neighbors["previous"] == ids[ids.index(4994) - 1]
        assert library.feed(selected, offset=position["index"], limit=1,
                            ceiling=page["ceiling"])["items"][0]["work_id"] == 4994

        broad = LibraryFilter(sort="newest")
        assert library.query(broad, limit=1)["items"][0]["id"] == 5000
        last_page = library.detail(5000, page_offset=1999, page_limit=1)
        assert last_page["display_page_count"] == 2000
        assert [item["index"] for item in last_page["pages"]] == [1999]
        began = perf_counter()
        stream = library.feed(broad, offset=1988, limit=20, ceiling=5000)
        metrics["long_work_feed_seconds"] = round(perf_counter() - began, 3)
        assert len(stream["items"]) == 20
        assert stream["items"][11]["work_id"] == 5000
        assert stream["items"][11]["page"] == 1999
        assert stream["items"][12]["work_id"] == 4999

        app = FastAPI()
        app.include_router(create_router(lambda: runtime))
        with TestClient(app) as client:
            for query in ("favorites=bad", "recent=true", "sort=wrong", "as_of=invalid"):
                assert client.get("/api/library?" + query).status_code == 422
        (tmp_path / "generated-library" / "scale-metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        runtime.close()
