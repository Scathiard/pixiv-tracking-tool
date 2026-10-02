"""HTTP boundaries for maintenance and source observations; synthetic archive only."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.client import PixivRequestError
from src.core.models import ArchiveIntent, Artist, Asset, Operation, TrackingPolicy, Work
from src.core.runtime import Runtime
from src.core.source_status import DeletionEvidence
from src.routes.archive_api import create_router


class Remote:
    def __init__(self):
        self.profile_reads = []

    def get_account_id(self):
        return "synthetic-account"

    def get_artist_detail(self, remote_id):
        self.profile_reads.append(remote_id)
        return {"user_id": remote_id, "name": "Remote name", "biography": "Remote biography"}

    def get_all_artist_illusts(self, _):
        raise AssertionError("A homepage check must not discover works")

    def get_illust_detail(self, _):
        raise AssertionError("A homepage check must not fetch work metadata")

    def add_bookmark(self, *_):
        raise AssertionError("Source checks must not write bookmarks")

    delete_bookmark = add_bookmark


@pytest.fixture
def api(tmp_path):
    remote = Remote()
    runtime = Runtime(tmp_path / "synthetic.db", tmp_path / "images", create=True, client=remote)
    runtime.account_id = runtime.library.account_id = remote.get_account_id()
    with runtime.database.write() as session:
        for identifier, mode in ((1, "paused"), (2, "stopped"), (3, "active")):
            artist = Artist(id=identifier, pixiv_user_id=str(10 * identifier), name=f"Local artist {identifier}")
            artist.policy = TrackingPolicy(mode=mode)
            session.add(artist)
        for identifier in (1, 2):
            work = Work(id=identifier, pixiv_illust_id=str(100 * identifier),
                        artist_id=identifier, title=f"Local work {identifier}")
            work.assets = [Asset(role="image", page_index=0, state="missing")]
            session.add_all([work, ArchiveIntent(pixiv_illust_id=work.pixiv_illust_id)])
    app = FastAPI()
    app.include_router(create_router(lambda: runtime))
    with TestClient(app) as client:
        yield client, runtime, remote
    assert runtime.close()


def test_unified_submission_is_one_deduplicated_persistent_operation(api):
    client, runtime, remote = api
    first = client.post("/api/tasks", json={"kind": "reconcile"})
    second = client.post("/api/tasks", json={"kind": "reconcile"})
    assert first.status_code == second.status_code == 200
    assert first.json()["operation_id"] == second.json()["operation_id"]
    assert first.json()["created"] is True and second.json()["created"] is False
    assert remote.profile_reads == []
    with runtime.database.read() as session:
        operations = list(session.scalars(select(Operation)))
        assert len(operations) == 1
        assert operations[0].kind == "reconcile"
        assert operations[0].parameters == {}
        assert operations[0].label == "检查并补齐图库"
        assert operations[0].source == "manual"


@pytest.mark.parametrize("body", [
    {"kind": "reconcile", "artist_ids": [1], "work_ids": [1]},
    {"kind": "reconcile", "work_ids": [0]},
    {"kind": "reconcile", "work_ids": [-1]},
    {"kind": "reconcile", "token": "never-persist"},
    {"kind": "artist_source", "artist_ids": [1, 2]},
])
def test_maintenance_rejects_ambiguous_unsafe_or_private_task_parameters(api, body):
    client, runtime, remote = api
    assert client.post("/api/tasks", json=body).status_code == 422
    assert runtime.queue.snapshot()["next_round_count"] == 0
    assert remote.profile_reads == []


def test_explicit_empty_range_remains_empty_instead_of_becoming_all(api):
    client, runtime, _ = api
    response = client.post("/api/tasks", json={"kind": "reconcile", "work_ids": []})
    assert response.status_code == 200
    with runtime.database.read() as session:
        operation = session.get(Operation, response.json()["operation_id"])
        assert operation.parameters == {"work_ids": []}


def test_homepage_check_runs_only_selected_artist_and_preserves_local_profile(api):
    client, runtime, remote = api
    account = remote.get_account_id()
    version = runtime.source_status.begin("artist", 1, account)
    runtime.source_status.apply_failure("artist", 1,
        PixivRequestError(404, "synthetic", failure_code="artist_unavailable", retryable=False), version, account)
    assert client.get("/api/library/source-status?kind=artists").json()["total"] == 1
    first = client.post("/api/artists/1/check-source").json()
    duplicate = client.post("/api/artists/1/check-source").json()
    assert first["operation_id"] == duplicate["operation_id"]
    assert remote.profile_reads == []
    assert runtime.queue.run_round()
    assert remote.profile_reads == ["10"]
    artist = client.get("/api/artists/1").json()
    assert artist["name"] == "Local artist 1"
    assert artist["availability"]["state"] == "available"
    assert client.get("/api/library/source-status?kind=artists").json()["total"] == 0
    with runtime.database.read() as session:
        assert session.get(TrackingPolicy, 1).mode == "paused"
        assert session.get(TrackingPolicy, 2).mode == "stopped"
    assert client.post("/api/artists/999/check-source").status_code == 404


def test_source_list_is_local_paginated_and_separates_evidence_from_404(api):
    client, runtime, remote = api
    account = remote.get_account_id()
    version = runtime.source_status.begin("work", 1, account)
    runtime.source_status.apply_failure("work", 1,
        PixivRequestError(404, "synthetic", failure_code="work_unavailable", retryable=False), version, account)
    version = runtime.source_status.begin("work", 2, account)
    evidence = DeletionEvidence("200", "synthetic-explicit-deletion-fixture", explicit=True, verified=True)
    runtime.source_status.apply_deleted("work", 2, evidence, version, account)
    for _ in range(3):
        overview = client.get("/api/library/source-status?kind=works&limit=1").json()
        assert overview["total"] == 2 and len(overview["items"]) == 1
        assert overview["counts"]["works"] == {"deleted": 1, "unavailable": 1}
    last = client.get("/api/library/source-status?kind=works&offset=1&limit=1").json()
    assert [row["id"] for row in last["items"]] == [2]
    deleted = client.get("/api/library/source-status?group=deleted").json()
    pending = client.get("/api/library/source-status?group=unavailable").json()
    assert [row["id"] for row in deleted["items"]] == [2]
    assert [row["id"] for row in pending["items"]] == [1]
    assert pending["items"][0]["availability"]["state"] == "unavailable"
    assert pending["items"][0]["local"]["state"] == "incomplete"
    assert any(gap["role"] == "image" for gap in pending["items"][0]["local"]["missing"])
    assert remote.profile_reads == []
    assert client.get("/api/library?show_hidden=1").json()["total"] == 2


@pytest.mark.parametrize("query", ["kind=unknown", "group=private", "limit=101", "limit=0", "offset=-1"])
def test_source_list_validates_pagination_and_group(api, query):
    client, _, remote = api
    assert client.get("/api/library/source-status?" + query).status_code == 422
    assert remote.profile_reads == []
