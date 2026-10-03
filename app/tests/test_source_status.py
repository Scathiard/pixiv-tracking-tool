"""Source checking and database-only overview use synthetic accounts/files only."""
from datetime import datetime

import pytest
from sqlalchemy import event

from src.client import PixivClient, PixivRequestError
from src.core.database import ArchiveDatabase
from src.core.library import Library
from src.core.models import ApplicationState, ArchiveIntent, Artist, Asset, Derivative, Work
from src.core.profiles import ArtistProfiles
from src.core.source_status import (ARTIST_PREFIX, ArtistSourceHandler, DeletionEvidence,
                                    SourceIdentityMismatch, SourceStatus, availability_snapshot)
from src.core.tasks import TaskQueue


@pytest.fixture
def archive(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    with db.write() as session:
        session.add_all([Artist(id=1, pixiv_user_id="100", name="old", biography="cached", avatar_path="old.webp"),
            Work(id=1, pixiv_illust_id="1000", artist_id=1, title="saved", page_count=3, caption_state="empty")])
    yield db
    db.close()


class Remote:
    account = "42"
    calls = []
    failure = None

    def get_account_id(self):
        return self.account

    def get_artist_detail(self, pixiv_id):
        self.calls.append(pixiv_id)
        if self.failure:
            raise self.failure
        return {"user_id": pixiv_id, "name": "new", "biography": "", "avatar_url": None}


@pytest.mark.parametrize("code,status,expected", [
    ("api_error", 404, "unavailable"), ("artist_unavailable", 404, "unavailable"),
    ("work_private", 400, "private"), ("api_private", 400, "private"),
    ("api_unauthorized", 401, None), ("api_forbidden", 403, None), ("api_rate_limit", 429, None),
    ("api_unavailable", 503, None), ("ugoira_metadata_unavailable", 404, None)])
def test_failure_never_means_deletion_or_erases_fact(archive, code, status, expected):
    source = SourceStatus(archive)
    version = source.begin("artist", 1)
    source.apply_available("artist", 1, {"user_id": "100"}, version)
    version = source.begin("artist", 1)
    source.apply_failure("artist", 1, PixivRequestError(status, "SECRET token arbitrary remote", failure_code=code), version)
    result = source.snapshot("artist", 1)
    assert result["state"] == (expected or "available")
    assert result["confirmed"] is True
    with archive.read() as session:
        value = session.get(ApplicationState, ARTIST_PREFIX + "100").value
        assert value["last_attempt"]["state"] == (expected or "failed")
        assert "SECRET" not in str(value)


def test_last_owned_observation_wins_and_confirmed_fact_survives_attempt(archive):
    source = SourceStatus(archive)
    old = source.begin("work", 1)
    new = source.begin("work", 1)
    assert source.apply_failure("work", 1, PixivRequestError(404, "Page not found"), new)
    fact = source.snapshot("work", 1)
    assert not source.apply_available("work", 1, {"illust_id": "1000"}, old)
    failed = source.begin("work", 1)
    source.apply_failure("work", 1, OSError("secret path"), failed)
    assert source.snapshot("work", 1) == fact
    restored = source.begin("work", 1)
    assert source.apply_available("work", 1, {"illust_id": "1000"}, restored)
    assert source.snapshot("work", 1)["state"] == "available"


def test_batch_reservations_are_monotonic_skip_suppression_and_protect_newer_fact(archive):
    with archive.write() as session:
        session.add_all([Work(id=2, pixiv_illust_id="2000", artist_id=1, title="suppressed"),
                         ArchiveIntent(pixiv_illust_id="2000", mode="suppressed")])
    source = SourceStatus(archive)
    versions = source.begin_many("work", [1, 2, 1, 999])
    assert versions == {1: 1}
    recent = source.begin("work", 1)
    assert source.apply_failure("work", 1, PixivRequestError(404, "synthetic"), recent)
    assert not source.apply_available("work", 1, {"illust_id": "1000"}, versions[1])
    assert source.begin_many("work", [1]) == {1: 3}
    assert source.begin_many("artist", [1, 999]) == {1: 1}


def test_identity_and_account_scope_protect_observations(archive):
    remote = Remote()
    source = SourceStatus(archive, remote)
    version = source.begin("artist", 1, "42")
    with pytest.raises(SourceIdentityMismatch):
        source.apply_available("artist", 1, {"user_id": "999"}, version, "42")
    remote.account = "43"
    assert not source.apply_available("artist", 1, {"user_id": "100"}, version, "42")
    remote.account = "42"
    assert source.apply_available("artist", 1, {"user_id": "100"}, version, "42")
    assert source.snapshot("artist", 1, "43")["confirmed"] is False


def test_verified_explicit_evidence_required_and_recovery_clears_marker(archive):
    source = SourceStatus(archive)
    version = source.begin("artist", 1)
    for evidence in (DeletionEvidence("100", "synthetic", explicit=True),
                     DeletionEvidence("999", "synthetic", explicit=True, verified=True)):
        with pytest.raises(ValueError):
            source.apply_deleted("artist", 1, evidence, version)
    source.apply_deleted("artist", 1, DeletionEvidence("100", "reviewed synthetic source", True, True), version)
    assert source.snapshot("artist", 1)["state"] == "deleted"
    version = source.begin("artist", 1)
    source.apply_available("artist", 1, {"user_id": "100"}, version)
    assert source.snapshot("artist", 1)["state"] == "available"
    assert availability_snapshot({"state": "deleted", "confirmed": False})["state"] == "unavailable"


def test_offline_preserves_cached_available_fact_but_known_other_account_requires_check(archive):
    source = SourceStatus(archive)
    for kind, identity in (("artist", {"user_id": "100"}), ("work", {"illust_id": "1000"})):
        version = source.begin(kind, 1, "42")
        assert source.apply_available(kind, 1, identity, version, "42")
        offline = source.snapshot(kind, 1, None)
        assert offline["state"] == "available" and offline["confirmation"] == "confirmed" and offline["cached"] is True
        different = source.snapshot(kind, 1, "43")
        assert different["state"] == "unavailable" and different["confirmation"] == "unconfirmed"
    library = Library(archive)
    assert library.source_overview()["total"] == 0
    assert library.source_overview(kind="artists")["total"] == 0
    library.account_id = "42"
    assert library.source_overview()["total"] == 0
    library.account_id = "43"
    assert library.source_overview()["total"] == 0
    assert library.source_overview(kind="artists")["total"] == 0


def test_unknown_derived_media_is_unverified_not_missing_in_artist_summary(archive):
    with archive.write() as session:
        artist = session.get(Artist, 1)
        artist.profile_refreshed_at = datetime(2026, 10, 3)
        work = session.get(Work, 1)
        work.page_count = 1
        session.add(Asset(work_id=1, role="image", state="verified", relative_path="synthetic.png"))
        session.add(Derivative(work_id=1, kind="thumbnail", state="unknown", relative_path="synthetic.webp"))
    source = SourceStatus(archive)
    source.apply_failure("artist", 1, PixivRequestError(404, "synthetic"), source.begin("artist", 1))
    row = Library(archive).source_overview(kind="artists")["items"][0]
    assert row["incomplete_work_count"] == 0
    assert row["local"] == {"state": "unknown", "missing": []}


def test_homepage_handler_does_not_refresh_profile_or_download(archive):
    remote = Remote()
    remote.calls = []
    source = SourceStatus(archive, remote)
    queue = TaskQueue(archive, {"artist_source": ArtistSourceHandler(source)})
    task = queue.submit("artist_source", {"artist_ids": [1]}, "核对主页")
    assert queue.run_round()
    assert remote.calls == ["100"]
    assert queue.details(task["operation_id"])["result"]["outcome"] == "succeeded"
    with archive.read() as session:
        artist = session.get(Artist, 1)
        assert (artist.name, artist.biography, artist.avatar_path) == ("old", "cached", "old.webp")
    remote.failure = PixivRequestError(429, "SECRET")
    task = queue.submit("artist_source", {"artist_ids": [1]}, "核对主页")
    assert queue.run_round()
    assert queue.details(task["operation_id"])["result"]["outcome"] == "failed"
    assert source.snapshot("artist", 1, "42")["state"] == "available"


def test_profile_avatar_failure_is_still_positive_source_fact(archive):
    class AvatarRemote(Remote):
        def get_artist_detail(self, pixiv_id):
            return {**super().get_artist_detail(pixiv_id), "avatar_url": "https://i.pximg.net/synthetic.png"}

    class Publisher:
        def staging(self):
            return None, None

    class Downloader:
        def download(self, *args):
            raise OSError("synthetic disk failure")

    source = SourceStatus(archive, AvatarRemote())
    profiles = ArtistProfiles(archive, Publisher(), source.remote, Downloader(), source_status=source)
    with pytest.raises(OSError):
        profiles.refresh(1)
    assert source.snapshot("artist", 1, "42")["state"] == "available"
    with archive.read() as session:
        artist = session.get(Artist, 1)
        assert artist.avatar_path == "old.webp" and artist.biography == ""


def _blocked(session, works):
    for work in works:
        work.source_metadata = {"remote_availability": {"state": "unavailable", "checked_at": "2026-10-03", "reason": "safe", "confirmed": True, "policy": "source-v2"}}


def test_overview_all_types_unregistered_pages_and_complete_archives(archive):
    with archive.write() as session:
        manga = session.get(Work, 1)
        manga.type = "manga"
        illust = Work(id=2, pixiv_illust_id="2000", artist_id=1, title="complete", caption_state="empty")
        motion = Work(id=3, pixiv_illust_id="3000", artist_id=1, title="motion", type="ugoira", caption_state="available")
        unknown = Work(id=4, pixiv_illust_id="4000", artist_id=1, title="unknown", caption_state="empty")
        session.add_all([illust, motion, unknown])
        _blocked(session, [manga, illust, motion, unknown])
        for work_id, page, state in ((1, 0, "verified"), (1, 1, "missing"), (2, 0, "verified"), (4, 0, "unknown")):
            session.add(Asset(work_id=work_id, role="image", page_index=page, state=state, relative_path=f"{work_id}_{page}.png"))
        for work_id in (1, 2, 4):
            session.add(Derivative(work_id=work_id, kind="thumbnail", state="verified", relative_path=f"{work_id}.webp"))
        session.add(Derivative(work_id=3, kind="gif", state="verified", relative_path="old.gif"))
    result = Library(archive).source_overview()
    assert result["counts"]["works"] == {"deleted": 0, "unavailable": 4}
    rows = {row["id"]: row for row in result["items"]}
    assert [item["page"] for item in rows[1]["local"]["missing"]] == [1, 2]
    assert rows[2]["local"]["state"] == "complete" and rows[2]["local"]["missing"] == []
    assert rows[2]["local"]["default_revision_id"] is None
    assert rows[2]["local"]["latest_revision_id"] is None
    assert {item["role"] for item in rows[3]["local"]["missing"]} == {"ugoira_zip", "ugoira_timing", "thumbnail"}
    assert rows[4]["local"]["state"] == "unknown" and rows[4]["local"]["missing"] == []
    assert all(row["availability"]["effective"] is True for row in rows.values())


def test_artist_summary_pagination_suppression_and_batched_reads(archive):
    source = SourceStatus(archive)
    source.apply_failure("artist", 1, PixivRequestError(404, "not found"), source.begin("artist", 1))
    with archive.write() as session:
        artist = session.get(Artist, 1)
        artist.profile_refreshed_at = datetime(2026, 10, 3)
        for number in range(2, 122):
            session.add(Artist(id=number, pixiv_user_id=str(number + 100), name=f"artist {number}"))
            session.add(ApplicationState(key=ARTIST_PREFIX + str(number + 100), value={"state": "unavailable", "confirmed": True, "policy": "source-v2"}))
        saved = session.get(Work, 1)
        saved.page_count = 1
        session.add(Asset(work_id=1, role="image", state="verified", relative_path="saved.png"))
        session.add(Derivative(work_id=1, kind="thumbnail", state="verified", relative_path="saved.webp"))
        hidden = Work(id=2, pixiv_illust_id="2000", artist_id=1, title="suppressed", caption_state="empty")
        _blocked(session, [hidden])
        session.add_all([hidden, ArchiveIntent(pixiv_illust_id="2000", mode="suppressed")])
    queries = []
    def record(*args):
        queries.append(args[2])
    event.listen(archive.engine, "before_cursor_execute", record)
    try:
        result = Library(archive).source_overview(kind="artists", limit=100)
    finally:
        event.remove(archive.engine, "before_cursor_execute", record)
    assert len(queries) <= 8
    assert result["total"] == 121 and len(result["items"]) == 100
    assert result["items"][0]["work_count"] == 1
    assert result["items"][0]["incomplete_work_count"] == 0
    assert result["items"][0]["local"] == {"state": "complete", "missing": []}
    assert Library(archive).source_overview(kind="artists", offset=120)["items"][0]["id"] == 121
    assert Library(archive).source_overview()["total"] == 0
    assert Library(archive).artist_detail(1)["availability"]["state"] == "unavailable"


@pytest.mark.parametrize("kind,group,offset,limit", [("x", "all", 0, 50), ("works", "x", 0, 50),
                                                  ("works", "all", -1, 50), ("works", "all", 0, 101)])
def test_overview_bounds(archive, kind, group, offset, limit):
    with pytest.raises(ValueError):
        Library(archive).source_overview(kind=kind, group=group, offset=offset, limit=limit)


def test_artist_client_validates_identity_and_zero_works_is_not_missing():
    client = PixivClient()
    client._get = lambda *args: {"user": {"id": 123, "name": "synthetic", "total_illusts": 0}}
    assert client.get_artist_detail("123")["user_id"] == "123"
    with pytest.raises(PixivRequestError):
        client.get_artist_detail("456")


@pytest.mark.parametrize("failure", [PixivRequestError(401, "synthetic", failure_code="work_unavailable"), PixivRequestError(429, "synthetic"), PixivRequestError(503, "synthetic"), TimeoutError("synthetic")])
def test_first_transient_observation_does_not_create_unreachable_source(archive, failure):
    source = SourceStatus(archive)
    for kind in ("artist", "work"):
        source.apply_failure(kind, 1, failure, source.begin(kind, 1))
        assert source.snapshot(kind, 1) is None
    library = Library(archive)
    assert library.source_overview()["total"] == 0
    assert library.source_overview(kind="artists")["total"] == 0


def test_legacy_weak_failure_is_kept_but_absent_from_source_overview(archive):
    with archive.write() as session:
        session.get(Work, 1).source_metadata = {"remote_availability": {"state":"unavailable", "confirmed":True}}
        session.add(ApplicationState(key=ARTIST_PREFIX + "100", value={"state":"unavailable", "confirmed":False}))
    library = Library(archive)
    assert library.source_overview()["total"] == 0
    assert library.source_overview(kind="artists")["total"] == 0
    assert library.artist_detail(1)["availability"]["effective"] is False
