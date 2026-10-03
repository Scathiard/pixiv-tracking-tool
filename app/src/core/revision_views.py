"""Batch projections for pinned versions; media slots belong to bindings, not files."""
from types import SimpleNamespace

from sqlalchemy import and_, case, false, literal, or_, select

from .models import Asset, Derivative, RevisionAsset, RevisionDerivative, WorkRevision


def selected_snapshots(session, works, explicit=None):
    explicit = explicit or {}
    ids = {work.id: explicit.get(work.id, work.default_revision_id) for work in works}
    if not any(ids.values()):
        return {}
    snapshots = {revision.id: revision for revision in session.scalars(select(WorkRevision).where(
        WorkRevision.id.in_([value for value in ids.values() if value])))}
    return {work_id: snapshots[value] for work_id, value in ids.items()
            if value in snapshots and snapshots[value].work_id == work_id}


def media_maps(session, works, snapshots, *, ranges=None):
    """Two queries, bounded to requested slots for image-page windows."""
    works = list(works)
    work_ids = [work.id for work in works]
    fallback = [work.id for work in works if work.id not in snapshots]
    owners = {revision.id: work_id for work_id, revision in snapshots.items()}
    assets, derivatives = {}, {}

    def window(column_work, column_page, column_role, mapping=None):
        if ranges is None:
            return True
        return or_(false(), *(and_(column_work == (mapping[work_id] if mapping else work_id),
            or_(column_role != "image", and_(column_page >= start, column_page < end)))
            for work_id, (start, end) in ranges.items() if mapping is None or work_id in mapping))

    # One query per media category even when a window mixes migrated and legacy
    # works. Logical page numbers come from the selected revision binding.
    role = case((RevisionAsset.revision_id.is_not(None), RevisionAsset.role), else_=Asset.role)
    page = case((RevisionAsset.revision_id.is_not(None), RevisionAsset.page_index), else_=Asset.page_index)
    asset_query = select(Asset, RevisionAsset).outerjoin(RevisionAsset, and_(
        RevisionAsset.asset_id == Asset.id, RevisionAsset.revision_id.in_(owners))).where(Asset.work_id.in_(work_ids),
            or_(and_(Asset.work_id.in_(fallback), Asset.revision_id.is_(None)),
                RevisionAsset.revision_id.in_(owners)), window(Asset.work_id, page, role))
    for asset, binding in session.execute(asset_query):
        key = (asset.work_id, binding.role, binding.page_index) if binding else (asset.work_id, asset.role, asset.page_index)
        assets[key] = asset
    kind = case((RevisionDerivative.revision_id.is_not(None), RevisionDerivative.kind), else_=Derivative.kind)
    page = case((RevisionDerivative.revision_id.is_not(None), RevisionDerivative.page_index), else_=Derivative.page_index)
    derivative_query = select(Derivative, RevisionDerivative).outerjoin(RevisionDerivative, and_(
        RevisionDerivative.derivative_id == Derivative.id, RevisionDerivative.revision_id.in_(owners))).where(Derivative.work_id.in_(work_ids),
            or_(and_(Derivative.work_id.in_(fallback), Derivative.revision_id.is_(None)),
                RevisionDerivative.revision_id.in_(owners)),
            or_(kind == "gif", and_(kind == "thumbnail", page == 0),
                and_(kind == "legacy_alternate", window(Derivative.work_id, page, literal("image")))))
    for record, binding in session.execute(derivative_query):
        key = (record.work_id, binding.kind, binding.page_index, binding.spec_key) if binding else (record.work_id, record.kind, record.page_index, record.spec_key)
        derivatives[key] = record
    return assets, derivatives


def revision_work(work, revision):
    if revision is None:
        return work
    return SimpleNamespace(id=work.id, type=revision.type, page_count=revision.page_count,
                           source_metadata=work.source_metadata)


def apply_snapshot(view, work, revision):
    from ..security import sanitize_caption
    from .tag_translations import tag_details
    view["revision_id"] = revision.id if revision else None
    view["latest_revision_id"] = work.latest_revision_id
    if revision is not None:
        view.update(title=revision.title, type=revision.type, page_count=revision.page_count,
            caption=sanitize_caption(revision.caption), caption_state=revision.caption_state,
            tags=revision.tags, tag_details=tag_details(revision.tags, revision.source_metadata))
    return view
