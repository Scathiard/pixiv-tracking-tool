"""Synthetic background repair, with no Pixiv account or real library."""
from datetime import datetime, timedelta
from io import BytesIO

from PIL import Image
from sqlalchemy import select
import pytest

from src.core.archive import ArchiveHandler
from src.core.database import ArchiveDatabase
from src.core.maintenance import LEDGER_PREFIX, MissingMediaMaintenance
from src.core.media import Publisher
from src.core.models import ApplicationState, ArchiveIntent, Artist, Asset, Derivative, Operation, Work
from src.core.tasks import TaskQueue


def png():
    output = BytesIO()
    Image.new("RGB", (16, 12), "blue").save(output, format="PNG")
    return output.getvalue()


class Rejected(Exception):
    user_message = "图片请求被拒绝（HTTP 403）"
    status = 403
    retryable = False
    failure_code = "image_forbidden"


class ApiRateLimit(Exception):
    user_message = "主站请求过快，稍后重试"
    status = 200
    retryable = True
    retry_after = 120
    failure_code = "api_rate_limit"


class ImageRateLimit(Exception):
    user_message = "图片请求过快，稍后重试"
    status = None
    retryable = True
    retry_after = 90
    failure_code = "image_rate_limit"


class Download:
    def __init__(self):
        self.calls = []
        self.fail = False

    def download(self, url, destination, report=None):
        self.calls.append(url)
        if self.fail:
            raise self.fail() if isinstance(self.fail, type) else Rejected()
        destination.write_bytes(png())


def archive(tmp_path, *, count=1, offline=False):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    root = tmp_path / "images"
    root.mkdir()
    downloader = Download()
    publisher = Publisher(database, root)
    queue = TaskQueue(database, {kind: ArchiveHandler(kind, database, publisher, object(), downloader)
                                 for kind in ("repair", "thumbnails")})
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        for work_id in range(1, count + 1):
            work = Work(id=work_id, pixiv_illust_id=str(100 + work_id), artist_id=1,
                        title=str(work_id), type="illust")
            work.assets = [Asset(role="image", page_index=0, state="missing",
                                 source_url=f"https://i.pximg.net/{work_id}.png")]
            session.add_all([work, ArchiveIntent(pixiv_illust_id=str(100 + work_id))])
    clock = [datetime(2026, 9, 29, 0, 0, 0)]
    maintenance = MissingMediaMaintenance(database, queue, remote_available=lambda: not offline,
                                          now=lambda: clock[0], monotonic=lambda: 0.0)
    return database, root, queue, downloader, maintenance, clock


def test_failed_original_survives_thumbnail_inventory_then_recovers(tmp_path):
    database, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        downloader.fail = True
        failed = queue.submit("repair", {"work_ids": [1]}, "synthetic")
        queue.run_round()
        with database.read() as session:
            asset = session.scalar(select(Asset))
            assert asset.state == "failed" and "403" in asset.error
        queued = queue.submit("thumbnails", {"work_ids": [1]}, "synthetic")
        queue.run_round()
        assert queue.details(queued["operation_id"])["result"]["outcome"] == "empty"
        with database.read() as session:
            asset = session.scalar(select(Asset))
            assert asset.state == "failed" and "403" in asset.error

        downloader.fail = False
        assert len(maintenance.tick(force=True)) == 1
        queue.run_round()
        maintenance.tick(force=True)
        with database.read() as session:
            assert session.scalar(select(Asset)).state == "verified"
            assert session.scalar(select(Derivative)).state == "verified"
            assert session.get(ApplicationState, LEDGER_PREFIX + "1").value["status"] == "ready"
        assert len(downloader.calls) == 2
        assert maintenance.tick(force=True) == []
    finally:
        database.close()


def test_maintenance_is_bounded_deduped_and_restart_respects_rejection(tmp_path):
    database, root, queue, downloader, maintenance, clock = archive(tmp_path, count=12)
    try:
        downloader.fail = True
        first = maintenance.tick(force=True)
        assert len(first) == 1
        assert maintenance.tick() == []
        queue.run_round()
        maintenance.tick(force=True)
        with database.read() as session:
            ledger = session.get(ApplicationState, LEDGER_PREFIX + "12").value
            assert ledger["status"] == "waiting"
            assert ledger["http_status"] == 403
            assert ledger["next_retry_at"] > clock[0].isoformat()
        restarted = MissingMediaMaintenance(database, queue, remote_available=lambda: True,
                                            now=lambda: clock[0], monotonic=lambda: 0.0)
        assert restarted.tick(force=True) == []
        assert len(downloader.calls) == 1
        clock[0] += timedelta(days=2)
        downloader.fail = False
        assert len(restarted.tick(force=True)) == 1
        queue.run_round()
        restarted.tick(force=True)
        assert len(downloader.calls) == 2
    finally:
        database.close()


