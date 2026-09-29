"""Synthetic remote availability and local archive recovery."""
from datetime import datetime

from sqlalchemy import select

from src.core.archive import ArchiveHandler
from src.core.activity_log import processing_log
from src.core.library import Library
from src.core.models import ApplicationState, Asset, Derivative, Operation, OperationItem, Work
from src.core.maintenance import LEDGER_PREFIX, MissingMediaMaintenance
from src.core.tasks import TaskContext
from src.client import PixivClient
from test_missing_media_maintenance import archive, png


class Unavailable(Exception):
    user_message = "主站暂时无法获取此作品，已有存档保留"
    failure_code = "work_unavailable"
    retryable = False
    status = 404


class MissingUgoiraMetadata(Exception):
    user_message = "动图时序资料暂时无法获取，已有存档保留"
    failure_code = "ugoira_metadata_unavailable"
    retryable = False
    status = 404


class RestoredClient:
    def __init__(self, count=2):
        self.count = count

    def get_illust_detail(self, remote_id):
        return {"illust_id": str(remote_id), "title": "restored", "type": "manga",
                "page_count": self.count, "urls": [{"original": f"https://i.pximg.net/{i}.png"}
                                           for i in range(self.count)]}


def _listed_work(remote_id, pages=1):
    return {"illust_id": str(remote_id), "title": f"work {remote_id}", "type": "manga",
            "page_count": pages, "urls": [{"original": f"https://i.pximg.net/{remote_id}_p{i}.png"}
                                          for i in range(pages)]}


def test_check_continues_after_one_work_conflicts_across_artist_pages(tmp_path):
    db, root, queue, downloader, _, _ = archive(tmp_path)
    try:
        with db.write() as session:
            old = session.get(Work, 1)
            old.type, old.page_count = "manga", 6
            for page in range(6):
                relative = f"old_p{page}.png"
                (root / relative).write_bytes(png())
                asset = old.assets[0] if page == 0 else Asset(work_id=old.id, role="image", page_index=page)
                asset.state, asset.relative_path = "verified", relative
                if page:
                    session.add(asset)

        class PagedClient:
            get_all_artist_illusts = PixivClient.get_all_artist_illusts

            def __init__(self):
                self.pages = []

            def _get(self, _, params):
                self.pages.append((params["type"], params.get("offset")))
                if params["type"] == "manga":
                    return {"illusts": [], "next_url": None}
                if params.get("offset") == "1":
                    return {"illusts": [_listed_work(202)], "next_url": None}
                return {"illusts": [_listed_work(101, 2), _listed_work(201)],
                        "next_url": "https://app-api.pixiv.net/v1/user/illusts?user_id=1&type=illust&offset=1"}

            def _parse_illust(self, data):
                return data

        client = PagedClient()
        repair = queue.handlers["repair"]
        queue.handlers["check"] = ArchiveHandler("check", db, repair.publisher, client, downloader)
        submitted = queue.submit("check", {"artist_ids": [1]}, "检查新作品")
        assert queue.run_round()

        with db.read() as session:
            old = session.get(Work, 1)
            assert old.page_count == 6 and len(old.assets) == 6
            for remote_id in ("201", "202"):
                work = session.scalar(select(Work).where(Work.pixiv_illust_id == remote_id))
                assert work is not None
                assert work.assets[0].state == "verified"
                assert (root / work.assets[0].relative_path).read_bytes() == png()
        assert client.pages == [("illust", None), ("illust", "1"), ("manga", None)]
        assert downloader.calls == ["https://i.pximg.net/201_p0.png", "https://i.pximg.net/202_p0.png"]
        details = queue.details(submitted["operation_id"])
        assert details["result"]["outcome"] == "partial_failure"
        assert details["result"]["inventory_complete"] is False
        assert any(item["result"].get("failure_code") == "work_metadata_conflict"
                   for item in details["items"] if item["state"] == "failed")
        assert any("作品 101：主站当前 2 页，本地记录 6 页" in line
                   for line in processing_log(db)["lines"])
        with db.write() as session:
            session.add_all([
                OperationItem(operation_id=submitted["operation_id"], item_key="unsafe-id",
                              position=999, kind="error", target={}, phase="checking", state="failed",
                              result={"failure_code": "work_metadata_conflict", "pixiv_id": "101\nSECRET",
                                      "message": "主站当前 2 页，本地记录 6 页；已有存档保留，需核对"}),
                OperationItem(operation_id=submitted["operation_id"], item_key="unsafe-message",
                              position=1000, kind="error", target={}, phase="checking", state="failed",
                              result={"failure_code": "work_metadata_conflict", "pixiv_id": "101",
                                      "message": "SECRET remote response"}),
            ])
        assert "SECRET" not in "\n".join(processing_log(db)["lines"])
    finally:
        db.close()


