"""One-operation maintenance against synthetic files and remote responses only."""
import io
import json
import zipfile

import pytest
from PIL import Image
from sqlalchemy import select

from src.core.bookmarks import BookmarkService
from src.core.database import ArchiveDatabase
from src.core.media import CARD_MOTION_SPEC, Publisher
from src.core.models import ArchiveIntent, Artist, Asset, Derivative, LocalPreference, Operation, OperationItem, RemoteBookmark, TrackingPolicy, Work, utcnow
from src.core.reconcile import ReconcileHandler
from src.core.source_status import SourceStatus
from src.core.tasks import TaskQueue


def image_bytes(color="red", gif=False):
    out = io.BytesIO()
    first = Image.new("RGB", (16, 12), color)
    if gif:
        first.save(out, "GIF", save_all=True, append_images=[Image.new("RGB", (16, 12), "blue")], duration=[80, 120], loop=0)
    else:
        first.save(out, "PNG")
    return out.getvalue()


class Unavailable(Exception):
    failure_code = "work_unavailable"
    retryable = False
    status = 404
    user_message = "本次无法取得作品，已有存档保留"


class Client:
    def __init__(self):
        self.account, self.calls, self.failures, self.hook = "account-a", [], {}, None
        self.types = {}
        self.counts = {}

    def get_account_id(self):
        return self.account

    def get_illust_detail(self, remote_id):
        self.calls.append(remote_id)
        if self.hook:
            self.hook(remote_id)
        if remote_id in self.failures:
            raise self.failures[remote_id]
        return {"illust_id": remote_id, "title": "refreshed", "caption": "remote caption",
            "type": self.types.get(remote_id, "manga"), "page_count": self.counts.get(remote_id, 2),
            "is_bookmarked": True, "urls": [{"original": f"https://i.pximg.net/{remote_id}_p{page}.png"}
                for page in range(self.counts.get(remote_id, 2))]}

    def get_ugoira_metadata(self, remote_id):
        return {"frames": [{"file": "0.png", "delay": 80}, {"file": "1.png", "delay": 120}],
            "zip_urls": {"original": f"https://i.pximg.net/{remote_id}.zip"}}


class Downloader:
    def __init__(self):
        self.calls = []

    def download(self, url, target, report=None):
        self.calls.append(url)
        if url.endswith(".zip"):
            with zipfile.ZipFile(target, "w") as output:
                output.writestr("0.png", image_bytes())
                output.writestr("1.png", image_bytes("blue"))
        else:
            target.write_bytes(image_bytes())
        if report:
            report(target.stat().st_size, target.stat().st_size)


