"""Additional isolated contract checks; no network, real config, or real data."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

EVIDENCE = Path(__file__).resolve().parent
sys.path.insert(0, str(EVIDENCE.parents[3] / "app"))
FIXTURE = Path(tempfile.mkdtemp(prefix="pixiv-review-contract-"))
os.environ["DATA_ROOT"] = str(FIXTURE)
os.environ["PIXIV_REFRESH_TOKEN"] = ""

from gallery_dl import archive, job
from src import config, models, progress, tracker, web
from src.routes import artists, works

results = []


def check(name, callback):
    try:
        results.append({"case": name, "completed": True, "observation": callback()})
    except Exception as exc:
        results.append({"case": name, "completed": False, "error": str(exc)})


def missing_file_in_archive():
    database = FIXTURE / "synthetic-download-archive.db"
    db = archive.connect(str(database), "pixiv", "{id}{suffix}.{extension}")
    metadata = {"id": 123, "suffix":"_p0", "extension":"jpg"}
    db.add(dict(metadata))
    fake = SimpleNamespace(hooks={}, archive=db, pathfmt=SimpleNamespace(
        set_filename=Mock(), fix_extension=Mock(), exists=Mock(return_value=False)),
        handle_skip=Mock(), download=Mock())
    try:
        # Exercise gallery-dl's real archive decision without an extractor or HTTP.
        job.DownloadJob.handle_url(fake, "https://invalid.example/123.jpg", dict(metadata))
        assert fake.handle_skip.call_count == 1 and fake.download.call_count == 0
        return {"file_exists":False, "archive_entry_exists":True,
                "skip_calls":fake.handle_skip.call_count, "download_calls":fake.download.call_count,
                "filesystem_checked":fake.pathfmt.exists.call_count > 0}
    finally:
        db.close()


def single_download_scope():
    models.init_db()
    with models.Session() as session:
        artist = models.TrackedArtist(pixiv_user_id="123", name="Scope")
        session.add(artist)
        session.flush()
        for i in range(2):
            session.add(models.Illustration(pixiv_illust_id=str(12300+i), artist_id=artist.id, title="Missing"))
        aid = artist.id
        session.commit()
    instance = tracker.Tracker()
    with patch.object(instance, "_download_artist") as download:
        instance.download_single_artist(aid)
    actual = download.call_args
    return {"target_user_id":actual.args[0], "target_illust_id_passed":False,
            "clear_archive_passed":actual.kwargs.get("clear_archive", False),
            "missing_works_for_artist":2}


def old_upgrade_conflict():
    project = FIXTURE / "legacy"
    app = project / "app"
    old_images = project / "images" / "Artist 123"
    new_images = app / "images" / "Artist 123"
    old_images.mkdir(parents=True)
    new_images.mkdir(parents=True)
    (old_images / "12300_p0.jpg").write_bytes(b"wanted-original")
    (new_images / "unrelated.jpg").write_bytes(b"existing-target")
    (project / "data").mkdir()
    env = app / "synthetic-settings.txt"
    env.write_text("IMAGES_DIR=images\nDATA_DIR=data\n", encoding="utf-8")
    with patch.object(config, "PROJECT_ROOT", project), patch.object(config, "APP_DIR", app), \
         patch.object(config, "env_file", env), patch.dict(os.environ, {
             "IMAGES_DIR":"images", "DATA_DIR":"data", "DATA_ROOT":str(project)}):
        config._migrate_to_data_root()
        changed = os.environ["DATA_ROOT"] == str(app)
    assert changed and not (new_images / "12300_p0.jpg").exists()
    return {"target_artist_directory_already_existed":True, "configured_root_switched":changed,
            "expected_image_in_target":False, "original_retained":(old_images / "12300_p0.jpg").exists()}


def port_runtime_split():
    env = FIXTURE / "synthetic-port-settings.txt"
    env.write_text("PORT=1919\n", encoding="utf-8")
    before = config.PORT
    requested = 12345 if before != 12345 else 12346
    try:
        with patch.object(config, "env_file", env), patch.dict(os.environ, {"PORT":str(before)}):
            config.set_port(requested)
            result = {"web_listener_constant":web.PORT, "settings_and_tray_port":config.PORT,
                      "restart_not_performed":True}
        assert result["web_listener_constant"] != result["settings_and_tray_port"]
        return result
    finally:
        config.PORT = before


if __name__ == "__main__":
    with patch("requests.sessions.Session.request", side_effect=AssertionError("Network disabled")):
        for name, callback in (
            ("archive_skips_missing_file", missing_file_in_archive),
            ("single_download_uses_entire_artist", single_download_scope),
            ("legacy_upgrade_switches_after_conflict", old_upgrade_conflict),
            ("port_setting_changes_links_before_restart", port_runtime_split),
        ):
            check(name, callback)
    output = {"fixture":str(FIXTURE), "real_network_used":False, "results":results}
    (EVIDENCE / "contract-results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    progress.shutdown()
    models.engine.dispose()
    sys.exit(0 if all(item["completed"] for item in results) else 1)
