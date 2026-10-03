"""Current-original totals on a large synthetic catalogue, including read latency."""
import json
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, update

from src.core.database import ArchiveDatabase
from src.core.models import ArchiveIntent, Artist, Asset, LocalPreference, RevisionAsset, Work, WorkRevision
from src.routes.settings_api import create_settings_router


def test_settings_large_catalogue_latency_and_current_revision_semantics(tmp_path, monkeypatch):
    import src.routes.settings_api as settings
    monkeypatch.setattr(settings, "lan_addresses", lambda port: [])
    count = 15_000
    db = ArchiveDatabase.create(tmp_path / "settings-scale.db")
    works, revisions, assets, refs, hidden, suppressed = [], [], [], [], [], []
    expected = {"artists": 1, "works": count, "downloaded_pages": 0, "registered_originals": 0,
                "verified_originals": 0, "missing_originals": 0, "unverified_originals": 0}
    for ident in range(1, count + 1):
        legacy = ident % 13 == 0
        ugoira = ident % 17 == 0
        excluded = ident % 23 == 0
        pages = 4 if ident % 3 == 0 else 3
        revision_id = None if legacy else f"current-{ident}"
        works.append({"id": ident, "artist_id": 1, "pixiv_illust_id": str(ident), "title": "synthetic",
                      "type": "ugoira" if ugoira else "manga", "page_count": pages,
                      "default_revision_id": revision_id})
        if not legacy:
            revisions.append({"id": revision_id, "work_id": ident, "ordinal": 2, "title": "current",
                              "type": "ugoira" if ugoira else "manga", "page_count": pages, "source_signature": revision_id})
            revisions.append({"id": f"old-{ident}", "work_id": ident, "ordinal": 1, "title": "old",
                              "type": "illust", "page_count": 1, "source_signature": f"old-{ident}"})
        if ident % 7 == 0:
            hidden.append({"work_id": ident, "is_hidden": True})
        if excluded:
            suppressed.append({"pixiv_illust_id": str(ident), "mode": "suppressed"})
        usable = 0
        for slot in range(1 if ugoira else pages):
            state = ("verified", "unknown", "missing", "failed")[ident % 4]
            path = "" if ident % 11 == 0 else f"synthetic/{ident}/{slot}"
            asset_id = f"original-{ident}-{slot}"
            # A default version can reuse an original created by its older version.
            owner_revision = f"old-{ident}" if not legacy and slot == 0 else revision_id
            assets.append({"id": asset_id, "work_id": ident, "revision_id": owner_revision,
                           "role": "ugoira_zip" if ugoira else "image", "page_index": slot,
                           "state": state, "relative_path": path})
            if not legacy:
                refs.append({"revision_id": revision_id, "role": "ugoira_zip" if ugoira else "image",
                             "page_index": slot, "asset_id": asset_id})
                if slot == 0:
                    refs.append({"revision_id": f"old-{ident}", "role": "ugoira_zip" if ugoira else "image",
                                 "page_index": slot, "asset_id": asset_id})
            if not excluded:
                expected["registered_originals"] += bool(path)
                expected["verified_originals"] += state == "verified"
                expected["downloaded_pages"] += state == "verified" and not ugoira
                expected["unverified_originals"] += state == "unknown" and bool(path)
                usable += bool(path) and state not in ("missing", "failed", "invalid", "mismatch", "corrupt")
        if not excluded:
            expected["missing_originals"] += (1 if ugoira else pages) - usable
        # These are genuine historical, unbound originals and out-of-range pages.
        if not legacy:
            assets.append({"id": f"history-{ident}", "work_id": ident, "revision_id": f"old-{ident}",
                           "role": "image", "page_index": 10, "state": "verified", "relative_path": "history"})
        if ugoira:
            assets.append({"id": f"timing-{ident}", "work_id": ident, "revision_id": revision_id,
                           "role": "ugoira_timing", "page_index": 0, "state": "verified", "relative_path": "timing"})
    with db.write() as session:
        session.execute(insert(Artist), [{"id": 1, "pixiv_user_id": "1", "name": "synthetic"}])
        for model, rows in [(Work, works), (WorkRevision, revisions), (Asset, assets), (RevisionAsset, refs),
                            (LocalPreference, hidden), (ArchiveIntent, suppressed)]:
            session.execute(insert(model), rows)
    runtime = SimpleNamespace(database=db, client=None, queue=SimpleNamespace(automatic_paused=lambda: False))
    app = FastAPI()
    app.include_router(create_settings_router(lambda: runtime))
    queries, starts = [], []
    def before(connection, cursor, statement, parameters, context, many):
        starts.append(time.perf_counter())
    def after(connection, cursor, statement, parameters, context, many):
        elapsed = time.perf_counter() - starts.pop()
        if statement.lstrip().upper().startswith("SELECT"):
            with db.engine.connect() as inspect:
                plan = inspect.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).fetchall()
            queries.append({"sql": statement, "seconds": elapsed, "plan": [list(row) for row in plan]})
    event.listen(db.engine, "before_cursor_execute", before)
    event.listen(db.engine, "after_cursor_execute", after)
    latencies = []
    try:
        with TestClient(app) as client:
            for _ in range(3):
                start = time.perf_counter()
                response = client.get("/api/settings")
                latencies.append(time.perf_counter() - start)
                assert response.status_code == 200
                assert response.json()["stats"] == expected
            # A subsequent read reflects writes immediately rather than a stale cache.
            with db.write() as session:
                session.execute(update(Asset).where(Asset.id == "original-1-0").values(state="verified"))
            expected["verified_originals"] += 1
            expected["downloaded_pages"] += 1
            expected["unverified_originals"] -= 1
            assert client.get("/api/settings").json()["stats"] == expected
    finally:
        event.remove(db.engine, "before_cursor_execute", before)
        event.remove(db.engine, "after_cursor_execute", after)
        (tmp_path / "settings-query-metrics.json").write_text(json.dumps({"works": count, "assets": len(assets),
            "revision_refs": len(refs), "latencies_seconds": latencies, "queries": queries}, indent=2), encoding="utf-8")
        db.close()
    # Keep the complete GET below one second on this fixed large input,
    # including query-plan instrumentation; timings are retained for comparison.
    assert max(latencies) < 1.0, latencies
