"""Bounded, persisted catalogue and remote-page planning steps."""
from __future__ import annotations

from itertools import islice
import hashlib
import json
from sqlalchemy import func, select
from .models import ArchiveIntent, Artist, Asset, Derivative, Operation, OperationItem, RevisionAsset, RevisionDerivative, Work, WorkRevision, new_id, utcnow
from .tasks import ItemResult, ItemSpec, PlanBatch, safe_failure

WORK_BATCH = 4
ARTIST_BATCH = 32


class _ScopedContext:
    def __init__(self, context):
        self._context = context
    def __getattr__(self, name):
        return getattr(self._context, name)
    def checkpoint_parameters(self, parameters):
        # Per-work legacy planner must not replace the frozen parent scope.
        self._context.checkpoint()
    def report(self, *args):
        self._context.checkpoint()


def _reserve_observations(handler, ids, context, cursor):
    if (handler.bookmarks or handler.source_status) and not cursor.get("observations_complete"):
        with handler.database.read() as session:
            statement = select(Work.id, Work.pixiv_illust_id, ArchiveIntent.revision).join(ArchiveIntent,
                ArchiveIntent.pixiv_illust_id == Work.pixiv_illust_id).where(Work.artist_id.in_(ids), ArchiveIntent.mode == "keep")
            if "observation_ceiling" not in cursor:
                cursor["observation_ceiling"] = session.scalar(select(func.max(Work.id)).where(Work.artist_id.in_(ids))) or 0
            rows = list(session.execute(statement.where(Work.id > cursor.get("observation_after", 0),
                Work.id <= cursor["observation_ceiling"]).order_by(Work.id).limit(65)))
        account = handler._bookmark_account()
        source_account = handler.source_status.account_context() if handler.source_status else account
        with handler.database.read() as session:
            existing_keys = set(session.scalars(select(OperationItem.item_key).where(OperationItem.operation_id == context.operation_id,
                OperationItem.item_key.in_([f"observation-{row.id}" for row in rows[:64]]))))
        rows_to_reserve = [row for row in rows[:64] if f"observation-{row.id}" not in existing_keys]
        work_ids = [row.id for row in rows_to_reserve]
        bookmarks = handler.bookmarks.begin_observations(work_ids) if handler.bookmarks else {}
        sources = handler.source_status.begin_many("work", work_ids, source_account) if handler.source_status else {}
        observations = [ItemSpec(f"observation-{row.id}", "observation", {"work_id": row.id,
            "bookmark_version": bookmarks.get(row.id), "source_version": sources.get(row.id),
            "revision": row.revision, "account": account, "source_account": source_account}, "inventory", "works") for row in rows_to_reserve]
        with handler.database.write() as session:
            context.queue.materialize_in_transaction(session, context.operation_id, observations)
            for spec in observations:
                record = session.scalar(select(OperationItem).where(OperationItem.operation_id == context.operation_id, OperationItem.item_key == spec.key))
                record.state, record.result = "skipped", {"message": "检查开始前已固定核对世代"}
        cursor["observation_after"] = rows[min(63, len(rows)-1)].id if rows else cursor.get("observation_after", 0)
        cursor["observations_complete"] = len(rows) <= 64
        return PlanBatch([], cursor, False)
    return None


