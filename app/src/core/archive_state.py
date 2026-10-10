"""Bounded, database-only work state shared by gallery, detail, and feed."""
from datetime import datetime, timezone

from sqlalchemy import Integer, String, and_, case, cast, func, literal, or_, select, true, union, union_all
from sqlalchemy.orm import aliased

from .catalogue_revision import catalogue_revision
from .models import ApplicationState, Asset, Derivative, Operation, OperationItem, Work, RevisionAsset, RevisionDerivative
from .task_indexes import task_target

ACTIVE = ("queued", "planning", "ready", "running", "interrupted")
LOCAL_KINDS = ("check", "metadata", "repair", "verify", "backfill", "thumbnails", "reconcile", "compare")


def _operation_window(ids):
    # Fixed identity expressions match the additive indexes. Two composite
    # indexes also provide the work-only left prefix; no extra work index.
    scope = func.json_each(Operation.parameters, "$.work_ids").table_valued("value").alias("scope")
    # Only identity-free items and active, entirely unplanned operations can
    # contribute the scope fallback below. Use the existing partial target
    # index before expanding JSON; modern unrelated history needs no expansion.
    # Bound the partial index's leading operation key, otherwise SQLite may
    # prefer a broader work/record NULL lookup before filtering the asset key.
    identity_free = select(OperationItem.operation_id).where(
        OperationItem.operation_id.in_(select(Operation.id)),
        task_target(OperationItem.target, "work_id").is_(None),
        task_target(OperationItem.target, "asset_id").is_(None),
        task_target(OperationItem.target, "record_id").is_(None)).distinct()
    any_item = select(OperationItem.id).where(OperationItem.operation_id == Operation.id).correlate(Operation).exists()
    scope_eligible = union(identity_free, select(Operation.id).where(
        Operation.state.in_(ACTIVE), ~any_item)).cte("scope_eligible").prefix_with("MATERIALIZED")
    scoped_operation = select(Operation.id).join(scope_eligible,
        scope_eligible.c.operation_id == Operation.id).join(scope, true()).where(
        cast(scope.c.value, Integer).in_(ids), Operation.kind.in_(LOCAL_KINDS)).distinct().cte("scoped_operations").prefix_with("MATERIALIZED")
    explicit_work = task_target(OperationItem.target, "work_id")
    asset_id = task_target(OperationItem.target, "asset_id")
    record_id = task_target(OperationItem.target, "record_id")
    selected_ids = union(
        select(OperationItem.id).where(explicit_work.in_(ids)),
        select(OperationItem.id).where(explicit_work.is_(None),
            asset_id.in_(select(Asset.id).where(Asset.work_id.in_(ids)))),
        select(OperationItem.id).where(explicit_work.is_(None),
            record_id.in_(select(Derivative.id).where(Derivative.work_id.in_(ids)))),
    ).cte("selected_item_ids").prefix_with("MATERIALIZED")
    items = select(OperationItem.id, OperationItem.operation_id, OperationItem.kind,
        OperationItem.state, OperationItem.position, explicit_work.label("work_id"),
        asset_id.label("asset_id"), record_id.label("record_id"),
        OperationItem.result["message"].as_string().label("reason")).join(
            selected_ids, selected_ids.c.id == OperationItem.id).cte("selected_items").prefix_with("MATERIALIZED")
    asset, derivative = aliased(Asset), aliased(Derivative)
    target_work = func.coalesce(items.c.work_id, asset.work_id, derivative.work_id)
    # INTEGER affinity lets SQLite build the (work_id, rank) lookup used by the
    # outer Works join; the selection predicate retains the original JSON type.
    direct = select(cast(target_work, Integer).label("work_id"), Operation.id.label("operation_id"),
        Operation.state.label("operation_state"), Operation.created_at.label("created_at"),
        Operation.updated_at.label("updated_at"), items.c.state.label("item_state"),
        items.c.reason, items.c.position,
        case((or_(items.c.asset_id.is_not(None), items.c.kind == "ugoira_pair"), "original"),
             (or_(items.c.record_id.is_not(None), Operation.kind == "thumbnails",
                  items.c.kind.in_(("gif", "thumbnail", "verify_derivative"))), "local"),
             else_="work").label("concern")).select_from(items).join(Operation, Operation.id == items.c.operation_id).outerjoin(
        asset, and_(items.c.work_id.is_(None), asset.id == items.c.asset_id)).outerjoin(
        derivative, and_(items.c.work_id.is_(None), asset.work_id.is_(None), derivative.id == items.c.record_id)).where(
        target_work.in_(ids), Operation.kind.in_(LOCAL_KINDS))
    # The persisted selection resolves unplanned work and legacy error items
    # without an asset/derivative identity. Expand that selection inside SQL.
    # Every identity-free legacy item applies to the same persisted selection.
    # Select its winning item once per operation before expanding that selection;
    # ranking thousands of identical work candidates on every read is redundant.
    legacy_item = aliased(OperationItem)
    item_active = and_(Operation.state.in_(ACTIVE), legacy_item.state.in_(("pending", "running")))
    best_item = select(legacy_item.id).where(legacy_item.operation_id == Operation.id,
        task_target(legacy_item.target, "work_id").is_(None),
        task_target(legacy_item.target, "asset_id").is_(None),
        task_target(legacy_item.target, "record_id").is_(None)).order_by(
        case((and_(item_active, legacy_item.state == "running"), 0), (item_active, 1), else_=2),
        case((legacy_item.state.in_(("failed", "not_executed")), 0), else_=1),
        legacy_item.position).limit(1).correlate(Operation).scalar_subquery()
    chosen = select(Operation.id.label("id"), best_item.label("item_id")).where(
        Operation.id.in_(select(scoped_operation.c.id))).cte("scoped_items").prefix_with("MATERIALIZED")
    scoped = select(cast(scope.c.value, Integer).label("work_id"), Operation.id.label("operation_id"),
        Operation.state.label("operation_state"), Operation.created_at.label("created_at"),
        Operation.updated_at.label("updated_at"), func.coalesce(OperationItem.state, "pending").label("item_state"),
        OperationItem.result["message"].as_string().label("reason"),
        func.coalesce(OperationItem.position, 0).label("position"),
        case((Operation.kind == "thumbnails", "local"), else_="work").label("concern")).select_from(Operation).join(
        scope, true()).join(chosen, chosen.c.id == Operation.id).outerjoin(OperationItem, OperationItem.id == chosen.c.item_id).where(
        cast(scope.c.value, Integer).in_(ids), Operation.kind.in_(LOCAL_KINDS),
        or_(OperationItem.id.is_not(None), and_(Operation.state.in_(ACTIVE), ~any_item)))
    candidates = union_all(direct, scoped).subquery()
    active = and_(candidates.c.operation_state.in_(ACTIVE), candidates.c.item_state.in_(("pending", "running")))
    priority = case((and_(active, candidates.c.item_state == "running"), 0), (active, 1), else_=2)
    ranked = select(candidates, func.row_number().over(partition_by=candidates.c.work_id,
        order_by=(priority, candidates.c.created_at.desc(), candidates.c.updated_at.desc(),
                  case((candidates.c.item_state.in_(("failed", "not_executed")), 0), else_=1),
                  candidates.c.position, candidates.c.operation_id.desc())).label("rank")).subquery()
    return ranked


