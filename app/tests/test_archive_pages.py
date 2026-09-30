import sqlite3
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src import config
from src.core.database import UpgradeRequired
from src.core.maintenance import MissingMediaMaintenance
from src.core.models import ArchiveIntent, Artist, Asset, TrackingPolicy, Work, WorkTag
from src.core.runtime import Runtime
from src.web import create_app


@pytest.fixture
def pages(tmp_path, monkeypatch):
    # Page/legacy-submit contracts need a controlled queue. Gap admission and
    # concurrent background work have dedicated maintenance/pipeline scenarios.
    monkeypatch.setattr(MissingMediaMaintenance, "tick", lambda self: None)
    runtime = Runtime(tmp_path / "data/pixiv_tracker.db", tmp_path / "images", create=True)
    with runtime.database.write() as session:
        artist = Artist(id=1, pixiv_user_id="1", name="artist")
        artist.policy = TrackingPolicy(mode="paused")
        work = Work(id=1, pixiv_illust_id="10", title="local image", artist=artist,
                    posted_at=datetime(2026, 9, 20), caption='<b>hello</b><script>alert(1)</script>')
        work.tags = [WorkTag(tag="tag&value")]
        work.assets = [Asset(role="image", relative_path="10_p0.png")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="10")])
    Image.new("RGB", (8, 6)).save(runtime.images / "10_p0.png")
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    monkeypatch.setattr(config, "env_file", tmp_path / "fixture.env")
    monkeypatch.setattr(config, "PAGE_SIZE", 50)
    app = create_app(lambda: runtime, authenticate=False, schedule=False)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 30000)) as client:
        yield client, runtime


def test_existing_pages_render_from_the_same_new_local_core(pages):
    client, runtime = pages
    index = client.get("/?artist_ids=1&tags=tag%26value")
    assert index.status_code == 200
    assert 'task-panel.js' in index.text
    assert "local image" in index.text and "tag%26value" in index.text
    assert "var _progressActive" not in index.text
    detail = client.get("/illust/1?artist_ids=1&tags=tag%26value")
    assert detail.status_code == 200
    assert '<script>alert(1)</script>' not in detail.text
    assert "/images/10_p0.png" in detail.text
    assert client.get("/artists").status_code == 200
    assert client.get("/settings").status_code == 200


def test_legacy_pages_preserve_literal_repeated_tag_in_navigation(pages):
    client, runtime = pages
    with runtime.database.write() as session:
        session.add(WorkTag(work_id=1, tag="逗号,标签"))
    literal = [("tags", "逗号,标签"), ("tags", "逗号,标签")]
    response = client.get("/", params=literal)
    assert response.status_code == 200 and "local image" in response.text
    assert response.text.count("%E9%80%97%E5%8F%B7%2C%E6%A0%87%E7%AD%BE") > 1
    assert client.get("/illust/1", params=literal).status_code == 200
    assert client.get("/").status_code == 200  # omitted tag selection remains unlimited


def test_legacy_submit_has_identifiable_persistent_result(pages):
    client, runtime = pages
    submission = client.post("/settings/validate-and-fix", headers={"Origin": "http://localhost"})
    assert submission.status_code == 200
    task_id = submission.json()["operation_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = client.get("/api/tasks/" + task_id).json()
        if result["state"] == "finished":
            break
        time.sleep(0.05)
    assert result["state"] == "finished"
    assert result["result"]["outcome"] == "succeeded"
    assert result["counts"] == {"succeeded": 1}
    assert result["phases"] == [{"phase": "verifying", "unit": "assets", "state": "succeeded", "count": 1}]
    assert client.get("/api/works/1").json()["archive"]["state"] == "complete"
    assert client.get("/api/status").json()["logged_in"] is False
    assert client.get("/api/login").status_code == 405
    assert client.post("/api/login", headers={"Origin": "http://localhost"}).status_code == 503


def test_saved_port_does_not_rewrite_running_links(pages, monkeypatch):
    client, runtime = pages
    monkeypatch.setattr(config, "LISTENING_PORT", 1919)
    client.post("/settings/port", data={"port": 2919}, headers={"Origin": "http://localhost"})
    status = client.get("/api/status").json()
    assert status["saved_port"] == 2919 and status["listening_port"] == 1919
    html = client.get("/settings").text
    assert 'href="http://localhost:1919"' in html
    assert 'value="2919"' in html


def test_old_schema_is_not_silently_upgraded_on_normal_start(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / "pixiv_tracker.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE illustration(id INTEGER PRIMARY KEY)")
    before = path.read_bytes()
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "images")
    app = create_app(authenticate=False, schedule=False)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 30000)) as client:
        assert client.get("/api/status").json()["upgrade_required"] is True
        assert client.get("/").status_code == 503
    assert path.read_bytes() == before