def test_check_still_fails_when_artist_pagination_breaks(tmp_path):
    db, root, queue, downloader, _, _ = archive(tmp_path)
    try:
        class BrokenPage(Exception):
            user_message = "画师分页请求失败"

        class BrokenClient:
            def get_all_artist_illusts(self, _):
                yield _listed_work(201)
                raise BrokenPage()

        repair = queue.handlers["repair"]
        queue.handlers["check"] = ArchiveHandler("check", db, repair.publisher, BrokenClient(), downloader)
        submitted = queue.submit("check", {"artist_ids": [1]}, "检查新作品")
        assert queue.run_round()
        with db.read() as session:
            assert session.scalar(select(Work).where(Work.pixiv_illust_id == "201")) is not None
            assert session.get(Work, 1).artist.last_checked_at is None
        details = queue.details(submitted["operation_id"])
        assert details["result"]["outcome"] == "partial_failure"
        assert details["result"]["inventory_complete"] is False
        assert any(item["key"] == "artist-error-1" and item["state"] == "failed"
                   for item in details["items"])
    finally:
        db.close()


def test_404_preserves_page_and_builds_local_thumbnail_with_attention(tmp_path):
    db, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        (root / "first.png").write_bytes(png())
        with db.write() as session:
            work = session.get(Work, 1)
            work.type, work.page_count = "manga", 2
            first = session.scalar(select(Asset).where(Asset.work_id == 1))
            first.state, first.relative_path = "verified", "first.png"
            session.add(Asset(work_id=1, role="image", page_index=1, state="missing"))

        class MissingClient:
            def get_illust_detail(self, remote_id):
                raise Unavailable()

        queue.handlers["repair"].client = MissingClient()
        maintenance.tick(force=True)
        queue.run_round()
        maintenance.tick(force=True)
        queue.run_round()
        maintenance.tick(force=True)
        assert maintenance.tick(force=True) == []
        with db.read() as session:
            work = session.get(Work, 1)
            assert work.page_count == 2
            assert work.source_metadata["remote_availability"]["state"] == "unavailable"
            assert session.scalar(select(Asset).where(Asset.page_index == 0)).state == "verified"
            assert session.scalar(select(Derivative).where(Derivative.kind == "thumbnail")).state == "verified"
            ledger = session.get(ApplicationState, LEDGER_PREFIX + "1").value
            assert ledger["tracks"]["remote"]["status"] == "attention"
            assert ledger["tracks"]["remote"]["next_retry_at"] is None
        detail = Library(db).detail(1)
        assert detail["pages"][0]["src"] and detail["pages"][1]["src"] is None
        assert detail["recovery"]["status"] == "attention"
        assert detail["availability"]["state"] == "unavailable"
        assert downloader.calls == []
    finally:
        db.close()


