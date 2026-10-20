"""Independent product-path contracts; every fixture is a synthetic database.

The raw state/query readers are deliberately retained as semantic oracles. These
tests exercise the integrated Library and HTTP paths rather than storage helpers.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select

from src.core.archive_state import raw_work_states, work_states
from src.core.database import ArchiveDatabase
from src.core.library import Library, LibraryFilter
from src.core.models import (
    ArchiveIntent, Artist, Asset, Derivative, LocalPreference, Operation,
    OperationItem, RemoteBookmark, RevisionAsset, RevisionDerivative, Work,
    WorkRevision, WorkTag,
)
from src.core.read_projection import CursorExpired
from src.core.reading import ReadingSessions
from src.core.revision_views import media_maps, selected_snapshots
from src.routes.archive_api import create_router


REFERENCE = datetime(2026, 10, 4, 12)


@pytest.fixture
def library(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "synthetic.db")
    result = Library(database)
    with database.write() as session:
        session.add_all([Artist(id=i, pixiv_user_id=str(i), name=f"synthetic {i}") for i in (1, 2)])
        session.flush()
        session.add_all([Work(id=i, pixiv_illust_id=str(1000+i), artist_id=1 if i <= 30 else 2,
            title=("Alpha" if i % 3 else "alpha") + str(i % 7),
            type="manga" if i % 2 else "illust", page_count=i % 3 + 1,
            posted_at=None if i % 11 == 0 else REFERENCE-timedelta(days=i // 4),
            downloaded_at=REFERENCE-timedelta(days=i // 5),
            time_basis="legacy_unknown" if i == 3 else "utc") for i in range(1, 62)])
        session.flush()
        session.add_all([WorkTag(work_id=i, tag="A" if i % 2 else "B") for i in range(1, 62)])
        session.add(WorkTag(work_id=5, tag="B"))
        session.add_all([LocalPreference(work_id=i, is_hidden=True) for i in (2, 15)])
        session.add(ArchiveIntent(pixiv_illust_id="1005", mode="suppressed"))
        session.add_all([RemoteBookmark(work_id=i, value=True, confirmed_at=REFERENCE) for i in (1, 2, 20, 50)])
    try:
        yield result
    finally:
        database.close()


@contextmanager
def api_client(library):
    runtime = SimpleNamespace(database=library.database, library=library,
                              reading=ReadingSessions(library))
    app = FastAPI()
    app.include_router(create_router(lambda: runtime))
    with TestClient(app, base_url="http://localhost") as client:
        yield client


def _expected_ids(library, filters, ceiling):
    with library.database.read() as session:
        return list(session.scalars(library._statement(filters, ceiling).with_only_columns(Work.id)
                                   .order_by(*library._order(filters))))


def _assert_cursor_shape(page):
    assert isinstance(page["generation"], int)
    assert isinstance(page["ceiling"], int)
    assert isinstance(page["as_of"], str)
    assert page["performance"] in ("indexed_seek", "on_demand_scan")
    assert page["total"] is None or type(page["total"]) is int
    assert page["boundaries"] == [item["cursor"] for item in page["items"]]
    assert all(isinstance(cursor, str) and cursor for cursor in page["boundaries"])


@pytest.mark.parametrize("sort", ["newest", "oldest", "added", "title"])
@pytest.mark.parametrize("scope", ["all", "artist", "type", "favorite", "arbitrary"])
def test_cursor_matches_legacy_order_both_directions_with_ties_nulls(library, sort, scope):
    kwargs = {"sort": sort, "as_of": REFERENCE}
    kwargs.update({"all": {}, "artist": {"artist_ids": (1,)}, "type": {"types": ("manga",)},
                  "favorite": {"favorites": True}, "arbitrary": {"tags_exclude": ("A",), "q": "alpha"}}[scope])
    filters = LibraryFilter(**kwargs)
    expected = _expected_ids(library, filters, 61)
    page = library.list_cursor(filters, limit=7, ceiling=61)
    collected, prior = [], None
    for _ in range(12):
        _assert_cursor_shape(page)
        if scope == "arbitrary":
            assert page["performance"] == "on_demand_scan" and page["total"] is None
            assert page["count_mode"] == "on_demand"
        else:
            assert page["performance"] == "indexed_seek" and page["total"] == len(expected)
            assert page["count_mode"] == "indexed"
        ids = [item["id"] for item in page["items"]]
        assert ids == expected[len(collected):len(collected)+7]
        if prior is not None:
            backwards = library.list_cursor(filters, limit=7, cursor=page["previous_cursor"],
                                            direction="previous", ceiling=page["ceiling"])
            assert [item["id"] for item in backwards["items"]] == prior
        collected.extend(ids)
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"]
        prior = ids
        previous_page = page
        page = library.list_cursor(filters, limit=7, cursor=page["next_cursor"], ceiling=61)
        assert page["generation"] == previous_page["generation"]
        assert page["as_of"] == previous_page["as_of"]
    else:
        pytest.fail("cursor did not terminate within the bounded synthetic fixture")
    assert collected == expected
    assert len(collected) == len(set(collected))


def test_anchor_recovery_and_neighbors_preserve_card_boundaries(library):
    filters = LibraryFilter(as_of=REFERENCE)
    expected = _expected_ids(library, filters, 61)
    anchor = expected[28]
    page = library.list_cursor(filters, limit=7, ceiling=61, anchor_work_id=anchor)
    assert [item["id"] for item in page["items"]] == expected[28:35]
    for offset, card in enumerate(page["items"]):
        index = 28 + offset
        neighbors = library.cursor_neighbors(card["id"], filters, ceiling=61, cursor=card["cursor"])
        assert neighbors["index"] is None
        assert neighbors["anchor_cursor"] == card["cursor"]
        assert neighbors["previous"]["work_id"] == expected[index-1]
        assert neighbors["next"]["work_id"] == expected[index+1]
        for direction in ("previous", "next"):
            ref = neighbors[direction]
            assert set(("work_id", "revision_id", "page_count", "cursor")) <= set(ref)


def test_indexed_title_sort_preserves_sqlite_unicode_lower_semantics(library):
    titles = ["Ä", "ä", "İ", "i", "Σ", "σ", "ß", "SS", "Å", "å", "Alpha", "alpha"]
    with library.database.write() as session:
        for work_id, title in enumerate(titles, 20):
            session.get(Work, work_id).title = title
    filters = LibraryFilter(sort="title", as_of=REFERENCE)
    expected = _expected_ids(library, filters, 61)
    page = library.list_cursor(filters, limit=7)
    collected = []
    while True:
        collected.extend(item["id"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        page = library.list_cursor(filters, limit=7, cursor=page["next_cursor"])
    assert collected == expected


@pytest.mark.parametrize("sort", ["newest", "oldest", "added", "title"])
@pytest.mark.parametrize("posted", [None, REFERENCE])
def test_dense_tied_indexed_sort_boundaries_preserve_order_and_bounded_plans(library, sort, posted):
    from structural_scale_probe import Probe
    with library.database.write() as session:
        for work in session.scalars(select(Work)):
            work.title, work.posted_at, work.downloaded_at = "same title", posted, REFERENCE
    filters = LibraryFilter(sort=sort, as_of=REFERENCE)
    expected = _expected_ids(library, filters, 61)
    page, evidence = Probe(library.database).measure("dense_tie", lambda: library.list_cursor(filters, limit=20))
    assert [card["id"] for card in page["items"]] == expected[:20]
    assert evidence["vm_steps"] < 10000
    seeks = [entry for entry in evidence["statements"]
             if entry["sql"].startswith("SELECT work_id,") and any(
                 predicate in entry["sql"].replace(" ", "") for predicate in ("work_id>", "work_id<"))]
    assert seeks
    assert any(any("work_id" in detail or "rowid" in detail for *_, detail in entry["plan"]) for entry in seeks)
    # EXPLAIN does not reveal which of two same-column upper bounds SQLite
    # chooses. Per-seek VM work catches a wide ceiling followed by row filtering.
    for entry in seeks:
        if any(f"{key}=?" in entry["sql"] for key in ("order_key", "added_key", "title_key")):
            assert entry["vm_steps"] <= 8 * entry["parameters"][-1] + 80, entry
    second = library.list_cursor(filters, limit=20, cursor=page["next_cursor"])
    assert [card["id"] for card in second["items"]] == expected[20:40]
    previous = library.list_cursor(filters, limit=20, cursor=second["previous_cursor"], direction="previous")
    assert [card["id"] for card in previous["items"]] == expected[:20]


def test_compact_product_cards_leave_unused_large_source_json_deferred(library):
    large = {"meta_pages": [{"page": i, "unused": "x"*128} for i in range(10000)],
             "remote_availability": {"state": "private", "confirmed": True, "policy": "source-v2"}}
    with library.database.write() as session:
        session.get(Work, 1).source_metadata = large
        session.get(Work, 20).source_metadata = large
        session.add(WorkRevision(id="large-json", work_id=20, ordinal=1, title="pinned large",
            type="illust", page_count=1, source_signature="large-json", source_metadata=large,
            caption="x"*65536, tags=["unused compact tag"]))
        session.get(Work, 20).default_revision_id = session.get(Work, 20).latest_revision_id = "large-json"
    loaded, queries = [], []
    def load(record, context):
        loaded.append((type(record).__name__, record.id, "source_metadata" in inspect(record).unloaded))
    def capture(connection, cursor, sql, parameters, context, many):
        queries.append(sql)
    event.listen(Work, "load", load)
    event.listen(WorkRevision, "load", load)
    event.listen(library.database.engine, "before_cursor_execute", capture)
    try:
        cards = {card["id"]: card for card in library.list_cursor(limit=100, compact=True)["items"]}
    finally:
        event.remove(Work, "load", load)
        event.remove(WorkRevision, "load", load)
        event.remove(library.database.engine, "before_cursor_execute", capture)
    assert cards[20]["title"] == "pinned large"
    assert cards[1]["availability"]["effective"] is True
    assert loaded and all(deferred for _, _, deferred in loaded), loaded
    for sql in queries:
        if sql.startswith("SELECT works.") or sql.startswith("SELECT work_revisions."):
            columns = sql.split("\nFROM ", 1)[0]
            assert "works.source_metadata" not in columns
            assert "work_revisions.source_metadata" not in columns


@pytest.mark.parametrize("mutation", ["delete", "hidden", "sort"])
def test_changed_anchor_expires_cursor_and_identity_can_recover(library, mutation):
    page = library.list_cursor(limit=7)
    anchor = page["items"][-1]["id"]
    with library.database.write() as session:
        if mutation == "delete":
            session.delete(session.get(Work, anchor))
        elif mutation == "hidden":
            session.add(LocalPreference(work_id=anchor, is_hidden=True))
        else:
            session.get(Work, anchor).posted_at = None
    with pytest.raises(CursorExpired):
        library.list_cursor(limit=7, cursor=page["next_cursor"])
    with pytest.raises(CursorExpired):
        library.cursor_neighbors(anchor, cursor=page["next_cursor"])
    retained = anchor if mutation == "sort" else page["items"][2]["id"]
    recovered = library.list_cursor(limit=7, anchor_work_id=retained, ceiling=page["ceiling"])
    assert recovered["generation"] > page["generation"]
    assert recovered["items"][0]["id"] == retained


@pytest.mark.parametrize("mutation", ["insert", "bookmark", "title"])
def test_unrelated_changes_continue_cursor_with_original_ceiling(library, mutation):
    page = library.list_cursor(limit=7)
    expected = _expected_ids(library, LibraryFilter(), page["ceiling"])
    anchor = page["items"][-1]["id"]
    with library.database.write() as session:
        if mutation == "insert":
            session.add(Work(id=62, pixiv_illust_id="1062", artist_id=1, title="new", posted_at=REFERENCE))
        elif mutation == "bookmark":
            session.get(RemoteBookmark, 1).value = False
        else:
            session.get(Work, anchor).title = "New title does not change newest order"
    tail = library.list_cursor(limit=7, cursor=page["next_cursor"])
    assert tail["continuation_rebased"] and tail["generation"] > page["generation"]
    assert tail["ceiling"] == page["ceiling"]
    assert [item["id"] for item in tail["items"]] == expected[7:14]
    near = library.cursor_neighbors(anchor, cursor=page["next_cursor"],
        from_cursor=page["items"][-2]["cursor"])
    assert near["continuation_rebased"] and near["next"]["work_id"] == expected[7]
    # The browser can reuse another old boundary after a second normal write.
    with library.database.write() as session:
        session.get(RemoteBookmark, 1).value = True if mutation == "bookmark" else False
    back = library.list_cursor(limit=7, cursor=tail["previous_cursor"], direction="previous")
    assert [item["id"] for item in back["items"]] == expected[:7]


def test_direct_neighbor_revalidates_origin_and_adjacency(library):
    page = library.list_cursor(limit=3)
    first, middle, _ = page["items"]
    with library.database.write() as session:
        # A new position between the original pair changes adjacency although
        # both cached boundary keys themselves are still valid.
        session.get(LocalPreference, 2).is_hidden = False
    with pytest.raises(CursorExpired):
        library.cursor_neighbors(first["id"], cursor=first["cursor"],
            from_cursor=middle["cursor"], from_direction="previous")
    fresh = library.cursor_neighbors(middle["id"])
    assert fresh["previous"]["work_id"] == 2
    # Moving the origin itself must also trigger identity recovery.
    with library.database.write() as session:
        session.get(Work, middle["id"]).posted_at = None
    with pytest.raises(CursorExpired):
        library.cursor_neighbors(first["id"], cursor=first["cursor"],
            from_cursor=middle["cursor"], from_direction="previous")


def test_favorite_anchor_must_still_belong_to_scope(library):
    filters = LibraryFilter(favorites=True)
    first = library.list_cursor(filters, limit=1)
    anchor = first["items"][0]["id"]
    with library.database.write() as session:
        session.get(RemoteBookmark, anchor).value = False
    with pytest.raises(CursorExpired):
        library.list_cursor(filters, cursor=first["next_cursor"])


def test_feed_keeps_one_snapshot_while_background_changes_scope(library, monkeypatch):
    original = library.query_cursor
    changed = False

    def update_after_cards(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            changed = True
            with library.database.write() as session:
                session.add(LocalPreference(work_id=result["items"][0]["id"], is_hidden=True))
        return result

    expected = library.cursor_feed(limit=5)
    monkeypatch.setattr(library, "query_cursor", update_after_cards)
    batch = library.cursor_feed(limit=5)
    assert [(p["work_id"], p["page"]) for p in batch["items"]] == [
        (p["work_id"], p["page"]) for p in expected["items"]]
    assert batch["generation"] == expected["generation"]
    assert library.list_cursor(limit=1)["items"][0]["id"] != batch["items"][0]["work_id"]


def test_cursor_filter_fence_and_http_expiry_are_explicit(library):
    page = library.list_cursor(limit=7)
    with pytest.raises(ValueError, match="筛选|范围"):
        library.list_cursor(LibraryFilter(types=("manga",)), cursor=page["next_cursor"])
    with api_client(library) as client:
        response = client.get("/api/library/cursor", params={"limit": 7, "compact": "true"})
        assert response.status_code == 200, response.text
        body = response.json()
        _assert_cursor_shape(body)
        with library.database.write() as session:
            session.get(Work, body["items"][-1]["id"]).posted_at = None
        expired = client.get("/api/library/cursor", params={"cursor": body["next_cursor"], "limit": 7})
        assert expired.status_code == 409, expired.text


def test_http_relative_filter_cursor_preserves_implicit_snapshot_time(library):
    with api_client(library) as client:
        first = client.get("/api/library/cursor", params={"limit": 7, "recent": "1"})
        assert first.status_code == 200, first.text
        page = first.json()
        assert page["next_cursor"]
        second = client.get("/api/library/cursor", params={"limit": 7, "recent": "1", "cursor": page["next_cursor"]})
        assert second.status_code == 200, second.text
        assert second.json()["as_of"] == page["as_of"]


def test_cursor_feed_appends_and_prepends_same_pages_as_numeric_feed(library):
    filters = LibraryFilter(as_of=REFERENCE)
    expected = library.feed(filters, offset=0, limit=40, ceiling=61)["items"]
    identity = lambda items: [(item["work_id"], item["page"]) for item in items]
    with api_client(library) as client:
        first = client.get("/api/library/cursor/feed", params={"limit": 20, "as_of": REFERENCE.isoformat()})
        assert first.status_code == 200, first.text
        initial = first.json()["items"]
        assert identity(initial) == identity(expected[:20])
        last = initial[-1]
        second = client.get("/api/library/cursor/feed", params={"limit": 20,
            "cursor": last["work_cursor"], "anchor_work_id": last["work_id"],
            "page": last["page"]+1, "index": last["index"]+1, "work_index": last["work_index"]})
        assert second.status_code == 200, second.text
        continuation = second.json()["items"]
        assert identity(continuation) == identity(expected[20:40])
        head = continuation[0]
        previous = client.get("/api/library/cursor/feed", params={"limit": 20,
            "cursor": head["work_cursor"], "anchor_work_id": head["work_id"], "direction": "previous",
            "page": head["page"]-1, "index": head["index"]-1, "work_index": head["work_index"]})
        assert previous.status_code == 200, previous.text
        assert identity(previous.json()["items"]) == identity(initial)


@pytest.mark.parametrize("filters", [
    LibraryFilter(), LibraryFilter(show_hidden=True), LibraryFilter(artist_ids=()),
    LibraryFilter(artist_ids=(1,)), LibraryFilter(artist_ids_exclude=(1,)),
    LibraryFilter(types=()), LibraryFilter(types=("manga",)), LibraryFilter(tags=("A",)),
    LibraryFilter(tags_exclude=("A",)), LibraryFilter(q="Alpha1"), LibraryFilter(q="%"),
    LibraryFilter(favorites=True), LibraryFilter(recent=True, as_of=REFERENCE),
    LibraryFilter(posted_after=REFERENCE-timedelta(days=8), tz_offset_minutes=-480),
    LibraryFilter(posted_before=REFERENCE-timedelta(days=8), tz_offset_minutes=-480),
])
def test_legacy_numeric_pages_and_filtered_exact_counts_remain_exact(library, filters):
    expected = _expected_ids(library, filters, 50)
    for offset in (0, 5, 29, 999):
        page = library.query(filters, offset=offset, limit=7, ceiling=50, compact=True)
        assert page["total"] == len(expected)
        assert page["offset"] == offset
        assert [item["id"] for item in page["items"]] == expected[offset:offset+7]
        assert page["has_more"] == (offset + len(page["items"]) < len(expected))
    assert library.counts(filters, ceiling=50)["total"] == len(expected)


def test_frozen_random_reading_retains_membership_round_and_revision(library):
    reading = ReadingSessions(library)
    filters = LibraryFilter(artist_ids=(1,), as_of=REFERENCE)
    expected = set(_expected_ids(library, filters, 61))
    visit = reading.create(filters, ceiling=61)
    token = visit["session_id"]
    first = [reading.entry(token, i, mode="random", round_number=0)["work_id"]
             for i in range(visit["total"])]
    assert set(first) == expected and len(first) == len(expected)
    with library.database.write() as session:
        session.add(Work(id=62, pixiv_illust_id="1062", artist_id=1, title="new synthetic"))
        session.add(LocalPreference(work_id=first[0], is_hidden=True))
    assert [reading.entry(token, i, mode="random", round_number=0)["work_id"]
            for i in range(visit["total"])] == first
    second = [reading.entry(token, i, mode="random", round_number=1)["work_id"]
              for i in range(visit["total"])]
    assert set(second) == expected and second[0] != first[-1]
    reading.exclude(token, first[0])
    assert {reading.entry(token, i, mode="random")["work_id"]
            for i in range(visit["total"]-1)} == expected - {first[0]}


def _compare_states(library, ids):
    with library.database.read() as session:
        works = list(session.scalars(select(Work).where(Work.id.in_(ids))))
        projected = work_states(session, works)[0]
        raw = raw_work_states(session, works)[0]
        assert projected == raw
        return projected


@pytest.mark.parametrize("target", [
    {"work_id": 1}, {"work_id": 1.0}, {"work_id": True}, {"work_id": "1"},
    {"work_id": "01"}, {"work_id": 1, "asset_id": "asset-2"},
    {"work_id": None, "asset_id": "asset-1"},
    {"asset_id": "asset-2", "record_id": "record-1"},
    {"asset_id": "absent", "record_id": "record-1"},
    {"asset_id": 7}, {"asset_id": 7.0}, {"asset_id": "07"},
    {"record_id": 8}, {"record_id": 8.0}, {"record_id": "08"},
])
def test_persistent_task_state_preserves_old_json_identity_and_parent_updates(library, target):
    with library.database.write() as session:
        session.add_all([Asset(id="asset-1", work_id=1, role="image", page_index=0),
            Asset(id="asset-2", work_id=2, role="image", page_index=0),
            Asset(id="7", work_id=7, role="image", page_index=0),
            Asset(id="7.0", work_id=8, role="image", page_index=0),
            Derivative(id="record-1", work_id=1, kind="thumbnail"),
            Derivative(id="8", work_id=9, kind="thumbnail"),
            Derivative(id="8.0", work_id=10, kind="thumbnail")])
        session.add(Operation(id="typed", kind="repair", dedupe_key="typed", label="synthetic",
            state="running", parameters={}, created_at=REFERENCE, updated_at=REFERENCE))
        session.flush()
        session.add(OperationItem(id="typed-item", operation_id="typed", item_key="typed", position=0,
            kind="checked", target=target, phase="verifying", state="running", result={"message": "typed"}))
    _compare_states(library, list(range(1, 11)))
    with library.database.write() as session:
        session.get(Operation, "typed").state = "finished"
        session.get(OperationItem, "typed-item").state = "failed"
    _compare_states(library, list(range(1, 11)))
    with library.database.write() as session:
        session.get(OperationItem, "typed-item").target = {"work_id": 3}
        session.get(Operation, "typed").state = "running"
        session.get(OperationItem, "typed-item").state = "pending"
    _compare_states(library, list(range(1, 11)))


@pytest.mark.parametrize("work_ids", [None, [], [1], ["01"], 1, "01", True, {"legacy": 1}])
def test_scope_fallback_keeps_legacy_present_json_types(library, work_ids):
    with library.database.write() as session:
        session.add(Operation(id="scope", kind="verify", dedupe_key="scope", label="synthetic",
            state="running", parameters={"work_ids": work_ids}, created_at=REFERENCE))
        session.flush()
        session.add_all([OperationItem(id=f"scope-{i}", operation_id="scope", item_key=str(i),
            kind="checked", target={}, phase="verifying", state=state, position=i,
            result={"message": state}) for i, state in enumerate(("failed", "pending", "running"))])
    states = _compare_states(library, [1, 2])
    if work_ids not in (None, []):
        assert states[1]["processing"]["state"] == "running"
    with library.database.write() as session:
        session.get(Operation, "scope").state = "finished"
        session.get(Operation, "scope").parameters = {"work_ids": [2]}
    _compare_states(library, [1, 2])


def test_current_work_shape_and_explicit_revision_shape_stay_distinct(library):
    with library.database.write() as session:
        work = session.get(Work, 1)
        work.type, work.page_count = "manga", 3
        session.add(WorkRevision(id="old", work_id=1, ordinal=1, title="old", type="illust",
            page_count=1, source_signature="old"))
        session.add(WorkRevision(id="animation", work_id=1, ordinal=2, title="animation", type="ugoira",
            page_count=1, source_signature="animation"))
        session.flush()
        work.default_revision_id = work.latest_revision_id = "old"
        session.add(Asset(id="old-page", work_id=1, revision_id="old", role="image", page_index=0,
            state="verified", relative_path="synthetic.png"))
        session.add(Derivative(id="physical", work_id=2, kind="thumbnail", page_index=0,
            spec_key="v1", state="verified", relative_path="synthetic-thumb.png"))
        session.flush()
        session.add(RevisionAsset(revision_id="old", role="image", page_index=0, asset_id="old-page"))
        session.add(RevisionDerivative(revision_id="old", kind="thumbnail", page_index=0,
            spec_key="v1", derivative_id="physical"))
    current = _compare_states(library, [1])[1]
    assert current["archive"] == {"state": "partial", "total": 3, "verified": 1, "missing": 2, "unknown": 0}
    with library.database.read() as session:
        revision = session.get(WorkRevision, "old")
        assert library._revision_archive(session, revision) == library._raw_revision_archive(session, revision)
        assert library._revision_archive(session, revision)["total"] == 1
        assert library._revision_archive(session, session.get(WorkRevision, "animation"))["total"] == 2
    with library.database.write() as session:
        session.get(Derivative, "physical").work_id = 1
        session.get(Derivative, "physical").kind = "gif"
        session.get(Asset, "old-page").relative_path = ""
    _compare_states(library, [1, 2])
    with library.database.read() as session:
        revision = session.get(WorkRevision, "old")
        assert library._revision_archive(session, revision) == library._raw_revision_archive(session, revision)
        assert library._revision_archive(session, revision)["missing"] == 1


def test_mixed_work_media_matches_independent_selected_revision_reads(library):
    """A card's media cannot change because another work shares its window.

    This intentionally corrects the legacy mixed-window cross-owner leak while
    keeping the established single-work behavior and resource-count semantics.
    """
    with library.database.write() as session:
        for work_id in (1, 2):
            session.add(WorkRevision(id=f"cross-{work_id}", work_id=work_id, ordinal=1,
                title=f"cross {work_id}", type="illust", page_count=1, source_signature=f"cross-{work_id}"))
            work = session.get(Work, work_id)
            work.type, work.page_count = "illust", 1
            work.default_revision_id = work.latest_revision_id = f"cross-{work_id}"
        session.add(Asset(id="physical-cross", work_id=2, role="image", page_index=9,
            state="verified", relative_path="synthetic-cross.png"))
        session.add(Derivative(id="physical-cross-thumb", work_id=2, kind="thumbnail", page_index=9,
            spec_key="v2", state="verified", relative_path="synthetic-cross.webp"))
        session.flush()
        session.add(RevisionAsset(revision_id="cross-1", role="image", page_index=0, asset_id="physical-cross"))
        session.add(RevisionDerivative(revision_id="cross-1", kind="thumbnail", page_index=0,
            spec_key="v1", derivative_id="physical-cross-thumb"))
    with library.database.read() as session:
        works = [session.get(Work, i) for i in (1, 2)]
        snapshots = selected_snapshots(session, works)
        assets, derivatives = media_maps(session, works, snapshots, ranges={1: (0, 1), 2: (0, 1)})
        expected_assets, expected_derivatives = {}, {}
        for work in works:
            one_assets, one_derivatives = media_maps(session, [work],
                {work.id: snapshots[work.id]}, ranges={work.id: (0, 1)})
            expected_assets.update(one_assets)
            expected_derivatives.update(one_derivatives)
        assert assets == expected_assets == {}
        assert derivatives == expected_derivatives == {}
    _compare_states(library, [1, 2])


def test_legacy_media_window_does_not_scan_unrelated_revision_bindings(library):
    """A false LEFT JOIN can still walk every binding for each legacy slot."""
    from structural_scale_probe import Probe
    with library.database.write() as session:
        work = session.get(Work, 1)
        work.type, work.page_count = "illust", 1
        session.add(Asset(id="legacy-one", work_id=1, role="image", page_index=0,
            state="verified", relative_path="synthetic-legacy.png"))
        session.add(Derivative(id="legacy-thumb", work_id=1, kind="thumbnail", page_index=0,
            spec_key="v1", state="verified", relative_path="synthetic-legacy.webp"))
    library.detail(1, page_limit=1)
    before, baseline = Probe(library.database).measure("legacy_before_bindings",
        lambda: library.detail(1, page_limit=1))
    with library.database.write() as session:
        session.add(WorkRevision(id="unrelated-bindings", work_id=20, ordinal=1,
            title="synthetic unrelated bindings", type="manga", page_count=2000,
            source_signature="unrelated-bindings"))
        session.add(Asset(id="unrelated-one", work_id=20, role="image", page_index=0,
            state="verified", relative_path="synthetic-unrelated.png"))
        session.add(Derivative(id="unrelated-thumb", work_id=20, kind="thumbnail", page_index=0,
            spec_key="v1", state="verified", relative_path="synthetic-unrelated.webp"))
        session.flush()
        session.execute(RevisionAsset.__table__.insert(), [dict(revision_id="unrelated-bindings",
            role="image", page_index=i, asset_id="unrelated-one") for i in range(2000)])
        session.execute(RevisionDerivative.__table__.insert(), [dict(revision_id="unrelated-bindings",
            kind="thumbnail", page_index=i, spec_key="v1", derivative_id="unrelated-thumb")
            for i in range(2000)])
    after, evidence = Probe(library.database).measure("legacy_after_bindings",
        lambda: library.detail(1, page_limit=1))
    assert after["pages"] == before["pages"]
    assert evidence["vm_steps"] <= baseline["vm_steps"] * 1.35 + 500
    plans = [detail for statement in evidence["statements"] for *_, detail in statement["plan"]]
    assert not any("SCAN revision_assets" in detail or "SCAN revision_derivatives" in detail for detail in plans)