def test_offline_local_thumbnail_and_suppressed_or_legacy_gif_are_excluded(tmp_path):
    database, root, queue, downloader, maintenance, clock = archive(tmp_path, count=2, offline=True)
    try:
        (root / "local.png").write_bytes(png())
        with database.write() as session:
            asset = session.scalar(select(Asset).where(Asset.work_id == 1))
            asset.state, asset.relative_path = "verified", "local.png"
            session.get(ArchiveIntent, "102").mode = "suppressed"
            legacy = Work(id=3, pixiv_illust_id="103", artist_id=1, title="legacy", type="ugoira",
                          source_metadata={"original_backfill_requires_selection": True})
            legacy.assets = [Asset(role="ugoira_zip", state="missing"), Asset(role="ugoira_timing", state="missing")]
            session.add_all([legacy, ArchiveIntent(pixiv_illust_id="103")])
        assert len(maintenance.tick(force=True)) == 1
        queue.run_round()
        with database.read() as session:
            assert session.scalar(select(Derivative).where(Derivative.work_id == 1)).state == "verified"
            assert session.scalar(select(Operation).where(Operation.source == "maintenance")).parameters == {"work_ids": [1]}
        assert downloader.calls == []
    finally:
        database.close()


def test_local_batch_has_eight_work_limit(tmp_path):
    database, root, queue, downloader, maintenance, clock = archive(tmp_path, count=12, offline=True)
    try:
        with database.write() as session:
            for asset in session.scalars(select(Asset)):
                asset.state = "verified"
                asset.relative_path = f"{asset.work_id}.png"
                (root / asset.relative_path).write_bytes(png())
        first = maintenance.tick(force=True)
        assert len(first) == 8
        with database.read() as session:
            assert len(list(session.scalars(select(Operation).where(Operation.source == "maintenance")))) == 8
        queue.run_round()
        maintenance.tick(force=True)
        with database.read() as session:
            assert len(list(session.scalars(select(Derivative).where(Derivative.state == "verified")))) == 8
        assert downloader.calls == []
    finally:
        database.close()


@pytest.mark.parametrize("failure, expected_status", [(ApiRateLimit, 200), (ImageRateLimit, None)])
def test_rate_limit_sets_remote_hold(tmp_path, failure, expected_status):
    database, root, queue, downloader, maintenance, clock = archive(tmp_path, count=3)
    try:
        downloader.fail = failure
        maintenance.tick(force=True)
        queue.run_round()
        assert maintenance.tick(force=True) == []
        with database.read() as session:
            ledger = session.get(ApplicationState, LEDGER_PREFIX + "3").value
            assert ledger["http_status"] == expected_status
            assert ledger["failure_code"] == failure.failure_code
            assert ledger["next_retry_at"] >= (clock[0] + timedelta(minutes=15)).isoformat()
            hold = session.get(ApplicationState, "maintenance_remote_hold").value
            assert hold["until"] == ledger["next_retry_at"]
        assert len(downloader.calls) == 1
    finally:
        database.close()


def test_remote_denial_does_not_delay_local_thumbnail(tmp_path):
    database, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        (root / "first.png").write_bytes(png())
        with database.write() as session:
            first = session.scalar(select(Asset))
            first.state, first.relative_path = "verified", "first.png"
            session.add(Asset(work_id=1, role="image", page_index=1, state="missing"))
        class DeniedClient:
            def get_illust_detail(self, remote_id):
                raise Rejected()
        queue.handlers["repair"].client = DeniedClient()
        assert len(maintenance.tick(force=True)) == 1
        queue.run_round()
        local = maintenance.tick(force=True)
        assert local == []
        with database.read() as session:
            assert session.scalar(select(Derivative)).state == "verified"
            ledger = session.get(ApplicationState, LEDGER_PREFIX + "1").value
            assert ledger["tracks"]["remote"]["status"] == "waiting"
            assert ledger["status"] == "waiting"
    finally:
        database.close()


def test_paused_automatic_check_does_not_block_maintenance(tmp_path):
    database, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        queue.handlers["check"] = queue.handlers["repair"]
        queue.set_automatic_paused(True)
        queue.submit("check", {}, "paused check", source="automatic")
        assert len(maintenance.tick(force=True)) == 1
        queue.run_round()
        assert len(downloader.calls) == 1
        with database.read() as session:
            check = session.scalar(select(Operation).where(Operation.kind == "check"))
            assert check.state == "queued"
        queue.accept_submissions(False)
        assert maintenance.tick(force=True) == []
    finally:
        database.close()


