"""Synthetic media lookup, feed window, and HTTP conditional-request probes."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "app"))

from src.routes.archive_api import create_router  # noqa: E402


def media_probe():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE artists(id INTEGER PRIMARY KEY, avatar_path TEXT);
        CREATE TABLE works(id INTEGER PRIMARY KEY, pixiv_illust_id TEXT);
        CREATE TABLE assets(id TEXT PRIMARY KEY, work_id INTEGER, relative_path TEXT);
        CREATE INDEX ix_assets_work_id ON assets(work_id);
        CREATE TABLE derivatives(id TEXT PRIMARY KEY, work_id INTEGER, relative_path TEXT);
        CREATE INDEX ix_derivatives_work_id ON derivatives(work_id);
        CREATE TABLE archive_intents(pixiv_illust_id TEXT PRIMARY KEY, mode TEXT);
    """)
    connection.executemany("INSERT INTO artists VALUES(?,?)", ((i, f"avatar/{i}.jpg") for i in range(1, 84)))
    connection.executemany("INSERT INTO works VALUES(?,?)", ((i, str(i)) for i in range(1, 15245)))
    connection.executemany("INSERT INTO assets VALUES(?,?,?)", (
        (str(i), (i % 15244) + 1, f"work/{i}.jpg") for i in range(1, 44993)))
    connection.executemany("INSERT INTO derivatives VALUES(?,?,?)", (
        (str(i), (i % 15244) + 1, f"thumb/{i}.webp") for i in range(1, 15245)))
    query = """SELECT assets.work_id FROM assets JOIN works ON works.id=assets.work_id
        WHERE assets.relative_path=? AND works.pixiv_illust_id NOT IN
        (SELECT archive_intents.pixiv_illust_id FROM archive_intents
        WHERE archive_intents.mode='suppressed') LIMIT 1"""

    def batch_ms():
        began = perf_counter()
        for i in range(44893, 44993):
            connection.execute(query, (f"work/{i}.jpg",)).fetchone()
        return round((perf_counter() - began) * 1000, 2)

    before_plan = connection.execute("EXPLAIN QUERY PLAN " + query, ("work/44992.jpg",)).fetchall()
    before_ms = batch_ms()
    connection.execute("CREATE INDEX synthetic_assets_relative_path ON assets(relative_path)")
    after_plan = connection.execute("EXPLAIN QUERY PLAN " + query, ("work/44992.jpg",)).fetchall()
    after_ms = batch_ms()
    connection.close()
    return {"works": 15244, "assets": 44992, "derivatives": 15244, "artists": 83,
            "request_count": 100, "before_plan": before_plan, "before_ms": before_ms,
            "after_plan": after_plan, "after_ms": after_ms,
            "note": "Pure in-memory SQL microbenchmark; excludes HTTP, filesystem, ORM, and real-library latency."}


def feed_probe():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE works(id INTEGER PRIMARY KEY, pixiv_illust_id TEXT,
            type TEXT, page_count INTEGER, posted_at TEXT);
        CREATE INDEX ix_works_order ON works(posted_at,id);
        CREATE TABLE archive_intents(pixiv_illust_id TEXT PRIMARY KEY,mode TEXT);
        CREATE TABLE local_preferences(work_id INTEGER PRIMARY KEY,is_hidden INTEGER);
    """)
    connection.executemany("INSERT INTO works VALUES(?,?,?,?,?)", (
        (i, str(i), "illust", 3, f"2025-01-{(i % 28) + 1:02d}") for i in range(1, 15245)))
    query = """WITH ranges AS (
        SELECT works.id AS work_id, works.page_count,
        row_number() OVER (ORDER BY works.posted_at DESC, works.id DESC)-1 AS work_index,
        sum(works.page_count) OVER
          (ORDER BY works.posted_at DESC, works.id DESC
           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)-works.page_count AS page_start
        FROM works WHERE works.id<=? AND works.pixiv_illust_id NOT IN
          (SELECT pixiv_illust_id FROM archive_intents WHERE mode='suppressed')
          AND works.id NOT IN
          (SELECT work_id FROM local_preferences WHERE is_hidden=1))
        SELECT work_id,work_index,page_start FROM ranges
        WHERE page_start<? AND page_start+page_count>? ORDER BY page_start"""
    plan = connection.execute("EXPLAIN QUERY PLAN " + query, (15244, 40, 0)).fetchall()
    windows = []
    for offset in (0, 22000, 45000):
        began = perf_counter()
        for _ in range(10):
            rows = connection.execute(query, (15244, offset + 40, offset)).fetchall()
        windows.append({"offset": offset, "returned_works": len(rows),
                        "ten_windows_ms": round((perf_counter() - began) * 1000, 2)})
    connection.close()
    return {"works": 15244, "pages_per_work": 3, "window_pages": 40,
            "plan": plan, "windows": windows,
            "note": "Equivalent single window SQL in memory; actual feed also computes total and loads pages/media."}


def conditional_probe():
    path = ROOT / "app" / "src" / "version.py"

    class SyntheticRuntime:
        def media_path(self, relative):
            return path if relative == "synthetic" else None

    app = FastAPI()
    app.include_router(create_router(lambda: SyntheticRuntime()))
    with TestClient(app) as client:
        first = client.get("/images/synthetic")
        etag = client.get("/images/synthetic", headers={"If-None-Match": first.headers["etag"]})
        modified = client.get("/images/synthetic", headers={
            "If-Modified-Since": first.headers["last-modified"]})
    return {"served_file": "app/src/version.py (synthetic path adapter)",
            "initial": {"status": first.status_code, "body_bytes": len(first.content),
                        "cache_control": first.headers.get("cache-control"),
                        "has_etag": "etag" in first.headers},
            "matching_etag": {"status": etag.status_code, "body_bytes": len(etag.content)},
            "matching_modified_since": {"status": modified.status_code,
                                        "body_bytes": len(modified.content)}}


if __name__ == "__main__":
    print(json.dumps({"media_lookup": media_probe(), "feed_window": feed_probe(),
                      "conditional_http": conditional_probe()}, ensure_ascii=False, indent=2))