def plan_batch(handler, parameters, context, cursor):
    cursor = dict(cursor)
    if handler.kind == "reconcile":
        return _reconcile_plan_batch(handler, parameters, context, cursor)
    if handler.kind == "check":
        ids = handler._artist_ids(parameters, context)
        reserved = _reserve_observations(handler, ids, context, cursor)
        if reserved is not None:
            return reserved
        offset = cursor.get("artist_offset", 0)
        subset = ids[offset:offset + ARTIST_BATCH]
        specs = [ItemSpec(f"discover-page-{artist_id}-illust-0", "discover_page",
            {"artist_id": artist_id, "work_type": "illust", "offset": 0}, "checking", "pages") for artist_id in subset]
        offset += len(subset)
        context.report("inventory", offset, len(ids), "artists", "已固定画师检查范围")
        return PlanBatch(specs, {**cursor, "artist_offset": offset}, offset == len(ids))
    if handler.kind not in ("reconcile", "verify", "repair", "backfill", "thumbnails", "metadata", "bookmarks"):
        return PlanBatch(handler.plan(parameters, context))
    if handler.kind in ("metadata", "bookmarks") and "work_ids" not in parameters:
        ids = handler._artist_ids(parameters, context)
        if handler.kind == "bookmarks":
            reserved = _reserve_observations(handler, ids, context, cursor)
            if reserved is not None:
                return reserved
        offset = cursor.get("artist_offset", 0)
        subset = ids[offset:offset + ARTIST_BATCH]
        specs = []
        for artist_id in subset:
            if handler.kind == "bookmarks":
                specs.append(ItemSpec(f"bookmark-page-{artist_id}-illust-0", "bookmark_page",
                    {"artist_id": artist_id, "work_type": "illust", "offset": 0}, "bookmarks", "pages"))
            else:
                specs.append(ItemSpec(f"metadata-artist-{artist_id}", "metadata_artist", {"artist_id": artist_id}, "metadata", "artists"))
        offset += len(subset)
        if handler.kind == "bookmarks":
            return PlanBatch(specs, {**cursor, "artist_offset": offset}, offset == len(ids))
        parameters = {**parameters, "artist_ids": ids}
        cursor["artist_offset"] = offset
        if offset < len(ids):
            return PlanBatch(specs, cursor, False)
        if not cursor.get("artists_complete"):
            cursor["artists_complete"] = True
            return PlanBatch(specs, cursor, False)
    with handler.database.read() as session:
        statement = select(Work.id).where(~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed")))
        if "work_ids" in parameters:
            statement = statement.where(Work.id.in_(parameters["work_ids"]))
        elif "artist_ids" in parameters:
            statement = statement.where(Work.artist_id.in_(parameters["artist_ids"]))
        if "ceiling" not in cursor:
            cursor["ceiling"] = session.scalar(select(func.max(Work.id)).where(Work.id.in_(statement))) or 0
        ids = list(session.scalars(statement.where(Work.id > cursor.get("work_after", 0),
            Work.id <= cursor["ceiling"]).order_by(Work.id).limit(WORK_BATCH + 1)))
    specs = []
    for work_id in ids[:WORK_BATCH]:
        context.checkpoint()
        if handler.kind == "reconcile":
            if handler.profiles and "work_ids" in parameters:
                with handler.database.read() as session:
                    artist_id = session.scalar(select(Work.artist_id).where(Work.id == work_id))
                if artist_id is not None:
                    specs.append(ItemSpec(f"profile-{artist_id}", "metadata_artist", {"artist_id": artist_id}, "checking", "artists"))
            specs.extend(handler.plan({**parameters, "work_ids": [work_id], "started_work_ids": [work_id], "started_artist_ids": []}, _ScopedContext(context)))
        elif handler.kind == "metadata":
            specs.append(ItemSpec(f"metadata-work-{work_id}", "metadata_work", {"work_id": work_id}, "captions", "works"))
        else:
            try:
                specs.extend(handler._plan_work(work_id, parameters, context))
            except Exception as exc:
                specs.append(ItemSpec(f"work-error-{work_id}", "error", {**safe_failure(exc), "work_id": work_id}, "checking", "works"))
        with handler.database.write() as session:
            context.queue.materialize_in_transaction(session, context.operation_id, specs)
        cursor["work_after"] = work_id
    cursor["work_done"] = cursor.get("work_done", 0) + len(ids[:WORK_BATCH])
    context.report("inventory", cursor["work_done"], None, "works", "正在分批整理作品清单")
    complete = len(ids) <= WORK_BATCH
    if complete and handler.kind == "reconcile" and handler.profiles and "work_ids" not in parameters:
        with handler.database.read() as session:
            artists = select(Artist.id).where(Artist.id > cursor.get("profile_after", 0)).order_by(Artist.id).limit(ARTIST_BATCH + 1)
            if "artist_ids" in parameters:
                artists = artists.where(Artist.id.in_(parameters["artist_ids"]))
            artist_ids = list(session.scalars(artists))
        for artist_id in artist_ids[:ARTIST_BATCH]:
            specs.append(ItemSpec(f"profile-{artist_id}", "metadata_artist", {"artist_id": artist_id}, "checking", "artists"))
            cursor["profile_after"] = artist_id
        complete = len(artist_ids) <= ARTIST_BATCH
    return PlanBatch(specs, cursor, complete)


def _reconcile_plan_batch(handler, parameters, context, cursor):
    """Inventory at most 64 pages; chain bounded dependency gates per work."""
    from .revisions import bind_asset, bind_derivative, latest_revision, project_revision, refresh_revision, signature
    context.checkpoint()
    specs = []
    with handler.database.write() as session:
        if "ceiling" not in cursor:
            scope = select(Work.id)
            if "work_ids" in parameters:
                scope = scope.where(Work.id.in_(parameters["work_ids"]))
            elif "artist_ids" in parameters:
                scope = scope.where(Work.artist_id.in_(parameters["artist_ids"]))
            cursor["ceiling"] = session.scalar(select(func.max(Work.id)).where(Work.id.in_(scope))) or 0
            cursor["profile_ceiling"] = session.scalar(select(func.max(Artist.id))) or 0
            # New manifests check homepages before work snapshots. Older
            # persisted cursors retain their original order and completed keys.
            cursor["profiles_first"] = True
        if cursor.get("profiles_first") and not cursor.get("profiles_complete"):
            if handler.source_status or handler.profiles:
                artists = select(Artist.id).where(Artist.id > cursor.get("profile_after", 0), Artist.id <= cursor["profile_ceiling"])
                if "work_ids" in parameters:
                    selected = select(Work.artist_id).join(ArchiveIntent, ArchiveIntent.pixiv_illust_id == Work.pixiv_illust_id).where(
                        Work.id.in_(parameters["work_ids"]), Work.id <= cursor["ceiling"], ArchiveIntent.mode == "keep")
                    artists = artists.where(Artist.id.in_(selected))
                elif "artist_ids" in parameters:
                    artists = artists.where(Artist.id.in_(parameters["artist_ids"]))
                ids = list(session.scalars(artists.order_by(Artist.id).limit(ARTIST_BATCH + 1)))
                for artist_id in ids[:ARTIST_BATCH]:
                    specs.append(ItemSpec(f"profile-{artist_id}", "metadata_artist", {"artist_id": artist_id}, "checking", "artists"))
                    cursor["profile_after"] = artist_id
                cursor["profiles_complete"] = len(ids) <= ARTIST_BATCH
                return PlanBatch(specs, cursor, False)
            cursor["profiles_complete"] = True
        inventory = dict(cursor.get("work_inventory", {}))
        work_id = inventory.get("work_id")
        if work_id is None:
            scope = select(Work.id).join(ArchiveIntent, ArchiveIntent.pixiv_illust_id == Work.pixiv_illust_id).where(
                Work.id > cursor.get("work_after", 0), Work.id <= cursor["ceiling"], ArchiveIntent.mode == "keep")
            if "work_ids" in parameters:
                scope = scope.where(Work.id.in_(parameters["work_ids"]))
            elif "artist_ids" in parameters:
                scope = scope.where(Work.artist_id.in_(parameters["artist_ids"]))
            work_id = session.scalar(scope.order_by(Work.id).limit(1))
        if work_id is None:
            if not cursor.get("profiles_first") and (handler.source_status or handler.profiles) and "work_ids" not in parameters:
                artists = select(Artist.id).where(Artist.id > cursor.get("profile_after", 0), Artist.id <= cursor["profile_ceiling"])
                if "artist_ids" in parameters:
                    artists = artists.where(Artist.id.in_(parameters["artist_ids"]))
                ids = list(session.scalars(artists.order_by(Artist.id).limit(33)))
                for artist_id in ids[:32]:
                    specs.append(ItemSpec(f"profile-{artist_id}", "metadata_artist", {"artist_id": artist_id}, "checking", "artists"))
                    cursor["profile_after"] = artist_id
                return PlanBatch(specs, cursor, len(ids) <= 32)
            return PlanBatch([], cursor, True)
        work = session.get(Work, work_id)
        intent = session.get(ArchiveIntent, work.pixiv_illust_id) if work else None
        if work is None or intent is None or intent.mode != "keep":
            cursor.update(work_after=work_id)
            cursor.pop("work_inventory", None)
            return PlanBatch([], cursor, False)
        revision = session.get(WorkRevision, inventory.get("revision_id")) if inventory.get("revision_id") else latest_revision(session, work_id)
        if revision is None:
            # Isolated native fixtures may not have passed the upgrade gate.
            # Bind their legacy records in the same bounded page batches.
            snapshot = dict(title=work.title, caption=work.caption, caption_state=work.caption_state,
                type=work.type, page_count=work.page_count, tags=sorted(tag.tag for tag in work.tags),
                source_metadata=dict(work.source_metadata or {}))
            revision = WorkRevision(id=new_id(), work_id=work.id, ordinal=1,
                source_signature=signature(snapshot), change_kind="legacy", **snapshot)
            session.add(revision)
            session.flush()
            work.latest_revision_id = revision.id
            inventory["legacy_binding"] = True
        inventory.update(work_id=work_id, revision_id=revision.id)
        work_view = project_revision(work, revision)
        target = {"work_id": work_id, "revision": intent.revision, "work_revision_id": revision.id}
        if not cursor.get("profiles_first") and (handler.source_status or handler.profiles) and "work_ids" in parameters:
            specs.append(ItemSpec(f"profile-{work.artist_id}", "metadata_artist", {"artist_id": work.artist_id}, "checking", "artists"))
        if inventory.get("stage", "pages") == "pages":
            first = inventory.get("page_after", 0)
            end = min(first + 64, revision.page_count)
            if inventory.get("legacy_binding"):
                legacy = list(session.scalars(select(Asset).where(Asset.work_id == work_id,
                    Asset.revision_id.is_(None), Asset.page_index >= first, Asset.page_index < end)))
                for asset in legacy:
                    asset.revision_id = revision.id
                    bind_asset(session, revision, asset)
            bound = list(session.execute(select(RevisionAsset, Asset).join(Asset, Asset.id == RevisionAsset.asset_id).where(
                RevisionAsset.revision_id == revision.id, RevisionAsset.page_index >= first, RevisionAsset.page_index < end)))
            known = {(binding.role, binding.page_index): asset for binding, asset in bound}
            expected = [("ugoira_zip", 0), ("ugoira_timing", 0)] if revision.type == "ugoira" else [("image", page) for page in range(first, end)]
            for role, page in expected:
                if (role, page) not in known:
                    urls = (revision.source_metadata or {}).get("urls", [])
                    asset = Asset(work_id=work_id, revision_id=revision.id, role=role, page_index=page, state="missing",
                        source_url=urls[page].get("original") if role == "image" and page < len(urls) else None)
                    session.add(asset)
                    bind_asset(session, revision, asset)
                    known[(role, page)] = asset
            keys = []
            for asset in known.values():
                key = f"inspect-asset-{asset.id}"
                specs.append(ItemSpec(key, "reconcile_asset", {**target, "asset_id": asset.id}, "verifying", "assets"))
                keys.append(key)
            previous = inventory.get("gate")
            if previous:
                keys.append(previous)
            gate = f"inventory-work-{work_id}-pages-{first}"
            specs.append(ItemSpec(gate, "checked", target, "inventory", "items", tuple(keys)))
            inventory.update(gate=gate, page_after=end)
            if end == revision.page_count:
                inventory.update(stage="derivatives", derivative_after="")
        else:
            legacy = []
            if inventory.get("legacy_binding"):
                legacy = list(session.scalars(select(Derivative).where(Derivative.work_id == work_id,
                    Derivative.revision_id.is_(None), Derivative.id > inventory.get("derivative_after", "")).order_by(Derivative.id).limit(65)))
                for record in legacy[:64]:
                    record.revision_id = revision.id
                    bind_derivative(session, revision, record)
            records = list(session.scalars(select(Derivative).join(RevisionDerivative, RevisionDerivative.derivative_id == Derivative.id).where(
                RevisionDerivative.revision_id == revision.id, Derivative.id > inventory.get("derivative_after", "")).order_by(Derivative.id).limit(65)))
            keys = [inventory["gate"]]
            for record in records[:64]:
                key = f"inspect-derivative-{record.id}"
                specs.append(ItemSpec(key, "reconcile_derivative", {**target, "record_id": record.id}, "verifying", "previews"))
                keys.append(key)
            gate = f"inventory-work-{work_id}-derivatives-{inventory.get('derivative_after', '')}"
            specs.append(ItemSpec(gate, "checked", target, "inventory", "items", tuple(keys)))
            inventory["gate"] = gate
            if records:
                inventory["derivative_after"] = records[min(63, len(records)-1)].id
            if len(records) <= 64 and len(legacy) <= 64:
                if revision.type == "ugoira":
                    pair = f"inspect-pair-{work_id}"
                    specs.append(ItemSpec(pair, "reconcile_pair", target, "verifying", "works", (gate,)))
                    gate = pair
                snapshot = f"snapshot-{work_id}"
                specs.append(ItemSpec(snapshot, "reconcile_snapshot", target, "checking", "works"))
                specs.append(ItemSpec(f"prepare-{work_id}", "reconcile_prepare", {**target, "snapshot_key": snapshot},
                    "reconciling", "works", (gate, snapshot)))
                if inventory.get("legacy_binding"):
                    refresh_revision(session, revision)
                cursor.update(work_after=work_id, work_done=cursor.get("work_done", 0) + 1)
                inventory = {}
    cursor["work_inventory"] = inventory
    context.report("inventory", cursor.get("work_done", 0), None, "works", "正在分批整理作品与图片清单")
    return PlanBatch(specs, cursor, False)


def _page(handler, remote_id, work_type, offset, operation_id):
    method = getattr(handler.client, "get_artist_illusts_page", None)
    if callable(method):
        return method(remote_id, work_type, offset)
    # Small injected clients retain their iterator between scheduling steps.
    # Production has a durable numeric page cursor and never enters this seam.
    if work_type != "illust":
        return {"items": [], "next_offset": None}
    iterators = getattr(handler, "_injected_iterators", None)
    if iterators is None:
        iterators = handler._injected_iterators = {}
    key = (operation_id, remote_id)
    existing = iterators.get(key)
    if existing is None or existing[1] != offset:
        iterator = iter(handler.client.get_all_artist_illusts(remote_id))
        # Recovery is supported for deterministic injected clients only.
        for _ in range(offset):
            next(iterator, None)
    else:
        iterator = existing[0]
    try:
        value = next(iterator)
    except StopIteration:
        iterators.pop(key, None)
        return {"items": [], "next_offset": None}
    iterators[key] = (iterator, offset + 1)
    return {"items": [value], "next_offset": offset + 1}


def execute(handler, item, context):
    if item.kind == "bookmark_artist":
        # Existing 0002 items keep their identity and dependency barrier while
        # their cursor advances by at most one catalogue batch or remote page.
        target = dict(item.target)
        cursor = dict(target.get("legacy_bookmark_cursor", {}))
        if cursor.get("stage") == "finalize":
            return execute(handler, ItemSpec(item.key, "bookmark_finalize", target, item.phase, item.unit), context)
        reserved = _reserve_observations(handler, [target["artist_id"]], context, cursor)
        dependencies = tuple(target.get("legacy_bookmark_dependencies", item.depends_on))
        specs = []
        if reserved is not None:
            cursor = reserved.cursor
        else:
            with handler.database.read() as session:
                artist = session.get(Artist, target["artist_id"])
                if artist is None:
                    return ItemResult("skipped", {"message": "画师记录不存在"})
                remote_id = artist.pixiv_user_id
            context.checkpoint()
            work_type, offset = cursor.get("work_type", "illust"), cursor.get("offset", 0)
            page = _page(handler, remote_id, work_type, offset, context.operation_id)
            for data in page["items"]:
                fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
                specs.append(ItemSpec(f"bookmark-work-{target['artist_id']}-{data['illust_id']}-{fingerprint}",
                    "discover_work", {"artist_id": target["artist_id"], "data": data, "bookmark_only": True}, "checking", "works"))
            next_offset = page.get("next_offset")
            if next_offset is None and work_type == "illust":
                cursor.update(work_type="manga", offset=0)
            elif next_offset is None:
                cursor["stage"] = "finalize"
            else:
                if type(next_offset) is not int or next_offset <= offset:
                    raise ValueError("主站分页未前进")
                cursor.update(work_type=work_type, offset=next_offset)
        with handler.database.write() as session:
            context.queue.materialize_in_transaction(session, context.operation_id, specs)
            stored = session.get(OperationItem, context.item_id)
            stored.target = {**target, "legacy_bookmark_cursor": cursor,
                "legacy_bookmark_dependencies": list(dependencies)}
            stored.depends_on = list(dict.fromkeys([*dependencies, *[spec.key for spec in specs]]))
        return ItemResult("yielded", {"message": "已保存收藏核对游标，等待下一步"})
    if item.kind == "bookmark_finalize":
        with handler.database.read() as session:
            missing = session.scalar(select(func.count()).select_from(OperationItem).join(Work,
                Work.id == OperationItem.target["work_id"].as_integer()).where(
                OperationItem.operation_id == context.operation_id, OperationItem.kind == "observation",
                Work.artist_id == item.target["artist_id"],
                OperationItem.result["observed"].as_boolean().is_not(True)))
        return ItemResult("failed" if missing else "succeeded", {"unconfirmed_works": missing,
            "message": "未出现在本次主站列表中的作品已保留收藏缓存" if missing else "所选画师收藏状态核对结束"})
    if item.kind in ("discover_page", "bookmark_page"):
        target = item.target
        with handler.database.read() as session:
            artist = session.get(Artist, target["artist_id"])
            if artist is None:
                return ItemResult("skipped", {"message": "画师记录不存在"})
            remote_id = artist.pixiv_user_id
        context.checkpoint()
        if item.kind == "discover_page" and target["work_type"] == "illust" and target["offset"] == 0 and handler.profiles:
            from datetime import timedelta
            if artist.profile_refreshed_at is None or artist.profile_refreshed_at < utcnow() - timedelta(days=1):
                with handler.database.write() as session:
                    context.queue.materialize_in_transaction(session, context.operation_id,
                        [ItemSpec(f"profile-{artist.id}", "metadata_artist", {"artist_id": artist.id}, "checking", "artists")])
        page = _page(handler, remote_id, target["work_type"], target["offset"], context.operation_id)
        specs = []
        for data in page["items"]:
            key = str(data["illust_id"]) + "-" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
            specs.append(ItemSpec(f"{'bookmark' if item.kind == 'bookmark_page' else 'discover'}-work-{target['artist_id']}-{key}",
                "discover_work", {"artist_id": target["artist_id"], "data": data, "bookmark_only": item.kind == "bookmark_page"}, "checking", "works"))
        next_offset = page.get("next_offset")
        next_type = target["work_type"]
        if next_offset is None and next_type == "illust":
            next_type, next_offset = "manga", 0
        if next_offset is not None:
            if next_type == target["work_type"] and next_offset <= target["offset"]:
                raise ValueError("主站分页未前进")
            prefix = "bookmark-page" if item.kind == "bookmark_page" else "discover-page"
            specs.append(ItemSpec(f"{prefix}-{target['artist_id']}-{next_type}-{next_offset}", item.kind,
                {**target, "offset": next_offset, "work_type": next_type}, item.phase, item.unit))
        if next_offset is None and item.kind == "bookmark_page":
            dependencies = tuple([item.key, *[spec.key for spec in specs if spec.kind == "discover_work"]])
            specs.append(ItemSpec(f"bookmark-finalize-{target['artist_id']}", "bookmark_finalize", {"artist_id": target["artist_id"]},
                "bookmarks", "artists", dependencies))
        with handler.database.write() as session:
            context.queue.materialize_in_transaction(session, context.operation_id, specs)
            if next_offset is None and item.kind == "discover_page":
                session.get(Artist, target["artist_id"]).last_checked_at = utcnow()
        return ItemResult(detail={"page_items": len(page["items"])})
    data, artist_id = item.target["data"], item.target["artist_id"]
    remote_id = str(data["illust_id"])
    account = handler._bookmark_account()
    with handler.database.read() as session:
        existing = session.scalar(select(Work).where(Work.pixiv_illust_id == remote_id))
        work_id = existing.id if existing else None
        intent = session.get(ArchiveIntent, remote_id)
        deletion_revision = intent.revision if intent else None
    if item.target.get("bookmark_only") and (not work_id or existing.artist_id != artist_id):
        return ItemResult("skipped")
    with handler.database.read() as session:
        observation = session.scalar(select(OperationItem).where(OperationItem.operation_id == context.operation_id,
            OperationItem.item_key == f"observation-{work_id}")) if work_id else None
        reserved = observation.target if observation else {}
    versions = {work_id: reserved.get("bookmark_version", 0)}
    source_account = reserved.get("source_account", handler.source_status.account_context() if handler.source_status else account)
    source_version = reserved.get("source_version")
    account = reserved.get("account", account)
    if observation is not None:
        deletion_revision = reserved["revision"]
    context.checkpoint()
    if item.target.get("bookmark_only"):
        if observation is not None:
            with handler.database.write() as session:
                record = session.get(OperationItem, observation.id)
                record.result = {**record.result, "observed": True}
        if account is None or type(data.get("is_bookmarked")) is not bool:
            return ItemResult("failed", {"unconfirmed_works": 1, "message": "未取得明确的收藏事实，已有缓存保留"})
        applied = handler._apply_bookmark(work_id, data, versions.get(work_id), account)
        return ItemResult("succeeded" if applied else "skipped", {"confirmed_works": int(applied)})
    metadata = metadata_error = None
    if data.get("type") == "ugoira":
        with handler.database.read() as session:
            deferred = existing and (existing.source_metadata or {}).get("original_backfill_requires_selection")
            needs_metadata = existing is None or bool(existing and data.get("urls", []) != (existing.source_metadata or {}).get("urls", [])) or bool(existing and (existing.source_metadata or {}).get("source_updates")) or bool(existing and session.scalar(select(Asset.id).where(Asset.work_id == existing.id, Asset.state.in_(("missing", "failed", "invalid", "mismatch", "source_changed"))).limit(1)))
        if not needs_metadata or deferred:
            metadata = None
        else:
            try:
                metadata = handler.client.get_ugoira_metadata(remote_id)
            except Exception as exc:
                metadata_error = safe_failure(exc)
    work_id = handler._save_remote_work(artist_id, data, context=context, metadata=metadata, metadata_error=metadata_error,
        expected_work_id=work_id, expected_revision=deletion_revision, expected_source_version=source_version)
    if work_id and source_version is not None:
        handler.source_status.apply_available("work", work_id, data, source_version, source_account)
    handler._apply_bookmark(work_id, data, versions.get(work_id, 0), account)
    return ItemResult()
