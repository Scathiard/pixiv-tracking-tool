"""The same original-completeness truth in every bounded Library view."""
import pytest
from sqlalchemy import event

from src.core.database import ArchiveDatabase
from src.core.library import Library
from src.core.models import ApplicationState, Artist, Asset, Derivative, Operation, OperationItem, Work
from src.core.runtime import Runtime


@pytest.fixture
def catalogue(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "archive.db")
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
    yield database, Library(database)
    database.close()


def add_work(database, *, pages=10, states=("verified",), kind="manga", metadata=None):
    with database.write() as session:
        work = Work(id=1, pixiv_illust_id="101", artist_id=1, title="synthetic",
                    page_count=pages, type=kind, source_metadata=metadata or {})
        roles = ("ugoira_zip", "ugoira_timing") if kind == "ugoira" else ("image",) * len(states)
        work.assets = [Asset(id=f"asset-{page}", role=roles[page], page_index=0 if kind == "ugoira" else page,
                             state=state, relative_path=f"101_p{page}.png" if state in ("verified", "unknown", "source_changed") else None)
                       for page, state in enumerate(states)]
        session.add(work)


def views(library):
    card = library.query(compact=True)["items"][0]
    detail = library.detail(1, page_limit=1)
    feed = library.feed(limit=1)["items"][0]
    assert card["archive"] == detail["archive"] == feed["archive"]
    assert card["processing"] == detail["processing"] == feed["processing"]
    assert card["source_update"] == detail["source_update"] == feed["source_update"]
    return card, detail, feed


def test_first_readable_page_does_not_complete_ten_page_work(catalogue):
    database, library = catalogue
    add_work(database)
    with database.write() as session:
        session.add(ApplicationState(key="maintenance_work:1", value={"status": "waiting", "kind": "repair",
                    "next_retry_at": "2099-09-30T09:00:00", "reason": "retry scheduled"}))
    card, detail, feed = views(library)
    assert card["archive"] == {"state": "partial", "total": 10, "verified": 1, "missing": 9, "unknown": 0}
    assert card["preview"] == detail["pages"][0]["src"] == feed["src"] == "/images/101_p0.png"
    assert card["processing"]["state"] == "waiting"
    assert card["recovery"]["status"] == detail["recovery"]["status"] == feed["recovery"]["status"] == "waiting"


@pytest.mark.parametrize("states,expected", [(("verified", "verified"), "complete"),
                                             (("unknown", "unknown"), "unknown"),
                                             (("source_changed", "unknown"), "unknown")])
def test_verified_old_unknown_and_source_changed_are_distinct(catalogue, states, expected):
    database, library = catalogue
    add_work(database, pages=2, states=states)
    card, _, _ = views(library)
    assert card["archive"]["state"] == expected
    assert card["archive"]["missing"] == 0
    assert card["preview"]


def test_playable_gif_does_not_replace_required_zip_and_timing(catalogue):
    database, library = catalogue
    add_work(database, pages=1, states=("missing",), kind="ugoira")
    with database.write() as session:
        session.add(Derivative(work_id=1, kind="gif", state="missing", relative_path="legacy.gif"))
    card, detail, feed = views(library)
    assert card["archive"] == {"state": "partial", "total": 2, "verified": 0, "missing": 2, "unknown": 0}
    assert card["preview"] is None and card["preview_status"] == "pending"
    assert detail["pages"][0]["src"] == feed["src"] == "/images/legacy.gif"
    assert library.query()["items"][0]["preview"] == "/images/legacy.gif"


def test_source_update_and_unavailable_do_not_change_existing_original(catalogue):
    database, library = catalogue
    add_work(database, pages=1, metadata={"source_updates": {"asset-0": {"requested_url": "https://i.pximg.net/new.png",
             "reason": "source changed"}}, "remote_availability": {"state": "unavailable", "checked_at": "2026-09-30"}})
    card, _, _ = views(library)
    assert card["archive"]["state"] == "complete"
    assert card["source_update"] == {"pending": True, "reason": "source changed"}
    assert card["availability"]["state"] == "unavailable"
    assert card["preview"] == "/images/101_p0.png"