def test_metadata_conflict_stops_retry_preserves_six_page_archive(tmp_path):
    db, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        with db.write() as session:
            work = session.get(Work, 1)
            work.type, work.page_count = "manga", 6
            for page in range(1, 6):
                session.add(Asset(work_id=1, role="image", page_index=page, state="missing"))
        queue.handlers["repair"].client = RestoredClient(2)
        maintenance.tick(force=True)
        queue.run_round()
        maintenance.tick(force=True)
        with db.read() as session:
            work = session.get(Work, 1)
            assert work.page_count == 6 and len(work.assets) == 6
            assert work.source_metadata["remote_availability"]["state"] == "available"
            ledger = session.get(ApplicationState, LEDGER_PREFIX + "1").value
            assert ledger["status"] == "attention"
            assert ledger["failure_code"] == "work_metadata_conflict"
            assert "6" in ledger["reason"] and "2" in ledger["reason"]
        assert maintenance.tick(force=True) == []
    finally:
        db.close()


def test_complete_local_archive_does_not_request_remote_or_fail(tmp_path):
    db, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        (root / "first.png").write_bytes(png())
        with db.write() as session:
            first = session.scalar(select(Asset))
            first.state, first.relative_path = "verified", "first.png"

        class NoRemote:
            def get_illust_detail(self, remote_id):
                raise AssertionError("complete local work requested remote detail")

        queue.handlers["repair"].client = NoRemote()
        submitted = queue.submit("repair", {"work_ids": [1]}, "synthetic")
        queue.run_round()
        assert queue.details(submitted["operation_id"])["result"]["outcome"] == "succeeded"
        with db.read() as session:
            assert session.scalar(select(Asset)).state == "verified"
    finally:
        db.close()


def test_old_waiting_unavailable_is_attention_before_resubmission(tmp_path):
    db, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        with db.write() as session:
            session.add(ApplicationState(key=LEDGER_PREFIX + "1", value={
                "status": "waiting", "kind": "repair", "failure_code": "work_unavailable",
                "next_retry_at": "2026-09-28T00:00:00", "reason": "主站暂时无法获取"}))
        assert maintenance.tick(force=True) == []
        with db.read() as session:
            ledger = session.get(ApplicationState, LEDGER_PREFIX + "1").value
            assert ledger["status"] == "attention" and ledger["next_retry_at"] is None
            assert ledger["tracks"]["remote"]["status"] == "attention"
        restarted = MissingMediaMaintenance(db, queue, remote_available=lambda: True,
            now=lambda: clock[0], monotonic=lambda: 0.0)
        assert restarted.tick(force=True) == []
    finally:
        db.close()


def test_legacy_404_and_conflict_ledgers_stop_without_a_new_request(tmp_path):
    db, root, queue, downloader, maintenance, clock = archive(tmp_path, count=2)
    try:
        with db.write() as session:
            session.add_all([
                ApplicationState(key=LEDGER_PREFIX + "1", value={"status": "waiting", "kind": "repair",
                    "failure_code": "api_error", "http_status": 404,
                    "next_retry_at": "2026-09-28T00:00:00", "reason": "尚无此页"}),
                ApplicationState(key=LEDGER_PREFIX + "2", value={"status": "waiting", "kind": "repair",
                    "next_retry_at": "2026-09-28T00:00:00",
                    "reason": "远端作品的归属、类型或页数发生变化，已有存档保留待核对"})])
        assert maintenance.tick(force=True) == []
        with db.read() as session:
            assert session.get(ApplicationState, LEDGER_PREFIX + "1").value["failure_code"] == "work_unavailable"
            assert session.get(ApplicationState, LEDGER_PREFIX + "2").value["failure_code"] == "work_metadata_conflict"
        assert downloader.calls == []
    finally:
        db.close()


def test_ugoira_subresource_404_does_not_label_work_unavailable(tmp_path):
    db, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        with db.write() as session:
            work = session.get(Work, 1)
            work.type, work.page_count = "ugoira", 1
            session.scalar(select(Asset)).role = "ugoira_zip"
            session.add(Asset(work_id=1, role="ugoira_timing", page_index=0, state="missing"))

        class MissingClient:
            def get_ugoira_metadata(self, remote_id):
                raise MissingUgoiraMetadata()

        queue.handlers["repair"].client = MissingClient()
        maintenance.tick(force=True)
        queue.run_round()
        maintenance.tick(force=True)
        with db.read() as session:
            assert session.get(Work, 1).source_metadata.get("remote_availability") is None
            ledger = session.get(ApplicationState, LEDGER_PREFIX + "1").value
            assert ledger["failure_code"] == "ugoira_metadata_unavailable"
            assert ledger["status"] == "attention"
    finally:
        db.close()


