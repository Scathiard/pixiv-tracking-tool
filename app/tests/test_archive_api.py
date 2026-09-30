import threading
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from src.client import PixivClient, PixivRequestError, _checked_response
from src.routes.archive_api import create_router
from src.core.models import ArchiveIntent, Artist, Asset, Derivative, RemoteBookmark, TrackingPolicy, Work
from src.core.runtime import Runtime
from src.security import NetworkPolicy, RequestBoundaryMiddleware


@pytest.fixture
def archive_api(tmp_path):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    with runtime.database.write() as session:
        artist = Artist(id=1, pixiv_user_id="10", name="offline")
        artist.policy = TrackingPolicy(mode="paused")
        work = Work(id=1, pixiv_illust_id="100", artist=artist, title="local work", page_count=3)
        work.assets = [Asset(role="image", page_index=index, relative_path=f"100_p{index}.png") for index in range(3)]
        work.bookmark = RemoteBookmark(value=False)
        session.add_all([work, ArchiveIntent(pixiv_illust_id="100")])
    for index in range(3):
        Image.new("RGB", (8, 6), "red").save(runtime.images / f"100_p{index}.png")
    runtime.bookmarks.start()
    app = FastAPI()
    app.add_middleware(RequestBoundaryMiddleware, policy=NetworkPolicy.from_interfaces([]))
    app.include_router(create_router(lambda: runtime))
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 30000)) as client:
        yield client, runtime
    assert runtime.close()


def test_unlogged_local_browsing_organizing_and_task_feedback(archive_api):
    client, runtime = archive_api
    assert client.get("/api/library").json()["items"][0]["title"] == "local work"
    assert client.get("/images/100_p0.png").status_code == 200
    result = client.patch("/api/works/1/preferences", json={"hidden": True}, headers={"Origin": "http://localhost"})
    assert result.status_code == 200
    assert client.get("/api/library").json()["items"] == []
    assert client.get("/api/library?show_hidden=1").json()["total"] == 1
    response = client.post("/api/tasks", json={"kind": "verify", "work_ids": [1]}, headers={"Origin": "http://localhost"})
    assert response.status_code == 200 and response.json()["state"] == "queued"
    operation_id = response.json()["operation_id"]
    assert client.get("/api/tasks/" + operation_id).json()["state"] == "queued"
    runtime.queue.run_round()
    progress = client.get("/api/progress").json()["round"]
    assert progress["outcome"] == "succeeded"
    assert progress["total"] == progress["settled"] == 3
    assert progress["counts"] == {"succeeded": 3}
    assert client.get("/api/works/1").json()["archive"] == {
        "state": "complete", "total": 3, "verified": 3, "missing": 0, "unknown": 0}


def test_paused_and_stopped_artists_remain_filterable(archive_api):
    client, runtime = archive_api
    for mode in ("paused", "stopped"):
        response = client.patch("/api/artists/1/tracking", json={"mode": mode}, headers={"Origin": "http://localhost"})
        assert response.status_code == 200
        assert client.get("/api/library?artist_ids=1").json()["total"] == 1
    assert client.get("/api/library?artist_ids=").json()["total"] == 0
    assert client.get("/api/library?types=").json()["total"] == 0


def test_asset_and_detail_endpoints_reject_private_or_suppressed_files(archive_api):
    client, runtime = archive_api
    relative, path = runtime.publisher.staging()
    path.write_bytes(b"private test")
    assert client.get("/images/" + relative).status_code == 404
    runtime.publisher.suppress(1)
    assert client.get("/images/100_p0.png").status_code == 404
    assert client.get("/api/works/1").status_code == 404


def test_backfill_requires_explicit_selection_and_secret_parameters_are_rejected(archive_api):
    client, runtime = archive_api
    headers = {"Origin": "http://localhost"}
    assert client.post("/api/tasks", json={"kind": "backfill"}, headers=headers).status_code == 422
    assert client.post("/api/tasks", json={"kind": "check", "token": "never-persist"}, headers=headers).status_code == 422
    assert client.post("/api/works/delete", json={"work_ids": [1], "confirmed": False}, headers=headers).status_code == 422
    assert runtime.queue.snapshot()["next_round_count"] == 0


def test_offline_bookmark_write_requires_current_account_and_preserves_local_fact(archive_api):
    client, runtime = archive_api
    response = client.post("/api/works/1/bookmark", json={"desired": True, "request_id": str(uuid4())},
                           headers={"Origin": "http://localhost"})
    assert response.status_code == 422
    response = client.post("/api/works/1/bookmark", json={"desired": True, "request_id": str(uuid4()),
                           "expected_account_id": "other-account"}, headers={"Origin": "http://localhost"})
    assert response.status_code == 409
    state = client.get("/api/works/1/bookmark").json()
    assert state["value"] is False and state["cached"] is True
    assert state["operation_id"] is None
    with runtime.database.read() as session:
        record = session.get(RemoteBookmark, 1)
        assert record.value is False and record.confirmed_at is None


def test_slow_remote_artist_search_does_not_block_local_library(archive_api):
    client, runtime = archive_api
    entered, release = threading.Event(), threading.Event()
    errors = []

    class Remote:
        def search_artist(self, _):
            entered.set()
            if not release.wait(10):
                raise TimeoutError()
            return []

    runtime.client = Remote()

    def search():
        try:
            assert client.get("/api/artists/search?query=test").status_code == 200
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=search)
    thread.start()
    try:
        assert entered.wait(5)
        assert client.get("/api/library").status_code == 200
    finally:
        release.set()
        thread.join(10)
    assert not errors and not thread.is_alive()


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_parseable_http_errors_are_never_success(status):
    class Response:
        status_code = status

        def json(self):
            return {}

    with pytest.raises(PixivRequestError) as error:
        _checked_response(Response())
    assert error.value.status == status


def test_missing_restriction_metadata_remains_unknown():
    data = PixivClient._parse_illust({"id": 1, "title": "unknown", "image_urls": {}})
    assert data["x_restrict"] is None