def test_finished_failure_wins_stale_queued_ledger_and_new_success_clears_it(catalogue):
    database, library = catalogue
    add_work(database, pages=2, states=("verified", "failed"))
    with database.write() as session:
        session.add(Operation(id="failed", kind="repair", dedupe_key="failed", label="repair", state="finished",
                              needs_attention=True))
        session.add(OperationItem(operation_id="failed", item_key="old", kind="download", phase="downloading",
                                  target={"asset_id": "asset-1"}, state="failed", result={"message": "download failed"}))
        session.add(ApplicationState(key="maintenance_work:1", value={"status": "queued", "operation_id": "failed"}))
    card, _, _ = views(library)
    assert card["processing"] == {"state": "attention", "operation_id": "failed", "next_retry_at": None, "reason": "download failed"}
    with database.write() as session:
        session.get(Asset, "asset-1").state = "verified"
        session.get(Asset, "asset-1").relative_path = "101_p1.png"
        session.add(Operation(id="success", kind="repair", dedupe_key="success", label="repair", state="finished"))
        session.add(OperationItem(operation_id="success", item_key="new", kind="download", phase="downloading",
                                  target={"work_id": 1}, state="succeeded"))
    card, _, _ = views(library)
    assert card["processing"]["state"] == "idle"
    assert card["archive"]["state"] == "complete"


def test_scoped_queued_work_and_legacy_derivative_item(catalogue):
    database, library = catalogue
    add_work(database, pages=1)
    with database.write() as session:
        session.add(Operation(id="queued", kind="repair", parameters={"work_ids": [1]},
                              dedupe_key="queued", label="repair", state="queued"))
    assert views(library)[0]["processing"]["state"] == "queued"
    with database.write() as session:
        session.get(Operation, "queued").state = "finished"
        session.add(Derivative(id="thumb", work_id=1, kind="thumbnail", state="missing"))
        session.add(Operation(id="local", kind="thumbnails", dedupe_key="local", label="thumbnail", state="running"))
        session.add(OperationItem(operation_id="local", item_key="local", kind="thumbnail", phase="local",
                                  target={"record_id": "thumb"}, state="running"))
    assert views(library)[0]["processing"]["state"] == "running"


def test_original_and_derivative_retry_schedules_are_independent(catalogue):
    database, library = catalogue
    add_work(database, pages=1)
    with database.write() as session:
        session.add(ApplicationState(key="maintenance_work:1", value={"status": "waiting", "kind": "repair",
                    "tracks": {"remote": {"status": "waiting", "next_retry_at": "2099-09-30T09:00:00", "reason": "stale original retry"},
                               "local": {"status": "waiting", "next_retry_at": "2099-09-30T10:00:00", "reason": "thumbnail retry"}}}))
    card, _, _ = views(library)
    assert card["archive"]["state"] == "complete"
    assert card["processing"]["state"] == "waiting"
    assert card["processing"]["reason"] == "thumbnail retry"
    with database.write() as session:
        session.add(Derivative(work_id=1, kind="thumbnail", state="verified", relative_path="101.webp"))
    assert views(library)[0]["processing"]["state"] == "idle"


@pytest.mark.parametrize("old_operation", [None, "succeeded"])
def test_legacy_attention_with_current_original_gap_and_after_restore(catalogue, old_operation):
    database, library = catalogue
    add_work(database, pages=2, states=("verified", "missing"))
    with database.write() as session:
        if old_operation:
            session.add(Operation(id="old", kind="repair", state="finished", dedupe_key="old", label="repair"))
            session.add(OperationItem(operation_id="old", item_key="old", kind="download", phase="downloading",
                target={"work_id": 1, "asset_id": "asset-0"}, state=old_operation))
        session.add(ApplicationState(key="maintenance_work:1", value={"status": "attention", "kind": "repair",
            "reason": "source unavailable", "operation_id": "old" if old_operation else "removed-operation"}))
    card, _, _ = views(library)
    assert card["processing"]["state"] == "attention"
    assert card["processing"]["reason"] == "source unavailable"
    assert card["preview"]
    with database.write() as session:
        asset = session.get(Asset, "asset-1")
        asset.state, asset.relative_path = "verified", "101_p1.png"
    card, _, _ = views(library)
    assert card["archive"]["state"] == "complete"
    assert card["processing"]["state"] == "idle"