def test_busy_scan_keeps_recent_cursor_and_local_and_remote_windows_independent(tmp_path):
    db, root, queue, downloader, maintenance, clock = archive(tmp_path, count=140)
    try:
        with db.write() as session:
            for work_id in range(130, 141):
                asset = session.scalar(select(Asset).where(Asset.work_id == work_id))
                asset.state, asset.relative_path = "verified", f"{work_id}.png"
                (root / asset.relative_path).write_bytes(png())
        queue.submit("repair", {"work_ids": [1]}, "manual")
        assert maintenance.tick(force=True) == []
        assert all(value == 0 for value in maintenance._cursors.values())
        queue.run_round()
        submitted = maintenance.tick(force=True)
        assert len(submitted) == 9
        with db.read() as session:
            operations = [session.get(Operation, item["operation_id"]) for item in submitted]
            assert operations[0].parameters == {"work_ids": [129]}
            assert [operation.parameters["work_ids"][0] for operation in operations[1:]] == list(range(140, 132, -1))
        assert maintenance._cursors["image"] == 133
        assert maintenance._cursors["remote"] == 129
    finally:
        db.close()


def test_manual_local_verification_clears_unavailable_attention_but_not_conflict(tmp_path):
    db, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        (root / "restored.png").write_bytes(png())
        with db.write() as session:
            asset = session.scalar(select(Asset))
            asset.relative_path = "restored.png"
            work = session.get(Work, 1)
            work.source_metadata = {"remote_availability": {"state": "unavailable",
                "checked_at": "2026-09-29T00:00:00+00:00", "reason": "主站暂时无法获取"}}
            session.add(ApplicationState(key=LEDGER_PREFIX + "1", value={"status": "attention",
                "reason": "主站暂时无法获取", "failure_code": "work_unavailable",
                "tracks": {"remote": {"status": "attention", "kind": "repair",
                    "reason": "主站暂时无法获取", "failure_code": "work_unavailable"}}}))
        queue.handlers["verify"] = ArchiveHandler("verify", db,
            queue.handlers["repair"].publisher, object(), downloader)
        queue.submit("verify", {"work_ids": [1]}, "synthetic")
        queue.run_round()
        with db.read() as session:
            assert session.scalar(select(Asset)).state == "verified"
            assert session.get(ApplicationState, LEDGER_PREFIX + "1").value["status"] == "ready"
            assert session.get(Work, 1).source_metadata["remote_availability"]["state"] == "unavailable"

        with db.write() as session:
            setting = session.get(ApplicationState, LEDGER_PREFIX + "1")
            setting.value = {"status": "attention", "reason": "资料冲突",
                "failure_code": "work_metadata_conflict", "tracks": {"remote": {
                    "status": "attention", "kind": "repair", "reason": "资料冲突",
                    "failure_code": "work_metadata_conflict"}}}
        queue.submit("verify", {"work_ids": [1]}, "synthetic")
        queue.run_round()
        with db.read() as session:
            assert session.get(ApplicationState, LEDGER_PREFIX + "1").value["status"] == "attention"
    finally:
        db.close()


