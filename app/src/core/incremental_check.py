"""Automatic new-work discovery; durable overlapping frontiers, no catalogue sweep.

Pixiv's newest-first artist lists are read until two consecutive pages fall
outside the seven-day overlap. IDs also protect newly published/backdated works.
This cannot detect arbitrarily old works becoming public deep in remote history;
that remains the explicit historical-import/maintenance workflow.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .models import ApplicationState, ArchiveIntent, Artist, Operation, OperationItem, Work, utcnow
from .tasks import ItemResult, ItemSpec, PlanBatch

MODE = "incremental-v1"
OVERLAP = timedelta(days=7)
ANCHORS = 64


def automatic(session, operation_id):
    operation = session.get(Operation, operation_id)
    return operation is not None and operation.kind == "check" and operation.source == "automatic"


def state_key(artist_id, work_type):
    return f"incremental_check:{artist_id}:{work_type}"


def _date(value):
    try:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        if value is not None and not isinstance(value, datetime):
            return None
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value
    except (ValueError, TypeError):
        return None


def _number(value):
    return int(value) if str(value).isdigit() else 0


def _boundary(session, artist, kind):
    stored = session.get(ApplicationState, state_key(artist.id, kind))
    if stored and isinstance(stored.value, dict) and stored.value.get("mode") == MODE:
        return dict(stored.value)
    bootstrap = session.get(ApplicationState, state_key(artist.id, kind) + ":bootstrap")
    if bootstrap and isinstance(bootstrap.value, dict):
        return dict(bootstrap.value)
    # Index-backed, bounded near-head records, rather than reserving every work.
    types = ("manga",) if kind == "manga" else ("illust", "ugoira")
    # The artist/time index has no type column. Filter only after taking its
    # bounded head, otherwise a rare/empty manga list would scan all local art.
    head = session.execute(select(Work.pixiv_illust_id, Work.posted_at, Work.type).where(
        Work.artist_id == artist.id).order_by(Work.posted_at.desc(), Work.id.desc()).limit(ANCHORS))
    recent = [row for row in head if row.type in types]
    dates = [row.posted_at for row in recent if row.posted_at]
    times = [date for date in (artist.last_checked_at, max(dates) if dates else None) if date]
    return {"mode": MODE, "checked_at": (min(times) if times else artist.added_at).isoformat(),
            "head_id": max((_number(row.pixiv_illust_id) for row in recent), default=0),
            "anchor_ids": [str(row.pixiv_illust_id) for row in recent], "bootstrap": True}


def freeze_bootstrap(session, artist_id, kind):
    """Freeze before a captured legacy candidate can change local near-head data."""
    kind = "manga" if kind == "manga" else "illust"
    key = state_key(artist_id, kind) + ":bootstrap"
    if session.get(ApplicationState, state_key(artist_id, kind)) or session.get(ApplicationState, key):
        return
    artist = session.get(Artist, artist_id)
    if artist:
        session.add(ApplicationState(key=key, value=_boundary(session, artist, kind)))


def plan_batch(handler, parameters, context, cursor):
    from .task_planning import CHECK_ARTIST_BATCH
    ids = handler._artist_ids(parameters, context)
    offset = cursor.get("incremental_artist_offset", 0)
    # Two list items per artist; at most eight new projection items per commit.
    subset = ids[offset:offset + CHECK_ARTIST_BATCH]
    specs = [ItemSpec(f"incremental-page-{ident}-{kind}", "discover_page",
        {"artist_id": ident, "work_type": kind, "offset": 0, "incremental": MODE}, "checking", "artists")
        for ident in subset for kind in ("illust", "manga")]
    with handler.database.write() as session:
        operation = session.get(Operation, context.operation_id)
        if operation.result.get("incremental_check", {}).get("mode") != MODE:
            operation.result = {**operation.result, "incremental_check": {"mode": MODE,
                "artists_done": 0, "artists_total": len(ids), "new_works": 0, "scan_complete": not ids}}
    return PlanBatch(specs, {**cursor, "incremental_artist_offset": offset + len(subset)}, offset + len(subset) == len(ids))


def resume_legacy(session, operation):
    if operation.result.get("incremental_check", {}).get("mode") == MODE:
        return
    # Existing download children and captured work payloads are retained. Old
    # page items are skipped lazily by dispatch, without hydrating their JSON.
    operation.result = {**operation.result, "planning_complete": False,
                        "planning_cursor": {}, "inventory_failed": False, "inventory_complete": False}


def record_settlement(session, operation, item, result):
    if item.kind != "discover_page" or item.target.get("incremental") != MODE:
        return
    artists = dict(operation.result.get("incremental_artist_outcomes", {}))
    artist_key = str(item.target["artist_id"])
    kinds = dict(artists.get(artist_key, {}))
    kinds[item.target["work_type"]] = result.state == "succeeded"
    artists[artist_key] = kinds
    progress = dict(operation.result["incremental_check"])
    progress["artists_done"] = sum(len(types) == 2 for types in artists.values())
    progress["scan_complete"] = progress["artists_done"] == progress["artists_total"] and all(
        all(types.values()) for types in artists.values())
    operation.result = {**operation.result, "incremental_check": progress, "incremental_artist_outcomes": artists}


def _eligible(data, boundary):
    posted = _date(data.get("posted_at"))
    checked = _date(boundary.get("checked_at"))
    number = _number(data["illust_id"])
    anchors = [_number(ident) for ident in boundary.get("anchor_ids", []) if _number(ident)]
    # Timestamp and ID are alternative evidence, so equal-time works and a new
    # work mixed among older IDs are never lost on encountering one known ID.
    if number and boundary.get("head_id") and number > boundary["head_id"]:
        return True
    if posted is not None and checked is not None:
        return posted >= checked - OVERLAP
    if anchors:
        return number >= min(anchors) if number else True
    return True


def execute_page(handler, item, context):
    from .task_planning import _page, _remote_generation
    target = dict(item.target)
    with handler.database.read() as session:
        artist = session.get(Artist, target["artist_id"])
        if artist is None:
            return ItemResult("skipped", {"message": "画师记录不存在"})
        boundary = target.get("boundary") or _boundary(session, artist, target["work_type"])
        remote_id = artist.pixiv_user_id
    context.checkpoint()
    if boundary.get("bootstrap"):
        # A failed first run may already have registered newer works. Preserve
        # its original lower bound across subsequent operations, instead of
        # bootstrapping from those partial results and skipping the missing gap.
        with handler.database.write() as session:
            key = state_key(target["artist_id"], target["work_type"]) + ":bootstrap"
            if session.get(ApplicationState, key) is None:
                session.add(ApplicationState(key=key, value=boundary))
    if target.get("scan_finished"):
        with handler.database.write() as session:
            key = state_key(target["artist_id"], target["work_type"])
            value = {"mode": MODE, "checked_at": target["scan_started_at"],
                "head_id": target.get("head_id", boundary.get("head_id", 0)),
                "anchor_ids": target.get("head_ids", boundary.get("anchor_ids", []))}
            stored = session.get(ApplicationState, key)
            # Another successful operation must not move the frontier backwards.
            if stored is None:
                session.add(ApplicationState(key=key, value=value))
            elif not _date(stored.value.get("checked_at")) or _date(stored.value["checked_at"]) <= _date(value["checked_at"]):
                stored.value = value
            other = session.scalar(select(OperationItem.state).where(OperationItem.operation_id == context.operation_id,
                OperationItem.item_key == f"incremental-page-{target['artist_id']}-{'manga' if target['work_type'] == 'illust' else 'illust'}"))
            if other == "succeeded":
                session.get(Artist, target["artist_id"]).last_checked_at = utcnow()
        return ItemResult(detail={"message": "新增范围检查结束"})
    target.setdefault("scan_started_at", utcnow().isoformat())
    target["boundary"] = boundary
    generation = _remote_generation(handler)
    page = _page(handler, remote_id, target["work_type"], target["offset"], context.operation_id)
    data_items = page["items"]
    context.checkpoint()
    identities = [str(data["illust_id"]) for data in data_items]
    # Consecutive repeated pages with advancing offsets indicate a bad remote
    # cursor; fail without certifying coverage instead of endlessly paging.
    if identities and identities == target.get("previous_page_ids"):
        repeats = target.get("repeated_pages", 0) + 1
        if repeats >= 2:
            raise ValueError("主站分页重复，增量边界未推进")
    else:
        repeats = 0
    target.update(previous_page_ids=identities, repeated_pages=repeats)
    head = list(target.get("head_ids", []))
    for identity in identities:
        if len(head) < ANCHORS and identity not in head:
            head.append(identity)
    target["head_ids"] = head
    target["head_id"] = max([target.get("head_id", boundary.get("head_id", 0)), *map(_number, identities)])
    specs = []
    with handler.database.write() as session:
        known = set(session.scalars(select(Work.pixiv_illust_id).where(Work.pixiv_illust_id.in_(identities))))
        suppressed = set(session.scalars(select(ArchiveIntent.pixiv_illust_id).where(
            ArchiveIntent.pixiv_illust_id.in_(identities), ArchiveIntent.mode == "suppressed")))
        for data in data_items:
            identity = str(data["illust_id"])
            if identity in known or identity in suppressed or not _eligible(data, boundary):
                continue
            specs.append(ItemSpec(f"incremental-work-{identity}", "discover_work", {"artist_id": target["artist_id"],
                "data": data, "incremental": MODE, "remote_generation": generation}, "checking", "works"))
            known.add(identity)
        outside = bool(data_items) and not repeats and all(not _eligible(data, boundary) for data in data_items)
        target["old_pages"] = target.get("old_pages", 0) + 1 if outside else 0
        next_offset = page.get("next_offset")
        if next_offset is not None and (type(next_offset) is not int or next_offset <= target["offset"]):
            raise ValueError("主站分页未前进")
        target["scan_finished"] = next_offset is None or target["old_pages"] >= 2
        if next_offset is not None:
            # Re-read a few rows at page joins: deletion while listing can shift
            # unvisited records backwards. De-duplication happens in the writer.
            distance = next_offset - target["offset"]
            target["offset"] = next_offset - min(3, distance // 4)
        context.queue.materialize_in_transaction(session, context.operation_id, specs)
        stored = session.get(OperationItem, context.item_id)
        stored.target = target
        stored.depends_on = list(dict.fromkeys(spec.key for spec in specs))
    return ItemResult("yielded", {"message": "新增候选和分页进度已保存"})


def skip_known_work(handler, item):
    identity = str(item.target["data"]["illust_id"])
    with handler.database.read() as session:
        if session.scalar(select(Work.id).where(Work.pixiv_illust_id == identity)) is not None:
            return ItemResult("skipped", {"message": "已有存档，自动检查仅处理新增"})
        intent = session.get(ArchiveIntent, identity)
        if intent and intent.mode == "suppressed":
            return ItemResult("skipped", {"message": "用户已阻止重新保存"})
    return None


def registered(session, context):
    """Called inside the new-work transaction, including its download handoff."""
    operation = session.get(Operation, context.operation_id)
    progress = dict(operation.result.get("incremental_check", {}))
    if progress.get("mode") == MODE:
        progress["new_works"] += 1
        operation.result = {**operation.result, "incremental_check": progress}
