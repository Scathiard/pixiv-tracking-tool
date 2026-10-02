"""Bounded, database-only work state shared by gallery, detail, and feed."""
from datetime import datetime, timezone

from sqlalchemy import Integer, String, and_, case, cast, func, literal, or_, select, true, union_all
from sqlalchemy.orm import aliased

from .catalogue_revision import catalogue_revision
from .models import ApplicationState, Asset, Derivative, Operation, OperationItem, Work

ACTIVE = ("queued", "planning", "ready", "running", "interrupted")
LOCAL_KINDS = ("check", "metadata", "repair", "verify", "backfill", "thumbnails", "reconcile")


def _operation_window(ids):
    # Older items identify their work through their asset or derivative. Resolve
    # that in SQL, so even a 2,000-page work never loads its job history in Python.
    asset, derivative = aliased(Asset), aliased(Derivative)
    target_work = func.coalesce(OperationItem.target["work_id"].as_integer(), asset.work_id, derivative.work_id)
    direct = select(target_work.label("work_id"), Operation.id.label("operation_id"),
        Operation.state.label("operation_state"), Operation.created_at.label("created_at"),
        Operation.updated_at.label("updated_at"), OperationItem.state.label("item_state"),
        OperationItem.result["message"].as_string().label("reason"),
        OperationItem.position.label("position"),
        case((or_(OperationItem.target["asset_id"].as_string().is_not(None), OperationItem.kind == "ugoira_pair"), "original"),
             (or_(OperationItem.target["record_id"].as_string().is_not(None), Operation.kind == "thumbnails",
                  OperationItem.kind.in_(("gif", "thumbnail", "verify_derivative"))), "local"),
             else_="work").label("concern")).select_from(OperationItem).join(Operation).outerjoin(
        asset, asset.id == OperationItem.target["asset_id"].as_string()).outerjoin(
        derivative, derivative.id == OperationItem.target["record_id"].as_string()).where(
        target_work.in_(ids), Operation.kind.in_(LOCAL_KINDS))
    # The persisted selection resolves unplanned work and legacy error items
    # without an asset/derivative identity. Expand that selection inside SQL.
    scope = func.json_each(Operation.parameters, "$.work_ids").table_valued("value").alias("scope")
    scoped = select(cast(scope.c.value, Integer).label("work_id"), Operation.id.label("operation_id"),
        Operation.state.label("operation_state"), Operation.created_at.label("created_at"),
        Operation.updated_at.label("updated_at"), func.coalesce(OperationItem.state, "pending").label("item_state"),
        OperationItem.result["message"].as_string().label("reason"),
        func.coalesce(OperationItem.position, 0).label("position"),
        case((Operation.kind == "thumbnails", "local"), else_="work").label("concern")).select_from(Operation).join(
        scope, true()).outerjoin(OperationItem, OperationItem.operation_id == Operation.id).where(
        cast(scope.c.value, Integer).in_(ids), Operation.kind.in_(LOCAL_KINDS),
        or_(OperationItem.id.is_not(None), Operation.state.in_(ACTIVE)),
        OperationItem.target["work_id"].as_integer().is_(None),
        OperationItem.target["asset_id"].as_string().is_(None),
        OperationItem.target["record_id"].as_string().is_(None))
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
    return {"pending": bool(pending), "reason": reason}


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
    required = or_(and_(Work.type != "ugoira", Asset.role == "image", Asset.page_index < Work.page_count),
                   and_(Work.type == "ugoira", Asset.role.in_(("ugoira_zip", "ugoira_timing")), Asset.page_index == 0))
    verified = and_(Asset.state == "verified", func.length(Asset.relative_path) > 0)
    missing = or_(Asset.state.in_(("missing", "failed", "invalid", "mismatch", "corrupt")),
                  or_(Asset.relative_path.is_(None), Asset.relative_path == ""))
    counts = select(Asset.work_id.label("work_id"), func.count().label("recorded"),
        func.sum(case((verified, 1), else_=0)).label("verified"),
        func.sum(case((missing, 1), else_=0)).label("missing")).join(Work, Work.id == Asset.work_id).where(
        Asset.work_id.in_(ids), required).group_by(Asset.work_id).subquery()
    operations = _operation_window(ids)
    ledger, revision = aliased(ApplicationState), aliased(ApplicationState)
    thumbnail_ready = select(Derivative.id).where(Derivative.work_id == Work.id,
        Derivative.kind == "thumbnail", Derivative.page_index == 0, Derivative.spec_key == "v1",
        Derivative.state == "verified", func.length(Derivative.relative_path) > 0).exists()
    animation_ready = select(Derivative.id).where(Derivative.work_id == Work.id,
        Derivative.kind == "gif", Derivative.spec_key == "v1", Derivative.state.in_(("verified", "unknown")),
        func.length(Derivative.relative_path) > 0).exists()
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
