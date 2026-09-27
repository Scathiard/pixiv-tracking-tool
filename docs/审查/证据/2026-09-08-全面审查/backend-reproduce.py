"""Read-only audit reproductions. Application source is untouched; all data is synthetic.

Run with Python 3.12. Results describe observed defects, not passing product tests.
Temporary fixtures are retained. No Pixiv requests or real tokens are used.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[3]
sys.path.insert(0, str(REPO / "app"))
FIXTURE = Path(tempfile.mkdtemp(prefix="pixiv-review-backend-"))
os.environ["DATA_ROOT"] = str(FIXTURE)
os.environ["PIXIV_REFRESH_TOKEN"] = ""
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from src import auth, client, config, data_migration, models, progress, tracker, web
from src.routes import settings, works
from src.file_index import decode_file_paths, encode_file_paths, scan_artist_files

RESULTS = []


def check(name, callback):
    try:
        detail = callback()
        RESULTS.append({"case": name, "completed": True, "observation": detail})
    except Exception as exc:
        RESULTS.append({"case": name, "completed": False, "error": f"{type(exc).__name__}: {exc}"})


def seed_artist(uid, name, specs):
    with models.Session() as session:
        artist = models.TrackedArtist(pixiv_user_id=str(uid), name=name)
        session.add(artist)
        session.flush()
        ids = []
        for spec in specs:
            work = models.Illustration(artist_id=artist.id, **spec)
            session.add(work)
            session.flush()
            ids.append(work.id)
        aid = artist.id
        session.commit()
    folder = config.IMAGES_DIR / tracker._dir_name(name, str(uid))
    folder.mkdir(parents=True)
    return aid, ids, folder


def incomplete_archive():
    aid, ids, folder = seed_artist(801, "Partial", [
        dict(pixiv_illust_id="81001", title="Three pages", page_count=3),
        dict(pixiv_illust_id="81002", title="Zero bytes", page_count=1),
    ])
    (folder / "81001_p0.jpg").write_bytes(b"synthetic-page")
    (folder / "81002_p0.jpg").write_bytes(b"")
    instance = tracker.Tracker()
    with patch.object(instance, "_download_artist") as downloader:
        instance.download_single_artist(aid)
        calls = downloader.call_count
    with models.Session() as session:
        state = [dict(expected=session.get(models.Illustration, wid).page_count,
                      indexed=len(decode_file_paths(session.get(models.Illustration, wid).file_paths)))
                 for wid in ids]
    assert calls == 0 and state[0]["indexed"] == 1 and state[1]["indexed"] == 1
    return {"works": state, "download_calls": calls, "zero_byte_file_accepted": True}


def download_failure_status():
    manager = progress.JobManager()
    instance = tracker.Tracker()
    folder = FIXTURE / "status-only"
    fake_job = SimpleNamespace(run=lambda: 4, status=4)
    try:
        with patch.object(progress, "_manager", manager), patch.object(auth, "_load_token", return_value={}), \
             patch.object(tracker.gdl_job, "DownloadJob", return_value=fake_job):
            def run():
                tid = progress.begin_task("Synthetic download")
                instance._download_artist("999", str(folder), tid)
            tid, _ = manager.submit("audit", run)
            result = manager.wait_for_job(tid, 5)
        assert result["status"] == "succeeded"
        return {"gallery_dl_return_status": 4, "application_status": result["status"], "errors": result["errors"]}
    finally:
        manager.shutdown()


def image_url_encoding():
    app = FastAPI()
    app.add_api_route("/images/{path:path}", web.serve_image)
    outcomes = []
    with TestClient(app) as http:
        for i, name in enumerate(("Normal", "Hash#Name", "Percent%20Name")):
            directory = config.IMAGES_DIR / tracker._dir_name(name, str(820 + i))
            directory.mkdir()
            (directory / "82001_p0.jpg").write_bytes(b"synthetic-image")
            url = scan_artist_files(directory, f"/images/{directory.name}")["82001"][0]
            outcomes.append({"name": name, "file_exists": True, "status": http.get(url).status_code})
    assert [x["status"] for x in outcomes] == [200, 404, 404]
    return outcomes


def same_size_corruption():
    source, target = FIXTURE / "copy-source", FIXTURE / "copy-target"
    (source / "data").mkdir(parents=True)
    (source / "images").mkdir()
    original = source / "images" / "one.jpg"
    original.write_bytes(b"AAAA")
    plan = data_migration.prepare_migration(source, target)
    copy2 = data_migration.shutil.copy2
    def corrupt(src, dst, *args, **kwargs):
        value = copy2(src, dst, *args, **kwargs)
        Path(dst).write_bytes(b"BBBB")
        return value
    with patch.object(data_migration.shutil, "copy2", side_effect=corrupt):
        report = data_migration.execute_migration(plan)
    equal = original.read_bytes() == (target / "images" / "one.jpg").read_bytes()
    assert not equal and report.database_ok
    return {"migration_returned_success": True, "same_size": True, "bytes_equal": equal, "source_retained": original.exists()}


def rename_commit_failure():
    aid, ids, folder = seed_artist(830, "Before", [dict(pixiv_illust_id="83001", title="Rename")])
    (folder / "83001_p0.jpg").write_bytes(b"synthetic-image")
    with models.Session() as session:
        artist = session.get(models.TrackedArtist, aid)
        with patch.object(session, "commit", side_effect=RuntimeError("injected commit failure")):
            try:
                tracker._sync_artist_name(artist, SimpleNamespace(get_artist_detail=lambda _: {"name": "After"}), session)
            except RuntimeError:
                session.rollback()
    with models.Session() as session:
        name = session.get(models.TrackedArtist, aid).name
    after = config.IMAGES_DIR / tracker._dir_name("After", "830")
    assert name == "Before" and not folder.exists() and after.exists()
    return {"database_name": name, "old_directory_exists": folder.exists(), "new_directory_exists": after.exists()}


def post_http_failure():
    aid, ids, _ = seed_artist(840, "HTTP", [dict(pixiv_illust_id="84001", title="Bookmark", is_bookmarked=False)])
    instance = tracker.Tracker()
    fake_response = SimpleNamespace(status_code=503, json=lambda: {"message": "Service unavailable"})
    with patch.object(works, "get_tracker", return_value=instance), \
         patch.object(client, "_get_auth_header", return_value={}), \
         patch.object(instance.client, "_throttle"), \
         patch.object(instance.client, "_do_post", return_value=fake_response):
        response = asyncio.run(works.toggle_bookmark(ids[0]))
    result = json.loads(response.body)
    assert result["ok"] and result["is_bookmarked"]
    return {"upstream_http_status": 503, "application_response": result}


def ugoira_timing_failure():
    aid, ids, folder = seed_artist(850, "Animation", [dict(pixiv_illust_id="85001", title="Two frames", type="ugoira")])
    archive = folder / "85001_p0.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for i, color in enumerate(("red", "blue")):
            buffer = io.BytesIO()
            Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
            bundle.writestr(f"{i:06}.png", buffer.getvalue())
    instance = tracker.Tracker()
    with models.Session() as session, patch.object(instance.client, "_get", side_effect=RuntimeError("metadata unavailable")):
        instance._convert_ugoira_zips(session, session.get(models.TrackedArtist, aid))
        session.commit()
    output = folder / "85001.gif"
    with Image.open(output) as gif:
        duration = gif.info.get("duration")
        frame_count = gif.n_frames
    assert output.exists() and not archive.exists() and not duration
    return {"gif_frames": frame_count, "gif_duration_ms": duration, "original_zip_exists": archive.exists()}


def timestamp_offset():
    aid, ids, _ = seed_artist(860, "Time", [dict(pixiv_illust_id="86001", title="Timezone",
        posted_at=tracker._parse_iso_date("2026-09-08T12:00:00+09:00"))])
    with models.Session() as session:
        actual = session.get(models.Illustration, ids[0]).posted_at
    expected = datetime.fromisoformat("2026-09-08T12:00:00+09:00").astimezone(timezone.utc).replace(tzinfo=None)
    assert actual != expected
    return {"stored": actual.isoformat(), "expected_utc": expected.isoformat(), "error_hours": (actual - expected).total_seconds() / 3600}


def migration_reload_failure():
    target = FIXTURE.parent / (FIXTURE.name + "-migrated")
    synthetic_env = FIXTURE / "synthetic-settings.txt"
    synthetic_env.write_text(f"DATA_ROOT={FIXTURE}\n", encoding="utf-8")
    instance = tracker.Tracker()
    old = {key: getattr(config, key) for key in ("DATA_ROOT", "DATA_DIR", "IMAGES_DIR", "DATABASE_URL")}
    manager = progress.JobManager()
    try:
        with patch.object(config, "env_file", synthetic_env), patch.object(progress, "_manager", manager), \
             patch.object(settings, "get_tracker", return_value=instance), \
             patch("src.scheduler.stop_scheduler"), patch("src.scheduler.start_scheduler") as start, \
             patch.object(models, "reinit_db", side_effect=RuntimeError("injected reload failure")), \
             patch.object(web, "tracker", instance):
            response = asyncio.run(settings.change_path(None, str(target)))
            outcome = {"http_status": response.status_code, "config_switched": config.DATA_ROOT == target,
                       "engine_still_old": str(FIXTURE / "data" / "pixiv_tracker.db") in str(models.engine.url),
                       "web_tracker_is_none": web.tracker is None,
                       "old_scheduler_restarted": start.call_count > 0,
                       "maintenance_released": not progress.maintenance_active()}
        assert outcome["http_status"] == 500 and outcome["config_switched"] and outcome["engine_still_old"]
        return outcome
    finally:
        for key, value in old.items():
            setattr(config, key, value)
        os.environ["DATA_ROOT"] = str(FIXTURE)
        progress.end_maintenance()
        manager.shutdown()


def query_shape():
    with models.engine.connect() as connection:
        plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN SELECT id, posted_at FROM illustration ORDER BY posted_at DESC").all()
    return {"detail_navigation_query_plan": [row[-1] for row in plan], "detail_route_fetches_all_rows": True}


if __name__ == "__main__":
    # Reject accidental network calls, even if a reproduction is edited later.
    with patch.object(socket, "create_connection", side_effect=AssertionError("Network disabled in review")), \
         patch("requests.sessions.Session.request", side_effect=AssertionError("HTTP disabled in review")):
        models.init_db()
        for name, callback in (
            ("partial_and_zero_byte_archives", incomplete_archive),
            ("gallery_dl_nonzero_status", download_failure_status),
            ("image_url_special_characters", image_url_encoding),
            ("migration_same_size_corruption", same_size_corruption),
            ("rename_commit_failure", rename_commit_failure),
            ("bookmark_http_failure", post_http_failure),
            ("ugoira_missing_timing", ugoira_timing_failure),
            ("posted_at_timezone", timestamp_offset),
            ("migration_reload_failure", migration_reload_failure),
            ("query_shape", query_shape),
        ):
            check(name, callback)
    document = {"fixture": str(FIXTURE), "real_network_used": False, "results": RESULTS}
    (EVIDENCE / "backend-results.json").write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(document, ensure_ascii=False, indent=2))
    progress.shutdown()
    models.engine.dispose()
    sys.exit(0 if all(item["completed"] for item in RESULTS) else 1)