def test_finished_original_failure_does_not_pollute_later_complete_state(catalogue):
    database, library = catalogue
    add_work(database, pages=1)
    with database.write() as session:
        session.add(Operation(id="failed", kind="repair", state="finished", dedupe_key="failed", label="repair"))
        session.add(OperationItem(operation_id="failed", item_key="failed", kind="download", phase="downloading",
            target={"work_id": 1, "asset_id": "asset-0"}, state="failed", result={"message": "old failure"}))
    assert views(library)[0]["processing"]["state"] == "idle"


def test_complete_views_skip_history_but_active_and_new_gap_keep_their_truth(catalogue, monkeypatch):
    from src.core import normalized_task_projection as tasks
    database, library = catalogue
    add_work(database, pages=1)
    with database.write() as session:
        session.add(Derivative(id='thumb', work_id=1, kind='thumbnail',
            state='verified', relative_path='101.webp'))
        session.add(Operation(id='failed', kind='repair', state='finished',
            dedupe_key='failed', label='repair'))
        session.add(OperationItem(operation_id='failed', item_key='failed', kind='download',
            phase='downloading', target={'asset_id': 'asset-0'}, state='failed',
            result={'message': 'original still missing'}))
    reads = []
    original = tasks._read_winners
    def capture(connection, selected, *, active_only):
        reads.append(active_only)
        return original(connection, selected, active_only=active_only)
    monkeypatch.setattr(tasks, '_read_winners', capture)
    assert views(library)[0]['processing']['state'] == 'idle'
    assert reads and all(reads)
    reads.clear()
    with database.write() as session:
        asset = session.get(Asset, 'asset-0')
        asset.state, asset.relative_path = 'missing', None
    card, _, _ = views(library)
    assert card['processing']['state'] == 'attention'
    assert card['processing']['reason'] == 'original still missing'
    assert False in reads
    reads.clear()
    with database.write() as session:
        session.add(Operation(id='active', kind='repair', state='running',
            dedupe_key='active', label='repair'))
        session.add(OperationItem(operation_id='active', item_key='active', kind='download',
            phase='downloading', target={'work_id': 1}, state='running'))
    assert views(library)[0]['processing']['state'] == 'running'
    assert reads and all(reads)


def test_legacy_scoped_error_without_target_work_id_is_attention(catalogue):
    database, library = catalogue
    add_work(database, pages=2)
    with database.write() as session:
        session.add(Operation(id="failed", kind="repair", state="finished", dedupe_key="failed", label="repair",
                              parameters={"work_ids": [1]}))
        session.add(OperationItem(operation_id="failed", item_key="error", kind="error", phase="checking",
            target={"message": "source unavailable"}, state="failed", result={"message": "source unavailable"}))
    card, _, _ = views(library)
    assert card["processing"]["state"] == "attention"
    assert card["processing"]["operation_id"] == "failed"


