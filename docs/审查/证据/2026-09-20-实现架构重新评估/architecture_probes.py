"""Read-only application assessment; all data is synthetic and retained in TEMP.

The real config module is replaced before importing app modules, so no .env,
credential, real database or image library is loaded. No app source is edited.
Run: py -3.12 architecture_probes.py
"""
from __future__ import annotations

import asyncio
import json
import platform
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import types
from datetime import datetime
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
PROJECT = EVIDENCE.parents[3]
APP = PROJECT / "app"


def isolate():
    root = Path(tempfile.mkdtemp(prefix="pixiv-architecture-20260920-"))
    sys.path.insert(0, str(APP))
    import src
    cfg = types.ModuleType("src.config")
    for name, value in {
        "APP_DIR": APP, "PROJECT_ROOT": PROJECT, "BUNDLE_DIR": None,
        "DATA_ROOT": root, "DATA_DIR": root / "data", "IMAGES_DIR": root / "images",
        "DATABASE_URL": f"sqlite:///{root / 'data' / 'synthetic.db'}",
        "PORT": 1919, "CHECK_INTERVAL_HOURS": 0, "CHECK_INTERVAL_MINUTES": 0,
        "PAGE_SIZE": 50, "PAGINATION_MODE": "paginate", "MAX_VISIBLE_ITEMS": 0,
        "PIXIV_REFRESH_TOKEN": "", "DOWNLOAD_WORKERS": 1, "REQUEST_DELAY": 1.0,
        "env_file": root / ".env",
    }.items():
        setattr(cfg, name, value)
    cfg.resource_path = lambda relative: str(APP / relative)
    def forbidden(*args, **kwargs):
        raise AssertionError("External I/O or configuration mutation forbidden in assessment")
    cfg.set_data_root = cfg.set_pagination = cfg.set_port = forbidden
    cfg.DATA_DIR.mkdir()
    cfg.IMAGES_DIR.mkdir()
    sys.modules["src.config"] = cfg
    src.config = cfg
    import requests
    requests.sessions.Session.request = forbidden
    return root


def duplicate_intents():
    from src.progress import JobManager
    manager = JobManager()
    entered, release = threading.Event(), threading.Event()
    executed = []
    def downloading():
        executed.append("download_artist")
        entered.set()
        if not release.wait(5):
            raise TimeoutError("probe barrier timeout")
    first, _ = manager.submit("download_artist", downloading, dedupe_key="artist-1")
    assert entered.wait(3)
    second, created = manager.submit("refresh_artist", lambda: executed.append("refresh_artist"), dedupe_key="artist-1")
    release.set()
    assert manager.wait_for_idle(5)
    manager.shutdown(wait=True)
    return {"same_task_id": first == second, "second_created": created, "executed": executed,
            "refresh_dropped": "refresh_artist" not in executed}


async def route_probes():
    import httpx
    from fastapi import FastAPI
    from src import models, web
    from src.routes import works
    models.Base.metadata.create_all(models.engine)
    with models.Session() as session:
        artist = models.TrackedArtist(pixiv_user_id="synthetic-1", name="Fixture", is_active=True)
        session.add(artist)
        session.flush()
        session.add(models.Illustration(pixiv_illust_id="synthetic-100", artist_id=artist.id,
            title="Synthetic", is_hidden=True, posted_at=datetime(2026, 1, 1), tags="[]"))
        session.commit()
    app = FastAPI()
    app.include_router(works.router)
    web.tracker = None
    real_session = works.Session
    def slow_session():
        time.sleep(0.2)
        return real_session()
    @app.get("/probe-sync-control")
    def control():
        with slow_session() as session:
            return {"count": session.query(models.Illustration).count()}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        deletion = await client.post("/illust/1/delete", data={"redirect_url": "/"})
        with models.Session() as session:
            remaining = session.query(models.Illustration).count()
        unhide = await client.post("/illust/1/unhide", data={"redirect_url": "/"})
        with models.Session() as session:
            hidden = session.get(models.Illustration, 1).is_hidden
        offline = {"logged_in": False, "delete_http": deletion.status_code, "rows_after_delete": remaining,
                   "delete_location": deletion.headers.get("location"), "unhide_http": unhide.status_code,
                   "hidden_after_unhide": hidden,
                   "limit": "Actual routes + ORM through ASGI; middleware/login not started; synthetic record only"}

        async def measure(url):
            ticks = []
            alive = True
            async def heartbeat():
                last = time.perf_counter()
                while alive:
                    await asyncio.sleep(0.005)
                    now = time.perf_counter()
                    ticks.append((now - last) * 1000)
                    last = now
            task = asyncio.create_task(heartbeat())
            await asyncio.sleep(0.02)
            start = time.perf_counter()
            response = await client.get(url)
            elapsed = (time.perf_counter() - start) * 1000
            await asyncio.sleep(0.02)
            alive = False
            await task
            return {"http": response.status_code, "request_ms": round(elapsed, 2), "max_heartbeat_gap_ms": round(max(ticks), 2)}
        works.Session = slow_session
        try:
            await client.get("/")  # warm template cache; not a measured run
            blocked = await measure("/")
            control_result = await measure("/probe-sync-control")
        finally:
            works.Session = real_session
    return {"offline_local_operations": offline,
            "event_loop": {"injected_synchronous_delay_ms": 200, "actual_home_route": blocked,
                "threadpool_control": control_result, "limit": "Fault injection demonstrates blocking, not real disk latency"}}


