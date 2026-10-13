"""Automatic discovery through real queue/handlers, with synthetic pages only."""
from datetime import timedelta
import io

import pytest
from PIL import Image
from sqlalchemy import select

from src.core.archive import ArchiveHandler
from src.core.bookmarks import BookmarkService
from src.core.database import ArchiveDatabase
from src.core.media import Publisher
from src.core.models import ApplicationState, ArchiveIntent, Artist, Operation, OperationItem, TrackingPolicy, Work, utcnow
from src.core.tasks import ItemSpec, TaskContext, TaskQueue


def payload(ident, posted=None, kind="illust"):
    return {"illust_id": str(ident), "title": "synthetic", "type": kind, "page_count": 1,
            "posted_at": (posted or utcnow()).isoformat(), "caption": "", "is_bookmarked": False,
            "urls": [{"original": f"https://synthetic.invalid/{ident}.png"}]}


class Remote:
    def __init__(self):
        self.pages, self.calls = {}, []

    def get_account_id(self):
        return "synthetic"

    def get_artist_illusts_page(self, artist, kind, offset):
        self.calls.append((kind, offset))
        value = self.pages.get((kind, offset), {"items": [], "next_offset": None})
        if isinstance(value, Exception):
            raise value
        return value

    def get_illust_detail(self, ident):
        raise AssertionError("Known works must not be refreshed")


class Downloader:
    def download(self, url, destination, report=None):
        assert url.startswith("https://synthetic.invalid/")
        content = io.BytesIO()
        Image.new("RGB", (16, 12), "blue").save(content, "PNG")
        destination.write_bytes(content.getvalue())


