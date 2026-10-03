"""Daily operations use synthetic identity and local application state only."""
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from src.core.database import ArchiveDatabase
from src.core.models import ApplicationState, ArchiveIntent, Artist, Asset, LocalPreference, Operation, RevisionAsset, Work, WorkRevision
from src.routes.artists_api import create_artists_router, artist_identity
from src.scheduler import interval_snapshot, save_interval


@pytest.fixture
def daily(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "daily.db")
    with db.write() as session:
        session.add(Artist(id=1, pixiv_user_id="123", name="existing"))
    yield db
    db.close()


def test_identity_supports_id_and_pixiv_homepage_and_rejects_other_hosts():
    assert artist_identity(" 123 ") == "123"
    assert artist_identity("https://www.pixiv.net/en/users/123?x=1") == "123"
    assert artist_identity("名字") is None
    with pytest.raises(ValueError):
        artist_identity("https://example.org/users/123")


def test_identity_preview_is_bounded_read_only_and_exposes_duplicate(daily):
    class Remote:
        calls = []
        def get_artist_detail(self, value):
            self.calls.append(("detail", value))
            return {"user_id":value,"name":"preview", "biography":"identity", "avatar_url":None}
        def search_artist(self, value):
            self.calls.append(("search", value))
            return [{"user_id":str(123 + i),"name":str(i)} for i in range(40)]
    remote = Remote()
    app = FastAPI(); app.include_router(create_artists_router(lambda: SimpleNamespace(database=daily, remote=remote)))
    with TestClient(app) as client:
        preview = client.get("/api/artists/preview", params={"query":"https://www.pixiv.net/users/123"}).json()
        assert preview["items"][0]["artist_id"] == 1
        assert preview["items"][0]["biography"] == "identity"
        assert preview["identity"] is True
        search = client.get("/api/artists/preview", params={"query":"名字"}).json()
        assert len(search["items"]) == 20
        assert client.get("/api/artists/preview", params={"query":"https://bad.example/users/123"}).status_code == 422
        assert client.get("/api/artists/preview/123").json()["artist_id"] == 1
    with daily.read() as session:
        assert session.get(Artist, 1).name == "existing"
    assert remote.calls == [("detail","123"),("search","名字"),("detail","123")]


def test_preview_never_exhausts_remote_candidates_or_returns_arbitrary_errors(daily):
    class Remote:
        def search_artist(self,value):
            for index in range(20):yield {"user_id":str(index),"name":"synthetic"}
            raise AssertionError("Candidate continuation must not be consumed")
        def get_artist_detail(self,value):raise ValueError("SECRET remote error")
    app=FastAPI();app.include_router(create_artists_router(lambda:SimpleNamespace(database=daily,remote=Remote())))
    with TestClient(app) as client:
        assert len(client.get('/api/artists/preview',params={'query':'名字'}).json()['items'])==20
        failed=client.get('/api/artists/preview',params={'query':'123'})
        assert failed.status_code==503 and 'SECRET' not in failed.text


def test_check_status_reports_matching_artist_only(daily):
    with daily.write() as session:
        session.add(Operation(id="one", kind="check", parameters={"artist_ids":[1]}, dedupe_key="one", label="one", result={"outcome":"failed"}))
        session.add(Operation(id="global", kind="check", parameters={}, dedupe_key="global", label="global"))
    app = FastAPI(); app.include_router(create_artists_router(lambda: SimpleNamespace(database=daily)))
    with TestClient(app) as client:
        assert client.get("/api/artists/1/check-status").json()["operation"]["id"] == "one"
        assert client.get("/api/artists/2/check-status").status_code == 404


def test_interval_persists_without_changing_pause_or_active_operations(daily, monkeypatch):
    import src.scheduler as scheduler
    class Scheduler:
        calls = []
        def reschedule_job(self, *args, **kwargs): self.calls.append((args, kwargs))
    live = Scheduler(); monkeypatch.setattr(scheduler, "_scheduler", live)
    tracker = SimpleNamespace(database=daily)
    with daily.write() as session:
        session.add(ApplicationState(key="automatic_paused",value=True))
        session.add(Operation(id="active-auto",kind="check",source="automatic",state="running",dedupe_key="active-auto",label="current batch",parameters={"round":"keep"}))
    first = save_interval(tracker, 75)
    result = interval_snapshot(daily)
    assert result["minutes"] == 75 and result["next_check_at"] == first.isoformat()
    assert live.calls[0][1]["minutes"] == 75
    assert live.calls[0][1]["next_run_time"] == first
    second = save_interval(tracker, 120)
    assert second > first and interval_snapshot(daily)["minutes"] == 120
    with pytest.raises(ValueError): save_interval(tracker, 0)
    assert interval_snapshot(daily)["minutes"] == 120
    with daily.read() as session:
        assert session.get(ApplicationState,"automatic_paused").value is True
        operation=session.get(Operation,"active-auto")
        assert operation.state=="running" and operation.parameters=={"round":"keep"}