@pytest.fixture
def archive(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    root = tmp_path / "images"
    root.mkdir()
    client, downloader = Client(), Downloader()
    bookmarks = BookmarkService(db, client, current_account=lambda: client.account)
    publisher = Publisher(db, root)
    handler = ReconcileHandler(db, publisher, client, downloader, bookmarks=bookmarks, source_status=SourceStatus(db, client))
    queue = TaskQueue(db, {"reconcile": handler})
    with db.write() as session:
        artist = Artist(id=1, pixiv_user_id="10", name="cached artist", biography="cached bio", avatar_path="cached.png")
        artist.policy = TrackingPolicy(mode="stopped")
        session.add(artist)
    yield db, root, client, downloader, queue
    db.close()


def seed(archive, *, work_id=1, pages=2, missing=(), corrupt=(), animation=False, legacy=False):
    db, root, client, _, _ = archive
    remote_id = str(100 + work_id)
    client.types[remote_id] = "ugoira" if animation else "manga"
    client.counts[remote_id] = 1 if animation else pages
    with db.write() as session:
        work = Work(id=work_id, pixiv_illust_id=remote_id, artist_id=1, title="cached title", caption="cached caption",
            type="ugoira" if animation else "manga", page_count=1 if animation else pages,
            source_metadata={"original_backfill_requires_selection": legacy})
        work.preference = LocalPreference(is_hidden=True)
        work.bookmark = RemoteBookmark(value=False, account_id=client.account, version=1)
        session.add(work)
        session.add(ArchiveIntent(pixiv_illust_id=remote_id))
        for page in range(pages if not animation else 2):
            role = ("ugoira_zip", "ugoira_timing")[page] if animation else "image"
            relative = f"{remote_id}-{page}.png" if not animation else f"{remote_id}.zip" if page == 0 else f"{remote_id}.frames.json"
            if page not in missing:
                if page in corrupt:
                    (root / relative).write_bytes(b"corrupt")
                elif role == "ugoira_zip":
                    with zipfile.ZipFile(root / relative, "w") as output:
                        output.writestr("0.png", image_bytes())
                        output.writestr("1.png", image_bytes("blue"))
                elif role == "ugoira_timing":
                    (root / relative).write_text(json.dumps({"frames": client.get_ugoira_metadata(remote_id)["frames"]}), encoding="utf-8")
                else:
                    (root / relative).write_bytes(image_bytes())
            session.add(Asset(work_id=work_id, role=role, page_index=page if not animation else 0,
                state="unknown", relative_path=relative if page not in missing else None,
                source_url=f"https://i.pximg.net/{remote_id}_p{page}.png" if not animation else None))
    return remote_id


def run(archive, parameters=None):
    *_, queue = archive
    operation_id = queue.submit("reconcile", parameters or {}, "检查并补齐图库")["operation_id"]
    assert queue.run_round()
    return queue.details(operation_id)


def test_complete_originals_not_downloaded_snapshot_reused_and_stopped_hidden_kept(archive):
    db, _, client, downloader, _ = archive
    remote_id = seed(archive)
    result = run(archive)
    assert result["result"]["outcome"] == "succeeded"
    assert client.calls == [remote_id] and downloader.calls == []
    with db.read() as session:
        assert session.scalar(select(Operation).where(Operation.kind == "reconcile")) is not None
        assert len(list(session.scalars(select(Operation)))) == 1
        work = session.get(Work, 1)
        assert work.bookmark.value is True and work.caption == "remote caption"
        assert work.preference.is_hidden is True and work.artist.policy.mode == "stopped"
        assert all(asset.state == "verified" for asset in work.assets)
        assert work.derivatives[0].state == "verified"
    assert result["result"]["phase_snapshots"]["verifying:assets"] == {"phase": "verifying", "unit": "assets", "done": 2, "total": 2}


@pytest.mark.parametrize("kind", ["reconcile", "metadata"])
def test_valid_source_with_conflicting_page_count_stays_available_without_losing_local_pages(archive, kind):
    from src.core.archive import ArchiveHandler
    db, root, client, downloader, queue = archive
    remote_id = seed(archive, pages=6)
    client.counts[remote_id] = 2
    handler = queue.handlers["reconcile"]
    if kind == "metadata":
        queue.handlers[kind] = ArchiveHandler(kind, db, handler.publisher, client, downloader,
            bookmarks=handler.bookmarks, source_status=handler.source_status)
    operation_id = queue.submit(kind, {"work_ids": [1]}, "检查已有作品")["operation_id"]
    assert queue.run_round()
    assert queue.details(operation_id)["result"]["outcome"] in ("failed", "partial_failure")
    assert downloader.calls == [] and client.calls == [remote_id]
    with db.read() as session:
        work = session.get(Work, 1)
        assert work.page_count == 6 and len(work.assets) == 6
        assert work.source_metadata["remote_availability"]["state"] == "available"
        assert work.source_metadata["remote_availability"]["confirmed"] is True
        for asset in work.assets:
            assert (root / asset.relative_path).read_bytes() == image_bytes()


@pytest.mark.parametrize("gap", ["missing", "corrupt"])
def test_actual_missing_or_corrupt_page_repaired_without_redownloading_sibling(archive, gap):
    db, root, client, downloader, _ = archive
    remote_id = seed(archive, missing=(1,) if gap == "missing" else (), corrupt=(1,) if gap == "corrupt" else ())
    result = run(archive)
    assert result["result"]["outcome"] == "succeeded"
    assert downloader.calls == [f"https://i.pximg.net/{remote_id}_p1.png"]
    assert client.calls == [remote_id]
    with db.read() as session:
        assert all(asset.state == "verified" for asset in session.get(Work, 1).assets)


def test_full_verification_redecodes_registered_preview_and_rebuilds_corruption(archive):
    db, root, _, downloader, _ = archive
    seed(archive)
    run(archive)
    with db.read() as session:
        record = session.scalar(select(Derivative))
        path = root / record.relative_path
    path.write_bytes(b"corrupt")
    run(archive)
    with Image.open(path) as image:
        image.load()
    assert downloader.calls == []


def test_old_gif_kept_while_original_package_and_timing_filled(archive):
    db, root, client, downloader, _ = archive
    remote_id = seed(archive, animation=True, legacy=True, missing=(0, 1))
    (root / "old.gif").write_bytes(image_bytes(gif=True))
    with db.write() as session:
        session.add(Derivative(work_id=1, kind="gif", relative_path="old.gif", state="unknown"))
    assert run(archive)["result"]["outcome"] == "succeeded"
    assert downloader.calls == [f"https://i.pximg.net/{remote_id}.zip"]
    assert client.calls == [remote_id]
    with db.read() as session:
        work = session.get(Work, 1)
        assert all(asset.state == "verified" for asset in work.assets)
        assert work.source_metadata["original_backfill_requires_selection"] is False
        assert {(d.kind, d.spec_key) for d in work.derivatives} == {("gif", "v1"), ("thumbnail", "v1"), ("thumbnail", CARD_MOTION_SPEC)}
        assert all(d.state == "verified" for d in work.derivatives)


@pytest.mark.parametrize("animation", [False, True])
def test_offline_continues_local_preview_generation_and_other_works(archive, animation):
    db, _, client, downloader, _ = archive
    remote_id = seed(archive, animation=animation)
    client.failures[remote_id] = ConnectionError("must not be logged")
    seed(archive, work_id=2)
    result = run(archive)
    assert result["result"]["outcome"] == "partial_failure"
    assert downloader.calls == []
    with db.read() as session:
        first, second = session.get(Work, 1), session.get(Work, 2)
        assert first.caption == "cached caption" and second.caption == "remote caption"
        assert all(d.state == "verified" for d in first.derivatives)
        assert len(first.derivatives) == (3 if animation else 1)


def test_unavailable_work_retains_files_state_and_gif_only_rebuilds_local_cards(archive):
    db, root, client, downloader, _ = archive
    remote_id = seed(archive, animation=True, legacy=True, missing=(0, 1))
    gif = image_bytes(gif=True)
    (root / "old.gif").write_bytes(gif)
    with db.write() as session:
        session.add(Derivative(work_id=1, kind="gif", relative_path="old.gif", state="unknown"))
    client.failures[remote_id] = Unavailable()
    result = run(archive)
    assert result["result"]["outcome"] == "succeeded" and result["result"]["counts"]["skipped"] == 3
    assert downloader.calls == [] and (root / "old.gif").read_bytes() == gif
    with db.read() as session:
        work = session.get(Work, 1)
        assert work.caption == "cached caption" and work.preference.is_hidden
        assert work.artist.biography == "cached bio" and work.artist.avatar_path == "cached.png"
        assert work.artist.policy.mode == "stopped"
        assert work.source_metadata["remote_availability"]["state"] == "unavailable"
        assert work.source_metadata["original_backfill_requires_selection"] is True
        assert len(work.derivatives) == 3 and all(d.state == "verified" for d in work.derivatives)


@pytest.mark.parametrize("race", ["account", "version"])
def test_bookmark_read_only_old_response_cannot_overwrite_newer_action_or_account(archive, race):
    db, _, client, _, _ = archive
    seed(archive)
    def change(_):
        if race == "account":
            client.account = "account-b"
        else:
            with db.write() as session:
                fact = session.get(RemoteBookmark, 1)
                fact.version += 1
    client.hook = change
    run(archive)
    with db.read() as session:
        assert session.get(RemoteBookmark, 1).value is False
        assert session.get(RemoteBookmark, 1).account_id == "account-a"


def test_deletion_during_remote_response_does_not_import_or_download(archive):
    db, root, client, downloader, _ = archive
    seed(archive, missing=(1,))
    def suppress(_):
        with db.write() as session:
            intent = session.get(ArchiveIntent, "101")
            intent.mode, intent.revision = "suppressed", intent.revision + 1
    client.hook = suppress
    run(archive)
    assert downloader.calls == []
    with db.read() as session:
        assert session.get(Work, 1).caption == "cached caption"


def test_restart_completed_inspections_and_snapshot_not_replayed(archive):
    db, root, client, downloader, queue = archive
    remote_id = seed(archive, missing=(1,))
    operation_id = queue.submit("reconcile", {}, "manual")["operation_id"]
    round_id = queue._claim_round()
    assert queue._step("check", round_id)  # catalogue plan
    assert queue._step("check", round_id)  # one remote snapshot
    assert queue._step("local", round_id)  # one verified page
    with db.write() as session:
        op = session.get(Operation, operation_id)
        op.state = "interrupted"
    resumed = TaskQueue(db, queue.handlers)
    resumed.recover()
    assert resumed.run_round()
    assert client.calls == [remote_id]
    assert downloader.calls == [f"https://i.pximg.net/{remote_id}_p1.png"]
    with db.read() as session:
        completed = session.scalar(select(OperationItem).where(OperationItem.operation_id == operation_id,
            OperationItem.item_key.like("inspect-%"), OperationItem.kind == "reconcile_asset").order_by(OperationItem.position))
        assert completed.attempts == 1


def test_manual_only_and_exact_work_scope(archive):
    db, _, client, _, queue = archive
    seed(archive)
    second = seed(archive, work_id=2)
    with pytest.raises(ValueError):
        queue.submit("reconcile", {}, "automatic", source="automatic")
    run(archive, {"work_ids": [2]})
    assert client.calls == [second]
    with db.read() as session:
        assert session.get(Work, 1).caption == "cached caption"


def test_asset_and_derivative_same_id_keep_independent_inspection_keys(archive):
    db, root, _, downloader, _ = archive
    seed(archive)
    with db.write() as session:
        asset = session.scalar(select(Asset).where(Asset.work_id == 1, Asset.page_index == 0))
        (root / "preview.png").write_bytes(image_bytes())
        session.add(Derivative(id=asset.id, work_id=1, kind="thumbnail", relative_path="preview.png", state="unknown"))
    result = run(archive)
    assert result["result"]["outcome"] == "succeeded" and downloader.calls == []
    with db.read() as session:
        asset = session.scalar(select(Asset).where(Asset.work_id == 1, Asset.page_index == 0))
        keys = list(session.scalars(select(OperationItem.item_key)))
        assert f"inspect-asset-{asset.id}" in keys and f"inspect-derivative-{asset.id}" in keys


@pytest.mark.parametrize("scope", [None, {"artist_ids": [2]}, {"work_ids": [1]}])
def test_profiles_include_zero_work_artists_but_respect_explicit_work_scope(archive, scope):
    db, _, _, _, queue = archive
    seed(archive)
    with db.write() as session:
        artist = Artist(id=2, pixiv_user_id="20", name="empty stopped artist")
        artist.policy = TrackingPolicy(mode="stopped")
        session.add(artist)
    class Profiles:
        def __init__(self):
            self.calls = []
        def refresh(self, artist_id):
            self.calls.append(artist_id)
    profiles = Profiles()
    queue.handlers["reconcile"].profiles = profiles
    run(archive, scope)
    assert profiles.calls == ([1, 2] if scope is None else [2] if "artist_ids" in scope else [1])


def test_valid_zip_with_mismatched_timing_is_repaired_and_preserves_pair_boundary(archive):
    db, root, client, downloader, _ = archive
    remote_id = seed(archive, animation=True)
    (root / f"{remote_id}.frames.json").write_text(json.dumps({"frames": [{"file": "missing.png", "delay": 80}]}), encoding="utf-8")
    assert run(archive)["result"]["outcome"] == "succeeded"
    assert downloader.calls == []
    with db.read() as session:
        work = session.get(Work, 1)
        assert all(asset.state == "verified" for asset in work.assets)
        assert all(d.state == "verified" for d in work.derivatives)


def test_maintenance_does_not_create_duplicate_operation_during_reconcile(archive):
    from src.core.maintenance import MissingMediaMaintenance
    db, _, _, _, queue = archive
    seed(archive, missing=(1,))
    queue.submit("reconcile", {}, "manual")
    maintenance = MissingMediaMaintenance(db, queue, remote_available=lambda: True)
    assert maintenance.tick(force=True) == []
    with db.read() as session:
        assert len(list(session.scalars(select(Operation)))) == 1


@pytest.mark.parametrize("code", ["source_identity_mismatch", "api_forbidden"])
def test_nonretryable_identity_or_access_rejection_still_reports_failure(archive, code):
    _, _, client, downloader, _ = archive
    remote_id = seed(archive)
    class Rejected(Exception):
        failure_code = code
        retryable = False
        status = 403
    client.failures[remote_id] = Rejected()
    result = run(archive)
    assert result["result"]["outcome"] == "partial_failure"
    assert result["needs_attention"] is True and downloader.calls == []


def test_runtime_remote_identity_shared_by_library_source_projection(tmp_path):
    from src.core.runtime import Runtime
    client = Client()
    runtime = Runtime(tmp_path / "library.db", tmp_path / "images", create=True, client=client)
    try:
        with runtime.database.write() as session:
            session.add(Artist(id=1, pixiv_user_id="10", name="artist"))
        assert runtime.remote.get_account_id() == "account-a"
        version = runtime.source_status.begin("artist", 1, "account-a")
        assert runtime.source_status.apply_available("artist", 1, {"user_id": "10"}, version, "account-a")
        assert runtime.library.account_id == runtime.account_id == "account-a"
        assert runtime.source_status.snapshot("artist", 1, runtime.library.account_id)["state"] == "available"
    finally:
        runtime.close()


def test_unknown_404_is_unavailable_and_skips_unrecoverable_gap_without_deletion(archive):
    db, _, client, downloader, _ = archive
    remote_id = seed(archive, missing=(1,))
    class NotFound(Exception):
        status = 404
    client.failures[remote_id] = NotFound()
    result = run(archive)
    assert result["result"]["outcome"] == "succeeded" and downloader.calls == []
    with db.read() as session:
        assert session.get(Work, 1).source_metadata["remote_availability"]["state"] == "unavailable"


def test_complete_animation_maintenance_reuses_all_originals_and_derived_files(archive):
    db, root, _, downloader, _ = archive
    seed(archive, animation=True)
    run(archive)
    with db.read() as session:
        files = {asset.relative_path: (root / asset.relative_path).stat().st_mtime_ns for asset in session.get(Work, 1).assets}
        files.update({d.relative_path: (root / d.relative_path).stat().st_mtime_ns for d in session.get(Work, 1).derivatives})
    result = run(archive)
    assert result["result"]["outcome"] == "succeeded" and downloader.calls == []
    assert all((root / path).stat().st_mtime_ns == mtime for path, mtime in files.items())


def test_deletion_after_download_before_publication_never_restores_missing_page(archive):
    db, root, _, downloader, _ = archive
    seed(archive, missing=(0,))
    download = downloader.download
    def racing_download(url, destination, report=None):
        download(url, destination, report)
        with db.write() as session:
            intent = session.get(ArchiveIntent, "101")
            intent.mode, intent.revision = "suppressed", intent.revision + 1
    downloader.download = racing_download
    result = run(archive)
    assert result["result"]["outcome"] == "succeeded"
    assert not (root / "artist_10/101_p0.png").exists()
    assert (root / "101-1.png").read_bytes() == image_bytes()
    with db.read() as session:
        assert session.scalar(select(Asset).where(Asset.work_id == 1, Asset.page_index == 0)).relative_path is None


def test_planning_reads_catalogue_without_decoding_or_hashing_files(archive, monkeypatch):
    import src.core.archive as archive_module
    from src.core.tasks import TaskContext
    for work_id in range(1, 27):
        seed(archive, work_id=work_id)
    progress = []
    report = TaskContext.report
    def observe_report(context, phase, done, total, unit, detail):
        if phase == "inventory":
            progress.append((done, total, unit))
        return report(context, phase, done, total, unit, detail)
    monkeypatch.setattr(TaskContext, "report", observe_report)
    def unexpected_decode(*args, **kwargs):
        pytest.fail("catalogue planning must not decode local files on the check lane")
    monkeypatch.setattr(archive_module, "verify_media", unexpected_decode)
    *_, queue = archive
    operation_id = queue.submit("reconcile", {}, "manual")["operation_id"]
    round_id = queue._claim_round()
    assert queue._step("check", round_id)
    assert queue.details(operation_id)["state"] == "ready"
    assert progress[0] == (0, 26, "works") and progress[-1] == (26, 26, "works")
    assert any(0 < done < 26 for done, _, _ in progress)
    assert all(total == 26 and unit == "works" for _, total, unit in progress)
    assert [done for done, _, _ in progress] == sorted(done for done, _, _ in progress)
    assert archive[2].calls == [] and archive[3].calls == []


@pytest.mark.parametrize("race", [False, True])
def test_runtime_lightweight_listing_restores_unavailable_source_with_observation_guard(tmp_path, monkeypatch, race):
    import src.core.archive as archive_module
    from src.core.runtime import Runtime
    client, downloader = Client(), Downloader()
    runtime = Runtime(tmp_path / "library.db", tmp_path / "images", create=True, client=client, downloader=downloader)
    try:
        with runtime.database.write() as session:
            artist = Artist(id=1, pixiv_user_id="10", name="cached artist", profile_refreshed_at=utcnow())
            artist.policy = TrackingPolicy(mode="active")
            work = Work(id=1, pixiv_illust_id="101", artist=artist, title="cached title", type="manga", page_count=2,
                source_metadata={"remote_availability": {"state": "unavailable", "confirmed": True, "version": 1,
                    "reason": "old source unavailable", "account_id": "account-a"}})
            session.add_all([work, ArchiveIntent(pixiv_illust_id="101")])
            for page in range(2):
                relative = f"101-{page}.png"
                (runtime.images / relative).write_bytes(image_bytes())
                session.add(Asset(work_id=1, role="image", page_index=page, state="verified", relative_path=relative))
        def forbidden_detail(*args, **kwargs):
            pytest.fail("lightweight check must not refetch existing work detail")
        def direct_listing(remote_id):
            assert remote_id == "10"
            if race:
                version = runtime.source_status.begin("work", 1, "account-a")
                runtime.source_status.apply_failure("work", 1, Unavailable(), version, "account-a")
            yield {"illust_id": "101", "title": "list title", "type": "manga", "page_count": 2,
                "urls": [{"original": f"https://i.pximg.net/101_p{page}.png"} for page in range(2)]}
        client.get_all_artist_illusts = direct_listing
        client.get_illust_detail = forbidden_detail
        monkeypatch.setattr(archive_module, "verify_media", lambda *args, **kwargs: pytest.fail("check must not decode established originals"))
        operation_id = runtime.queue.submit("check", {}, "lightweight check")["operation_id"]
        assert runtime.queue.run_round()
        assert runtime.queue.details(operation_id)["result"]["outcome"] == "succeeded"
        assert downloader.calls == []
        assert runtime.source_status.snapshot("work", 1, runtime.library.account_id)["state"] == ("unavailable" if race else "available")
        with runtime.database.read() as session:
            assert session.get(Work, 1).title == ("cached title" if race else "list title")
    finally:
        runtime.close()