@pytest.fixture
def case(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    remote = Remote()
    bookmarks = BookmarkService(db, remote, current_account=lambda: "synthetic")
    publisher = Publisher(db, tmp_path / "images")
    handler = ArchiveHandler("check", db, publisher, remote, bookmarks=bookmarks)
    repair = ArchiveHandler("repair", db, publisher, remote, Downloader(), bookmarks=bookmarks)
    queue = TaskQueue(db, {"check": handler, "repair": repair})
    with db.write() as session:
        artist = Artist(id=1, pixiv_user_id="10", name="synthetic", last_checked_at=utcnow())
        artist.policy = TrackingPolicy(mode="active")
        session.add(artist)
    yield db, remote, handler, queue
    queue.stop()
    bookmarks.stop()
    db.close()


def submit(case, source="automatic"):
    return case[3].submit("check", {"artist_ids": [1]}, "synthetic", source)["operation_id"]


def drain_checks(case, limit=100):
    idle = 0
    for _ in range(limit):
        advanced = [case[3]._step(lane) for lane in ("check", "download", "local")]
        if not any(advanced):
            idle += 1
            if idle == 3:
                return
        else:
            idle = 0
    raise AssertionError("Synthetic check did not finish in bounded steps")


def test_automatic_check_does_not_reserve_or_refresh_history(case):
    db, remote, handler, queue = case
    old = utcnow() - timedelta(days=30)
    for ident in (101, 102, 103, 104):
        handler._save_remote_work(1, payload(ident, old))
    with db.write() as session:
        session.add(ApplicationState(key="incremental_check:1:illust", value={"mode": "incremental-v1",
            "checked_at": utcnow().isoformat(), "head_id": 104, "anchor_ids": ["104", "103"]}))
    remote.pages = {("illust", 0): {"items": [payload(104, old), payload(103, old)], "next_offset": 2},
                    ("illust", 2): {"items": [payload(102, old), payload(101, old)], "next_offset": 4}}
    ident = submit(case)
    drain_checks(case)
    with db.read() as session:
        items = list(session.scalars(select(OperationItem).where(OperationItem.operation_id == ident)))
        assert not any(item.kind in ("observation", "discover_work") for item in items)
        assert session.get(Operation, ident).result["incremental_check"]["new_works"] == 0
    assert ("illust", 4) not in remote.calls


def test_bootstrap_downtime_mixed_known_equal_times_and_both_lists(case):
    db, remote, handler, queue = case
    before = utcnow() - timedelta(days=40)
    old = before - timedelta(days=20)
    handler._save_remote_work(1, payload(100, before))
    handler._save_remote_work(1, payload(50, old))
    handler._save_remote_work(1, payload(60, old))
    with db.write() as session:
        session.get(Artist, 1).last_checked_at = before
    same_time = before + timedelta(days=1)
    remote.pages = {
        ("illust", 0): {"items": [payload(100, before), payload(101, same_time)], "next_offset": 2},
        ("illust", 2): {"items": [payload(102, same_time), payload(100, before)], "next_offset": 4},
        ("illust", 4): {"items": [payload(103), payload(50, old)], "next_offset": 6},
        ("illust", 6): {"items": [payload(60, old)], "next_offset": 7},
        ("illust", 7): {"items": [payload(50, old)], "next_offset": 8},
        ("manga", 0): {"items": [payload(201, same_time, "manga")], "next_offset": None},
    }
    ident = submit(case)
    drain_checks(case)
    with db.read() as session:
        assert set(session.scalars(select(Work.pixiv_illust_id))) == {"50", "60", "100", "101", "102", "103", "201"}
        op = session.get(Operation, ident)
        assert op.result["incremental_check"] == {"mode": "incremental-v1", "artists_done": 1,
            "artists_total": 1, "new_works": 4, "scan_complete": True}
        assert len(op.result["child_operation_ids"]) == 4
        assert session.get(ApplicationState, "incremental_check:1:illust")
        assert session.get(ApplicationState, "incremental_check:1:manga")
    assert ("illust", 8) not in remote.calls
    # The actual status projection used by /api/status keeps the new contract.
    view = next(op for op in queue.snapshot()["operations"] if op["id"] == ident)
    assert view["result"]["incremental_check"]["new_works"] == 4


def test_suppression_duplicate_page_and_registration_resume_count_once(case):
    db, remote, handler, queue = case
    with db.write() as session:
        session.add(ArchiveIntent(pixiv_illust_id="102", mode="suppressed"))
    remote.pages = {
        ("illust", 0): {"items": [payload(101), payload(102)], "next_offset": 2},
        ("illust", 2): {"items": [payload(101), payload(102)], "next_offset": 4},
        ("illust", 4): {"items": [payload(103)], "next_offset": None},
    }
    ident = submit(case)
    assert queue._step("check")  # planning
    assert queue._step("check")  # capture page
    # Commit registration, then simulate exit before the queue settles the item.
    with db.read() as session:
        work_item = session.scalar(select(OperationItem).where(OperationItem.operation_id == ident, OperationItem.kind == "discover_work"))
        spec = ItemSpec(work_item.item_key, work_item.kind, work_item.target, work_item.phase, work_item.unit)
    context = TaskContext(queue, ident, work_item.id, kind="check")
    assert handler.execute(spec, context).state == "succeeded"
    with db.write() as session:
        session.get(OperationItem, work_item.id).state = "running"
    queue.recover()
    drain_checks(case)
    with db.read() as session:
        assert set(session.scalars(select(Work.pixiv_illust_id))) == {"101", "103"}
        op = session.get(Operation, ident)
        assert op.result["incremental_check"]["new_works"] == 2
        assert op.result["incremental_check"]["scan_complete"] is True
        assert len(op.result["child_operation_ids"]) == 2


@pytest.mark.parametrize("failure", ["page", "registration"])
def test_failure_keeps_previous_frontier_and_saved_children(case, monkeypatch, failure):
    db, remote, handler, queue = case
    before = utcnow() - timedelta(days=10)
    boundary = {"mode": "incremental-v1", "checked_at": before.isoformat(), "head_id": 100, "anchor_ids": ["100"]}
    with db.write() as session:
        session.add(ApplicationState(key="incremental_check:1:illust", value=boundary))
    remote.pages = {("illust", 0): {"items": [payload(101)], "next_offset": 1},
                    ("illust", 1): {"items": [payload(102)], "next_offset": None}}
    if failure == "page":
        remote.pages[("illust", 1)] = ValueError("synthetic page failure")
    else:
        original = handler._save_work_observation
        def fail(*args, **kwargs):
            if args[1]["illust_id"] == "102":
                raise ValueError("synthetic registration failure")
            return original(*args, **kwargs)
        monkeypatch.setattr(handler, "_save_work_observation", fail)
    ident = submit(case)
    drain_checks(case)
    with db.read() as session:
        assert session.get(ApplicationState, "incremental_check:1:illust").value == boundary
        op = session.get(Operation, ident)
        assert op.result["incremental_check"]["artists_done"] == 1
        assert op.result["incremental_check"]["new_works"] == 1
        assert op.result["incremental_check"]["scan_complete"] is False
        assert len(op.result["child_operation_ids"]) == 1
        assert session.scalar(select(Work.id).where(Work.pixiv_illust_id == "101"))


def test_legacy_running_check_skips_old_pages_preserves_captured_work_and_children(case):
    db, remote, handler, queue = case
    handler._save_remote_work(1, payload(100))
    ident = submit(case)
    with db.write() as session:
        op = session.get(Operation, ident)
        op.state = "interrupted"
        op.parameters = {"artist_ids": [1], "started_artist_ids": [1]}
        op.result = {"planning_complete": True, "planning_cursor": {"artist_offset": 1}}
        queue.materialize_in_transaction(session, ident, [
            ItemSpec("old-page", "discover_page", {"artist_id": 1, "work_type": "illust", "offset": 900}, "checking", "pages"),
            ItemSpec("old-known", "discover_work", {"artist_id": 1, "data": payload(100)}, "checking", "works"),
            ItemSpec("old-captured", "discover_work", {"artist_id": 1, "data": payload(101)}, "checking", "works"),
        ])
    queue.recover()
    drain_checks(case)
    with db.read() as session:
        op = session.get(Operation, ident)
        assert op.result["incremental_check"]["new_works"] == 1
        assert len(op.result["child_operation_ids"]) == 1
        assert session.scalar(select(Work.id).where(Work.pixiv_illust_id == "101"))
    assert ("illust", 900) not in remote.calls


def test_manual_check_keeps_explicit_history_scope(case):
    db, remote, handler, queue = case
    handler._save_remote_work(1, payload(100))
    remote.pages = {("illust", 0): {"items": [payload(100)], "next_offset": 1},
                    ("illust", 1): {"items": [payload(101, utcnow() - timedelta(days=900))], "next_offset": None}}
    ident = submit(case, source="manual")
    drain_checks(case)
    with db.read() as session:
        assert "incremental_check" not in session.get(Operation, ident).result
        assert session.scalar(select(Work.id).where(Work.pixiv_illust_id == "101"))
        assert session.scalar(select(OperationItem.id).where(OperationItem.operation_id == ident, OperationItem.kind == "observation"))


def test_repeated_remote_pages_fail_without_advancing_frontier(case):
    db, remote, handler, queue = case
    remote.pages = {("illust", index): {"items": [payload(101)], "next_offset": index + 1} for index in range(3)}
    ident = submit(case)
    drain_checks(case)
    with db.read() as session:
        assert session.get(ApplicationState, "incremental_check:1:illust") is None
        assert session.get(Operation, ident).result["incremental_check"]["scan_complete"] is False
    assert ("illust", 3) not in remote.calls


def test_first_check_without_local_anchor_covers_all_new_pages_after_tracking(case):
    db, remote, handler, queue = case
    added = utcnow() - timedelta(days=45)
    with db.write() as session:
        artist = session.get(Artist, 1)
        artist.added_at, artist.last_checked_at = added, None
    remote.pages = {
        ("illust", 0): {"items": [payload(103)], "next_offset": 1},
        ("illust", 1): {"items": [payload(102, added + timedelta(days=10))], "next_offset": 2},
        ("illust", 2): {"items": [payload(101, added + timedelta(days=1))], "next_offset": None},
    }
    ident = submit(case)
    drain_checks(case)
    with db.read() as session:
        assert session.get(Operation, ident).result["incremental_check"]["new_works"] == 3
        assert set(session.scalars(select(Work.pixiv_illust_id))) == {"101", "102", "103"}


def test_page_join_overlap_catches_deleted_row_shift_without_duplicate_downloads(case):
    db, remote, handler, queue = case
    # After the initial four rows, an earlier row is deleted remotely. Offset4
    # would skip 104; overlapping at offset3 sees it, plus one repeated ID.
    remote.pages = {
        ("illust", 0): {"items": [payload(108), payload(107), payload(106), payload(105)], "next_offset": 4},
        ("illust", 3): {"items": [payload(105), payload(104), payload(103), payload(102)], "next_offset": 7},
        ("illust", 6): {"items": [payload(102), payload(101)], "next_offset": None},
    }
    ident = submit(case)
    drain_checks(case)
    with db.read() as session:
        op = session.get(Operation, ident)
        assert op.result["incremental_check"]["new_works"] == 8
        assert len(op.result["child_operation_ids"]) == 8
        assert session.scalar(select(Work.id).where(Work.pixiv_illust_id == "104"))
    assert ("illust", 4) not in remote.calls


def test_failed_bootstrap_does_not_rebase_next_operation_on_partial_new_records(case):
    db, remote, handler, queue = case
    added = utcnow() - timedelta(days=45)
    with db.write() as session:
        artist = session.get(Artist, 1)
        artist.added_at, artist.last_checked_at = added, None
    remote.pages = {("illust", 0): {"items": [payload(103)], "next_offset": 1},
                    ("illust", 1): ValueError("synthetic failure")}
    first = submit(case)
    drain_checks(case)
    with db.read() as session:
        assert session.get(Operation, first).state == "finished"
        assert session.get(ApplicationState, "incremental_check:1:illust") is None
    # The next run must still find posts many weeks before partial result103.
    remote.pages[("illust", 1)] = {"items": [payload(102, added + timedelta(days=10))], "next_offset": 2}
    remote.pages[("illust", 2)] = {"items": [payload(101, added + timedelta(days=1))], "next_offset": None}
    second = submit(case)
    drain_checks(case)
    with db.read() as session:
        assert set(session.scalars(select(Work.pixiv_illust_id))) == {"101", "102", "103"}
        assert session.get(Operation, second).result["incremental_check"]["new_works"] == 2
        assert session.get(Operation, second).result["incremental_check"]["scan_complete"] is True