def _retry_time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def _source_update(work):
    updates = (work.source_metadata or {}).get("source_updates")
    pending = [value for value in updates.values() if isinstance(value, dict)] if isinstance(updates, dict) else []
    reason = next((value["reason"] for value in pending if isinstance(value.get("reason"), str)), None)
    changed = getattr(work, "latest_revision_id", None) != getattr(work, "default_revision_id", None)
    return {"pending": bool(pending) or changed, "reason": reason or ("新版尚未完整保存，继续显示现有存档" if changed else None)}


def _processing(row, ledger, *, original_gap, local_gap, source_pending):
    result = {"state": "idle", "operation_id": None, "next_retry_at": None, "reason": None}
    item_state, operation_state = row.item_state, row.operation_state
    if operation_state in ACTIVE and item_state in ("pending", "running"):
        result.update(state="running" if item_state == "running" else "queued", operation_id=row.operation_id)
        return result
    tracks = ledger.get("tracks")
    tracks = list(tracks.items()) if isinstance(tracks, dict) else [(
        "local" if ledger.get("kind") == "thumbnails" else "remote", ledger)]
    # The ledger schedules retries; it cannot make a finished failed operation
    # appear queued, or turn a readable first page into a complete original set.
    waiting, attention = [], []
    if original_gap or local_gap or source_pending:
        for name, track in tracks:
            if ((name == "remote" and not (original_gap or source_pending))
                    or (name == "local" and not local_gap)):
                continue
            if not isinstance(track, dict):
                continue
            if track.get("status") == "attention":
                attention.append(track)
                continue
            if track.get("status") != "waiting":
                continue
            retry = _retry_time(track.get("next_retry_at"))
            if retry is not None:
                waiting.append((retry, track))
    if waiting:
        retry, track = min(waiting, key=lambda entry: entry[0])
        reason = track.get("reason")
        result.update(state="waiting", operation_id=row.operation_id,
                      next_retry_at=retry.isoformat() + "Z", reason=reason if isinstance(reason, str) else None)
    elif attention:
        track = attention[0]
        reason = track.get("reason")
        result.update(state="attention", operation_id=row.operation_id,
                      reason=reason if isinstance(reason, str) else "此项未完成，请重试")
    elif item_state in ("failed", "not_executed") and (
            (original_gap or source_pending) if row.concern == "original" else local_gap if row.concern == "local"
            else original_gap or source_pending or local_gap):
        result.update(state="attention", operation_id=row.operation_id,
                      reason=row.reason if isinstance(row.reason, str) else "此项未完成，请重试")
    return result