def test_normal_check_reports_blocked_gap_without_repeating_remote_404(tmp_path):
    db, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        with db.write() as session:
            session.scalar(select(Asset)).source_url = None
            session.add(ApplicationState(key=LEDGER_PREFIX + "1", value={"status": "attention",
                "reason": "主站暂时无法获取此作品，尚不能确认是否删除；已有存档保留",
                "failure_code": "work_unavailable", "tracks": {"remote": {"status": "attention",
                    "kind": "repair", "failure_code": "work_unavailable",
                    "reason": "主站暂时无法获取此作品，尚不能确认是否删除；已有存档保留"}}}))
        operation = queue.submit("repair", {"work_ids": [1]}, "synthetic")
        checker = ArchiveHandler("check", db, queue.handlers["repair"].publisher, object(), downloader)
        specs = checker._plan_work(1, {}, TaskContext(queue, operation["operation_id"]))
        assert [spec.kind for spec in specs] == ["error"]
        assert specs[0].target["failure_code"] == "work_unavailable"
    finally:
        db.close()


def test_remote_detail_recovery_resumes_manual_gap_without_redownloading_saved_page(tmp_path):
    db, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        (root / "first.png").write_bytes(png())
        with db.write() as session:
            work = session.get(Work, 1)
            work.type, work.page_count = "manga", 2
            first = session.scalar(select(Asset))
            first.state, first.relative_path = "verified", "first.png"
            session.add(Asset(work_id=1, role="image", page_index=1, state="missing"))
            session.add(ApplicationState(key=LEDGER_PREFIX + "1", value={"status": "attention",
                "reason": "主站暂时无法获取", "failure_code": "work_unavailable",
                "tracks": {"remote": {"status": "attention", "kind": "repair",
                    "reason": "主站暂时无法获取", "failure_code": "work_unavailable"}}}))

        class Restored:
            def get_illust_detail(self, remote_id):
                return {"illust_id": str(remote_id), "title": "restored", "type": "manga",
                    "page_count": 2, "urls": [
                        {"original": "https://i.pximg.net/1.png"},
                        {"original": "https://i.pximg.net/2.png"}]}

        queue.handlers["repair"].client = Restored()
        manual = queue.submit("repair", {"work_ids": [1]}, "synthetic")
        queue.run_round()
        assert queue.details(manual["operation_id"])["result"]["outcome"] == "succeeded"
        assert downloader.calls == ["https://i.pximg.net/2.png"]
        with db.read() as session:
            assets = list(session.scalars(select(Asset).where(Asset.work_id == 1).order_by(Asset.page_index)))
            assert [asset.state for asset in assets] == ["verified", "verified"]
            work = session.get(Work, 1)
            assert work.source_metadata["remote_availability"]["state"] == "available"
            assert session.get(ApplicationState, LEDGER_PREFIX + "1").value["status"] == "ready"
    finally:
        db.close()


def test_work_detail_success_does_not_release_ugoira_subresource_attention(tmp_path):
    db, root, queue, downloader, maintenance, clock = archive(tmp_path)
    try:
        with db.write() as session:
            work = session.get(Work, 1)
            work.type = "ugoira"
            session.scalar(select(Asset)).role = "ugoira_zip"
            session.add(Asset(work_id=1, role="ugoira_timing", page_index=0, state="missing"))
            session.add(ApplicationState(key=LEDGER_PREFIX + "1", value={"status": "attention",
                "reason": "动图时序资料暂时无法获取", "failure_code": "ugoira_metadata_unavailable",
                "tracks": {"remote": {"status": "attention", "kind": "repair",
                    "reason": "动图时序资料暂时无法获取",
                    "failure_code": "ugoira_metadata_unavailable"}}}))

        class WorkDetail:
            def get_illust_detail(self, remote_id):
                return {"illust_id": str(remote_id), "title": "ugoira", "type": "ugoira",
                    "page_count": 1, "urls": []}

        queue.handlers["repair"].client = WorkDetail()
        queue.handlers["repair"]._refresh_work(1, "101", 1)
        with db.read() as session:
            assert session.get(Work, 1).source_metadata["remote_availability"]["state"] == "available"
            assert session.get(ApplicationState, LEDGER_PREFIX + "1").value["status"] == "attention"
        restarted = MissingMediaMaintenance(db, queue, remote_available=lambda: True,
            now=lambda: clock[0], monotonic=lambda: 0.0)
        assert restarted.tick(force=True) == []
    finally:
        db.close()