@pytest.mark.parametrize("missing_kind", ["original", "thumbnail"])
def test_observed_absent_media_updates_bounded_views_and_revision(tmp_path, missing_kind):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    try:
        with runtime.database.write() as session:
            session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        add_work(runtime.database, pages=2, states=("verified", "verified"))
        with runtime.database.write() as session:
            session.add(Derivative(work_id=1, kind="thumbnail", state="verified", relative_path="101.webp"))
        # All paths are absent in the synthetic directory. List projections trust
        # recorded facts; the particular requested file reveals its absence.
        before = runtime.library.query(compact=True)
        assert before["items"][0]["archive"]["state"] == "complete"
        relative = "101_p1.png" if missing_kind == "original" else "101.webp"
        assert runtime.media_path(relative) is None
        card, detail, feed = views(runtime.library)
        assert card["archive"]["state"] == ("partial" if missing_kind == "original" else "complete")
        after = runtime.library.query(compact=True)
        assert after["catalogue_revision"] == before["catalogue_revision"] + 1
        assert detail["catalogue_revision"] == after["catalogue_revision"]
        assert runtime.media_path(relative) is None
        assert runtime.library.query(compact=True)["catalogue_revision"] == after["catalogue_revision"]
    finally:
        assert runtime.close()


def test_observed_absent_legacy_gif_stops_claiming_readable_preview(tmp_path):
    runtime = Runtime(tmp_path / "archive.db", tmp_path / "images", create=True)
    try:
        with runtime.database.write() as session:
            session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        add_work(runtime.database, pages=1, states=(), kind="ugoira")
        with runtime.database.write() as session:
            session.add(Derivative(work_id=1, kind="gif", state="missing", relative_path="legacy.gif"))
        before = runtime.library.query(compact=True)
        card, detail, feed = views(runtime.library)
        assert card["preview"] is None and card["preview_status"] == "pending"
        assert detail["pages"][0]["src"] == feed["src"] == "/images/legacy.gif"
        assert runtime.library.query()["items"][0]["preview"] == "/images/legacy.gif"
        assert runtime.media_path("legacy.gif") is None
        card, detail, feed = views(runtime.library)
        assert card["preview"] is detail["pages"][0]["src"] is feed["src"] is None
        assert card["preview_status"] == "pending"
        original_view = runtime.library.query()["items"][0]
        assert original_view["preview"] is None and original_view["preview_status"] == "unavailable"
        assert card["archive"] == {"state": "partial", "total": 2, "verified": 0, "missing": 2, "unknown": 0}
        assert runtime.library.query(compact=True)["catalogue_revision"] == before["catalogue_revision"] + 1
    finally:
        assert runtime.close()


def test_window_reads_bound_entities_and_queries_with_5000_works_and_2000_pages(catalogue):
    database, library = catalogue
    with database.write() as session:
        session.execute(Work.__table__.insert(), [{"id": index, "pixiv_illust_id": str(index), "artist_id": 1,
            "title": "synthetic", "page_count": 2000 if index == 5000 else 1, "type": "manga"}
            for index in range(1, 5001)])
        session.execute(Asset.__table__.insert(), [{"id": f"long-{index}", "work_id": 5000,
            "role": "image", "page_index": index, "state": "verified", "relative_path": f"5000_p{index}.png"}
            for index in range(2000)])
        session.add(Operation(id="long", kind="repair", state="finished", dedupe_key="long", label="repair"))
        session.flush()
        session.execute(OperationItem.__table__.insert(), [{"id": f"item-{index}", "operation_id": "long",
            "item_key": str(index), "kind": "download", "phase": "downloading", "state": "succeeded",
            "target": {"work_id": 5000, "asset_id": f"long-{index}"}} for index in range(2000)])
        session.add(ApplicationState(key="catalogue_revision", value=7))
    statements, loaded = [], []

    def statement(connection, cursor, sql, parameters, context, many):
        statements.append(sql)

    def load(record, context):
        loaded.append(type(record).__name__)

    event.listen(database.engine, "before_cursor_execute", statement)
    for model in (Asset, OperationItem, Work):
        event.listen(model, "load", load)
    try:
        cards = library.query(compact=True, limit=60, ceiling=5000)
        assert len(cards["items"]) == 60
        assert cards["items"][0]["archive"]["total"] == 2000
        assert cards["items"][0]["archive"]["verified"] == 2000
        # State/display projections and cursor boundary checks add fixed reads;
        # each 60-work hydration still uses one state and one display batch.
        assert len(statements) <= 20
        assert sum(sql.startswith("SELECT work_id,value FROM read_work_state") for sql in statements) == 1
        assert sum(sql.startswith("SELECT scope,availability FROM read_display_metadata") for sql in statements) == 1
        assert loaded.count("Work") == 60
        assert loaded.count("Asset") == 1
        assert "OperationItem" not in loaded
        statements.clear()
        loaded.clear()
        feed = library.feed(ceiling=5000, offset=1880, limit=120)
        assert len(feed["items"]) == 120
        assert len(statements) <= 12
        assert sum(sql.startswith("SELECT work_id,value FROM read_work_state") for sql in statements) == 1
        assert sum(sql.startswith("SELECT scope,availability FROM read_display_metadata") for sql in statements) == 1
        assert loaded.count("Work") == 1
        assert loaded.count("Asset") == 120
        assert "OperationItem" not in loaded
        assert all(item["archive"]["state"] == "complete" for item in feed["items"])
        detail = library.detail(5000, page_offset=1999, page_limit=1)
        assert detail["archive"] == feed["items"][0]["archive"]
        assert len(detail["pages"]) == 1
        assert cards["catalogue_revision"] == feed["catalogue_revision"] == detail["catalogue_revision"] == 7
    finally:
        event.remove(database.engine, "before_cursor_execute", statement)
        for model in (Asset, OperationItem, Work):
            event.remove(model, "load", load)