def test_known_invalid_is_eligible_but_unknown_path_is_not(tmp_path):
    database, root, queue, downloader, maintenance, clock = archive(tmp_path, count=2)
    try:
        with database.write() as session:
            session.scalar(select(Asset).where(Asset.work_id == 1)).state = "unknown"
            session.scalar(select(Asset).where(Asset.work_id == 2)).state = "invalid"
        operations = maintenance.tick(force=True)
        assert len(operations) == 1
        with database.read() as session:
            assert session.get(Operation, operations[0]["operation_id"]).parameters == {"work_ids": [2]}
        queue.run_round()
        assert downloader.calls == ["https://i.pximg.net/2.png"]
    finally:
        database.close()


def test_maintenance_does_not_reverify_verified_sibling_pages(tmp_path):
    database, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        with database.write() as session:
            first = session.scalar(select(Asset))
            first.state, first.relative_path = "verified", "first.png"
            session.add(Asset(work_id=1, role="image", page_index=1, state="missing",
                              source_url="https://i.pximg.net/second.png"))
        (root / "first.png").write_bytes(png())
        inspected = []
        handler = queue.handlers["repair"]
        original = handler._inspect_asset

        def trace(asset, force=False):
            inspected.append(asset.page_index)
            return original(asset, force=force)

        handler._inspect_asset = trace
        maintenance.tick(force=True)
        queue.run_round()
        assert 0 not in inspected
        assert inspected == [1, 1]
    finally:
        database.close()


def test_unknown_first_page_with_local_file_makes_thumbnail_only(tmp_path):
    database, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        with database.write() as session:
            first = session.scalar(select(Asset))
            first.state, first.relative_path = "unknown", "first.png"
            session.add(Asset(work_id=1, role="image", page_index=1, state="unknown",
                              relative_path="other.png", source_url="https://i.pximg.net/other.png"))
        (root / "first.png").write_bytes(png())
        inspected = []
        handler = queue.handlers["thumbnails"]
        original = handler._inspect_asset

        def trace(asset, force=False):
            inspected.append(asset.page_index)
            return original(asset, force=force)

        handler._inspect_asset = trace
        submitted = maintenance.tick(force=True)
        assert len(submitted) == 1
        with database.read() as session:
            assert session.get(Operation, submitted[0]["operation_id"]).kind == "thumbnails"
        queue.run_round()
        assert inspected == [0]
        assert downloader.calls == []
        with database.read() as session:
            assert session.scalar(select(Asset).where(Asset.page_index == 0)).state == "verified"
            assert session.scalar(select(Asset).where(Asset.page_index == 1)).state == "unknown"
            assert session.scalar(select(Derivative)).state == "verified"
    finally:
        database.close()


def test_remote_cursor_reaches_due_work_after_128_waiting_with_high_local_id(tmp_path):
    database, root, queue, downloader, maintenance, clock = archive(tmp_path, count=131)
    try:
        future = (clock[0] + timedelta(days=1)).isoformat()
        with database.write() as session:
            local = session.scalar(select(Asset).where(Asset.work_id == 131))
            local.state, local.relative_path = "verified", "local.png"
            for work_id in range(1, 129):
                session.add(ApplicationState(key=LEDGER_PREFIX + str(work_id), value={
                    "status": "waiting", "tracks": {"remote": {
                        "status": "waiting", "kind": "repair", "next_retry_at": future}}}))
            session.add(ApplicationState(key=LEDGER_PREFIX + "131", value={
                "status": "waiting", "tracks": {"local": {
                    "status": "waiting", "kind": "thumbnails", "next_retry_at": future}}}))
        (root / "local.png").write_bytes(png())
        first = maintenance.tick(force=True)
        assert len(first) == 1
        with database.read() as session:
            assert session.get(Operation, first[0]["operation_id"]).parameters == {"work_ids": [130]}
        queue.run_round()
        later = maintenance.tick(force=True)
        assert later == []  # remote cadence is independent of forced inventory scans
        clock[0] += timedelta(minutes=2)
        maintenance._next_remote = -1
        later = maintenance.tick(force=True)
        assert len(later) == 1
        with database.read() as session:
            assert session.get(Operation, later[0]["operation_id"]).parameters == {"work_ids": [129]}
    finally:
        database.close()