def _recovery(ledger, processing, archive):
    if not ledger and processing["state"] == "idle":
        return None
    state = processing["state"]
    if state == "idle":
        state = "ready" if archive["state"] == "complete" else ledger.get("status", "ready")
        if state == "queued":
            state = "attention"
    elif state == "running":
        state = "queued"
    return {"status": state, "kind": ledger.get("kind"),
            "next_retry_at": processing["next_retry_at"], "reason": processing["reason"]}


def work_states(session, works):
    """One batch query, one output row per work, no filesystem or network I/O."""
    works = list(works)
    ids = [work.id for work in works]
    if not ids:
        return {}, catalogue_revision(session)
    legacy = select(Asset.work_id.label("work_id"), Asset.role, Asset.page_index, Asset.state, Asset.relative_path).join(
        Work, Work.id == Asset.work_id).where(Work.id.in_(ids), Work.default_revision_id.is_(None), Asset.revision_id.is_(None))
    bound = select(Work.id.label("work_id"), RevisionAsset.role, RevisionAsset.page_index, Asset.state, Asset.relative_path).select_from(
        Work).join(RevisionAsset, RevisionAsset.revision_id == Work.default_revision_id).join(
        Asset, Asset.id == RevisionAsset.asset_id).where(Work.id.in_(ids))
    current = union_all(legacy, bound).subquery()
    required = or_(and_(Work.type != "ugoira", current.c.role == "image", current.c.page_index < Work.page_count),
                   and_(Work.type == "ugoira", current.c.role.in_(("ugoira_zip", "ugoira_timing")), current.c.page_index == 0))
    verified = and_(current.c.state == "verified", func.length(current.c.relative_path) > 0)
    missing = or_(current.c.state.in_(("missing", "failed", "invalid", "mismatch", "corrupt")),
                  or_(current.c.relative_path.is_(None), current.c.relative_path == ""))
    counts = select(current.c.work_id.label("work_id"), func.count().label("recorded"),
        func.sum(case((verified, 1), else_=0)).label("verified"),
        func.sum(case((missing, 1), else_=0)).label("missing")).select_from(current).join(Work, Work.id == current.c.work_id).where(
        required).group_by(current.c.work_id).subquery()
    operations = _operation_window(ids)
    ledger, revision = aliased(ApplicationState), aliased(ApplicationState)
    current_derivative = or_(and_(Work.default_revision_id.is_(None), Derivative.revision_id.is_(None)),
        select(RevisionDerivative.revision_id).where(RevisionDerivative.revision_id == Work.default_revision_id,
            RevisionDerivative.derivative_id == Derivative.id).correlate(Work, Derivative).exists())
    thumbnail_ready = select(Derivative.id).where(Derivative.work_id == Work.id,
        Derivative.kind == "thumbnail", Derivative.page_index == 0, Derivative.spec_key == "v1",
        Derivative.state == "verified", func.length(Derivative.relative_path) > 0, current_derivative).exists()
    animation_ready = select(Derivative.id).where(Derivative.work_id == Work.id,
        Derivative.kind == "gif", Derivative.spec_key == "v1", Derivative.state.in_(("verified", "unknown")),
        func.length(Derivative.relative_path) > 0, current_derivative).exists()
    local_gap = or_(~thumbnail_ready, and_(Work.type == "ugoira", ~animation_ready))
    rows = session.execute(select(Work.id.label("work_id"), counts.c.recorded, counts.c.verified,
        counts.c.missing, operations.c.operation_id, operations.c.operation_state,
        operations.c.item_state, operations.c.reason, operations.c.concern, ledger.value.label("ledger"),
        revision.value.label("revision"), local_gap.label("local_gap")).outerjoin(counts, counts.c.work_id == Work.id).outerjoin(
        operations, and_(operations.c.work_id == Work.id, operations.c.rank == 1)).outerjoin(
        ledger, ledger.key == literal("maintenance_work:") + cast(Work.id, String)).outerjoin(
        revision, revision.key == "catalogue_revision").where(Work.id.in_(ids))).all()
    by_id = {work.id: work for work in works}
    result, current_revision = {}, 0
    for row in rows:
        work = by_id[row.work_id]
        total = 2 if work.type == "ugoira" else work.page_count
        verified_count = row.verified or 0
        missing_count = (row.missing or 0) + max(0, total - (row.recorded or 0))
        unknown_count = total - verified_count - missing_count
        archive = {"state": "partial" if missing_count else "unknown" if unknown_count else "complete",
                   "total": total, "verified": verified_count, "missing": missing_count, "unknown": unknown_count}
        source_update = _source_update(work)
        value = row.ledger if isinstance(row.ledger, dict) else {}
        processing = _processing(row, value, original_gap=bool(missing_count), local_gap=row.local_gap,
                                 source_pending=source_update["pending"])
        result[work.id] = {"archive": archive, "processing": processing, "source_update": source_update,
                           "recovery": _recovery(value, processing, archive)}
        current_revision = row.revision if type(row.revision) is int and row.revision >= 0 else 0
    return result, current_revision