def test_motion_card_reads_are_batched_for_current_window(catalogue):
    database, library = catalogue
    with database.write() as session:
        session.execute(Work.__table__.insert(), [{"id": index, "pixiv_illust_id": str(index), "artist_id": 1,
            "title": "synthetic animation", "page_count": 1, "type": "ugoira"} for index in range(1, 62)])
        session.execute(Derivative.__table__.insert(), [{"id": f"gif-{index}", "work_id": index,
            "kind": "gif", "spec_key": "v1", "page_index": 0, "state": "verified", "sha256": "a" * 64,
            "relative_path": f"{index}.gif"} for index in range(1, 62)])
        session.execute(Derivative.__table__.insert(), [{"id": f"card-{index}", "work_id": index,
            "kind": "thumbnail", "spec_key": "card-motion-v1", "page_index": 0, "state": "verified",
            "sha256": "b" * 64, "source_version": "a" * 64,
            "relative_path": f".thumbnails/{index}_card_motion_v1.webp"} for index in range(1, 62)])
    statements, loaded = [], []
    def statement(connection, cursor, sql, parameters, context, many):
        statements.append(sql)
    def load(record, context):
        if record.spec_key == "card-motion-v1":
            loaded.append(record.work_id)
    event.listen(database.engine, "before_cursor_execute", statement)
    event.listen(Derivative, "load", load)
    try:
        one = library.query(compact=True, limit=1, ceiling=60)
        # Each boundary uses at most two disjoint indexed ranges. A window
        # ending at the final tie can require one more empty range than K=1.
        is_seek = lambda sql: sql.startswith("SELECT work_id,order_key FROM read_work_order")
        single_hydration_count = sum(not is_seek(sql) for sql in statements)
        assert len(statements) <= 20
        assert sum(is_seek(sql) for sql in statements) <= 5
        assert len(one["items"]) == 1 and len(loaded) == 1
        statements.clear()
        loaded.clear()
        window = library.query(compact=True, limit=60, ceiling=60)
        assert len(statements) <= 20
        assert sum(not is_seek(sql) for sql in statements) == single_hydration_count
        assert sum(is_seek(sql) for sql in statements) <= 5
        assert sum(sql.startswith("SELECT derivatives.id") for sql in statements) == 1
        assert sum(sql.startswith("SELECT work_id,value FROM read_work_state") for sql in statements) == 1
        assert len(loaded) == 60 and set(loaded) == set(range(1, 61))
        assert all("card_motion_v1.webp?v=" in card["preview"] for card in window["items"])
    finally:
        event.remove(database.engine, "before_cursor_execute", statement)
        event.remove(Derivative, "load", load)