def test_settings_distinguish_registration_verification_gaps_and_history(daily, monkeypatch):
    from src.routes.settings_api import create_settings_router
    import src.routes.settings_api as settings
    monkeypatch.setattr(settings, "lan_addresses", lambda port:[f"http://192.168.1.2:{port}"])
    with daily.write() as session:
        session.add_all([Work(id=1,pixiv_illust_id="100",artist_id=1,title="new",page_count=3,default_revision_id="v2"),
                         Work(id=2,pixiv_illust_id="200",artist_id=1,title="missing",page_count=1)])
        session.flush()
        session.add_all([WorkRevision(id="v1",work_id=1,ordinal=1,title="old",type="illust",page_count=1,source_signature="old"),
                         WorkRevision(id="v2",work_id=1,ordinal=2,title="new",type="illust",page_count=3,source_signature="new")])
        session.flush()
        session.add_all([Asset(id="old",work_id=1,revision_id="v1",role="image",page_index=0,state="verified",relative_path="old.png"),
                         Asset(id="new",work_id=1,revision_id="v2",role="image",page_index=0,state="verified",relative_path="new.png"),
                         Asset(id="unknown",work_id=1,revision_id="v2",role="image",page_index=1,state="unknown",relative_path="unknown.png")])
        session.flush()
        session.add_all([RevisionAsset(revision_id="v1",role="image",page_index=0,asset_id="old"),
                         RevisionAsset(revision_id="v2",role="image",page_index=0,asset_id="new"),
                         RevisionAsset(revision_id="v2",role="image",page_index=1,asset_id="unknown")])
    runtime = SimpleNamespace(database=daily,client=None,queue=SimpleNamespace(automatic_paused=lambda:True))
    app=FastAPI(); app.include_router(create_settings_router(lambda:runtime))
    with TestClient(app) as client:
        data=client.get("/api/settings").json()
    assert data["stats"] == {"artists":1,"works":2,"downloaded_pages":1,"registered_originals":2,"verified_originals":1,"missing_originals":2,"unverified_originals":1}
    assert data["lan_urls"] == [f"http://192.168.1.2:{data['current_port']}"]
    assert data["automatic_paused"] is True


def test_settings_use_default_version_shape_legacy_ugoira_and_suppression(daily, monkeypatch):
    from src.routes.settings_api import create_settings_router
    import src.routes.settings_api as settings
    monkeypatch.setattr(settings, "lan_addresses", lambda port: [])
    with daily.write() as session:
        session.add_all([
            Work(id=1, artist_id=1, pixiv_illust_id="1", title="latest shape differs", type="ugoira", page_count=8, default_revision_id="default"),
            Work(id=2, artist_id=1, pixiv_illust_id="2", title="legacy animation", type="ugoira", page_count=9),
            Work(id=3, artist_id=1, pixiv_illust_id="3", title="suppressed", page_count=1),
            Work(id=4, artist_id=1, pixiv_illust_id="4", title="no originals", page_count=2),
        ])
        session.flush()
        session.add_all([
            WorkRevision(id="old", work_id=1, ordinal=1, title="old", type="manga", page_count=3, source_signature="old"),
            WorkRevision(id="default", work_id=1, ordinal=2, title="default", type="manga", page_count=2, source_signature="default"),
            LocalPreference(work_id=1, is_hidden=True), ArchiveIntent(pixiv_illust_id="3", mode="suppressed"),
        ])
        session.flush()
        session.add_all([
            Asset(id="reused", work_id=1, revision_id="old", role="image", page_index=0, state="verified", relative_path="reused.png"),
            Asset(id="no-path", work_id=1, revision_id="default", role="image", page_index=1, state="unknown"),
            Asset(id="unbound", work_id=1, revision_id="default", role="ugoira_zip", state="verified", relative_path="latest.zip"),
            Asset(id="legacy-zip", work_id=2, role="ugoira_zip", state="unknown", relative_path="legacy.zip"),
            Asset(id="legacy-timing", work_id=2, role="ugoira_timing", state="verified", relative_path="timing.json"),
            Asset(id="suppressed", work_id=3, role="image", state="verified", relative_path="deleted.png"),
        ])
        session.flush()
        session.add_all([
            RevisionAsset(revision_id="old", role="image", page_index=0, asset_id="reused"),
            RevisionAsset(revision_id="default", role="image", page_index=0, asset_id="reused"),
            RevisionAsset(revision_id="default", role="image", page_index=1, asset_id="no-path"),
        ])
    runtime = SimpleNamespace(database=daily, client=None, queue=SimpleNamespace(automatic_paused=lambda: False))
    app = FastAPI(); app.include_router(create_settings_router(lambda: runtime))
    with TestClient(app) as client:
        assert client.get("/api/settings").json()["stats"] == {
            "artists": 1, "works": 4, "downloaded_pages": 1, "registered_originals": 2,
            "verified_originals": 1, "missing_originals": 3, "unverified_originals": 1,
        }