def query_experiment(root):
    db = sqlite3.connect(root / "query-experiment.db")
    db.execute("CREATE TABLE illustration (id INTEGER PRIMARY KEY, posted_at TEXT, is_hidden BOOLEAN, artist_id INTEGER)")
    def measurements(sql, args=()):
        samples = []
        count = 0
        for _ in range(7):
            start = time.perf_counter()
            rows = db.execute(sql, args).fetchall()
            samples.append((time.perf_counter() - start) * 1000)
            count = len(rows)
        return {"rows_materialized": count, "median_ms": round(statistics.median(samples), 3),
                "plan": [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + sql, args)]}
    results = []
    for n in (10000, 100000):
        db.execute("DELETE FROM illustration")  # synthetic table only
        db.execute("DROP INDEX IF EXISTS probe_sort")
        db.executemany("INSERT INTO illustration VALUES (?, ?, 0, ?)",
            ((i, f"2026-01-{1+(i%28):02}T{(i//28)%24:02}:{(i//672)%60:02}:00", i%100) for i in range(1,n+1)))
        db.commit()
        current = measurements("SELECT id, posted_at FROM illustration WHERE is_hidden IS NOT 1 ORDER BY posted_at DESC")
        db.execute("CREATE INDEX probe_sort ON illustration(posted_at DESC, id DESC) WHERE is_hidden IS NOT 1")
        anchor = db.execute("SELECT posted_at, id FROM illustration WHERE is_hidden IS NOT 1 ORDER BY posted_at DESC,id DESC LIMIT 1 OFFSET ?",(n//2,)).fetchone()
        proposed = measurements("SELECT id, posted_at FROM illustration WHERE is_hidden IS NOT 1 AND (posted_at,id)<(?,?) ORDER BY posted_at DESC,id DESC LIMIT 1",anchor)
        # Equivalence is checked under the proposed deterministic order; existing
        # query has no tie-breaker. This does not implement null-date semantics.
        expected = db.execute("SELECT id FROM illustration WHERE is_hidden IS NOT 1 ORDER BY posted_at DESC,id DESC LIMIT 1 OFFSET ?",(n//2+1,)).fetchone()[0]
        actual = db.execute("SELECT id FROM illustration WHERE is_hidden IS NOT 1 AND (posted_at,id)<(?,?) ORDER BY posted_at DESC,id DESC LIMIT 1",anchor).fetchone()[0]
        results.append({"works": n, "current_all_rows_query": current, "candidate_indexed_neighbor_query": proposed,
            "candidate_matches_stable_order": actual == expected})
    db.close()
    return {"results": results, "limit": "Synthetic SQLite SQL-shape experiment; not full ORM/API/UI or real-library benchmark. All dates non-null; 7 warm runs; total/rank counts excluded."}


def main():
    root = isolate()
    result = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "temporary_root": str(root)}
    result.update(asyncio.run(route_probes()))
    result["different_intents_same_key"] = duplicate_intents()
    result["query_scaling"] = query_experiment(root)
    from src import models, progress
    models.engine.dispose()
    progress.shutdown(wait=True)
    output = EVIDENCE / "architecture-probes.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
