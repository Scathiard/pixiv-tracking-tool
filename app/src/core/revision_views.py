"""Batch projections for pinned versions; media slots belong to bindings, not files."""
from types import SimpleNamespace

from sqlalchemy import and_, case, false, literal, or_, select, union_all, literal_column
from sqlalchemy.orm import defer, load_only, aliased

from .models import Asset, Derivative, RevisionAsset, RevisionDerivative, WorkRevision


def selected_snapshots(session, works, explicit=None, *, compact=False, display=False):
    explicit = explicit or {}
    ids = {work.id: explicit.get(work.id, work.default_revision_id) for work in works}
    if not any(ids.values()):
        return {}
    # Resolve UUIDs through the covering identity index, then read the selected
    # physical rows in rowid order rather than random UUID table-lookup order.
    row_id = literal_column("work_revisions.rowid")
    selected_rows = select(row_id).select_from(WorkRevision).where(
        WorkRevision.id.in_([value for value in ids.values() if value])).correlate(None)
    statement = select(WorkRevision).where(row_id.in_(selected_rows))
    if compact:
        # Cards need the pinned shape and title, never discarded detail JSON or
        # captions. Fail on accidental access instead of issuing per-card SQL.
        statement = statement.options(load_only(WorkRevision.id, WorkRevision.work_id,
            WorkRevision.title, WorkRevision.type, WorkRevision.page_count, raiseload=True))
    elif display:
        statement = statement.options(defer(WorkRevision.source_metadata, raiseload=True))
    snapshots = {revision.id: revision for revision in session.scalars(statement)}
    if display and not compact:
        from .display_metadata import read
        metadata = read(session, snapshots, revisions=True, include_translations=True)
        for revision in snapshots.values():
            revision._read_metadata = metadata[revision.id]
    return {work_id: snapshots[value] for work_id, value in ids.items()
            if value in snapshots and snapshots[value].work_id == work_id}


def media_maps(session, works, snapshots, *, ranges=None, compact=False):
    """Read requested indexed slots, independent of unrelated page/history rows.

    Separate UNION branches retain equality prefixes on the binding/legacy slot
    indexes. A broad `role != image OR page BETWEEN ...` predicate would force a
    scan of all pages of a long work before returning this fixed-size window.
    """
    if compact:
        return card_media_maps(session, works, snapshots)
    from .media import CARD_MOTION_SPEC
    works = list(works)
    assets, derivatives = {}, {}
    if not works:
        return assets, derivatives
    asset_queries, derivative_queries = [], []
    for work in works:
        snapshot = snapshots.get(work.id)
        start, end = ranges[work.id] if ranges is not None and work.id in ranges else (0, 2**63 - 1)
        if snapshot is None:
            # Do not attach a false outer join merely to produce empty binding
            # objects: SQLite can still walk the whole binding table for each
            # legacy asset. Only logical slot values are needed by this reader.
            asset_base = select(Asset, Asset.role.label("slot_role"),
                Asset.page_index.label("slot_page")).where(
                Asset.work_id == work.id, Asset.revision_id.is_(None))
            asset_role, asset_page = Asset.role, Asset.page_index
            derivative_base = select(Derivative, Derivative.kind.label("slot_kind"),
                Derivative.page_index.label("slot_page"), Derivative.spec_key.label("slot_spec")).where(
                Derivative.work_id == work.id, Derivative.revision_id.is_(None))
            kind, page, spec = Derivative.kind, Derivative.page_index, Derivative.spec_key
        else:
            asset_base = select(Asset, RevisionAsset.role.label("slot_role"),
                RevisionAsset.page_index.label("slot_page")).select_from(RevisionAsset).join(
                Asset, Asset.id == RevisionAsset.asset_id).where(
                    RevisionAsset.revision_id == snapshot.id, Asset.work_id == work.id)
            asset_role, asset_page = RevisionAsset.role, RevisionAsset.page_index
            derivative_base = select(Derivative, RevisionDerivative.kind.label("slot_kind"),
                RevisionDerivative.page_index.label("slot_page"), RevisionDerivative.spec_key.label("slot_spec")).select_from(RevisionDerivative).join(
                Derivative, Derivative.id == RevisionDerivative.derivative_id).where(
                    RevisionDerivative.revision_id == snapshot.id, Derivative.work_id == work.id)
            kind, page, spec = RevisionDerivative.kind, RevisionDerivative.page_index, RevisionDerivative.spec_key
        asset_queries.append(asset_base.where(asset_role == "image", asset_page >= start, asset_page < end))
        for role in ("ugoira_zip", "ugoira_timing"):
            asset_queries.append(asset_base.where(asset_role == role, asset_page == 0))
        derivative_queries.append(derivative_base.where(kind == "gif", page == 0, spec == "v1"))
        derivative_queries.append(derivative_base.where(kind == "thumbnail", page == 0,
            spec.in_(("v1", CARD_MOTION_SPEC))))
        derivative_queries.append(derivative_base.where(kind == "legacy_alternate", page >= start, page < end))
    # SQLite permits at most 500 compound terms. Hydration chunks retain two
    # category queries per <=160 requested works without global materialization.
    for begin in range(0, len(asset_queries), 480):
        statement = select(Asset, literal_column("slot_role"), literal_column("slot_page")).from_statement(
            union_all(*asset_queries[begin:begin + 480]))
        for asset, role, page in session.execute(statement):
            key = (asset.work_id, role, page)
            assets[key] = asset
    for begin in range(0, len(derivative_queries), 480):
        statement = select(Derivative, literal_column("slot_kind"), literal_column("slot_page"),
            literal_column("slot_spec")).from_statement(union_all(*derivative_queries[begin:begin + 480]))
        for record, kind, page, spec in session.execute(statement):
            key = (record.work_id, kind, page, spec)
            derivatives[key] = record
    return assets, derivatives


def card_media_maps(session, works, snapshots):
    """Read page-zero card slots, then hydrate narrow entities in physical order.

    Bindings own the logical slot even when a physical file has another slot.
    Resolve bound UUIDs through their covering identity indexes without reading
    the file row until the final ordered, bounded hydration query.
    """
    from .media import CARD_MOTION_SPEC
    asset_queries, derivative_queries = [], []
    for work in works:
        snapshot = snapshots.get(work.id)
        animation = (snapshot.type if snapshot is not None else work.type) == "ugoira"
        if snapshot is None:
            asset_base = select(literal_column("assets.rowid").label("physical_row"),
                literal(work.id).label("owner"), literal("image").label("slot_role"),
                literal(0).label("slot_page")).select_from(Asset).where(
                    Asset.work_id == work.id, Asset.revision_id.is_(None))
            asset_role, asset_page = Asset.role, Asset.page_index
            derivative_base = select(literal_column("derivatives.rowid").label("physical_row"),
                literal(work.id).label("owner"), Derivative.kind.label("slot_kind"),
                Derivative.page_index.label("slot_page"), Derivative.spec_key.label("slot_spec")).where(
                    Derivative.work_id == work.id, Derivative.revision_id.is_(None))
            kind, page, spec = Derivative.kind, Derivative.page_index, Derivative.spec_key
        else:
            asset_row = select(literal_column("assets.rowid")).select_from(Asset).where(
                Asset.id == RevisionAsset.asset_id).correlate(RevisionAsset).scalar_subquery()
            asset_base = select(asset_row.label("physical_row"), literal(work.id).label("owner"),
                RevisionAsset.role.label("slot_role"), RevisionAsset.page_index.label("slot_page")).where(
                    RevisionAsset.revision_id == snapshot.id)
            asset_role, asset_page = RevisionAsset.role, RevisionAsset.page_index
            derivative_row = select(literal_column("derivatives.rowid")).select_from(Derivative).where(
                Derivative.id == RevisionDerivative.derivative_id).correlate(RevisionDerivative).scalar_subquery()
            derivative_base = select(derivative_row.label("physical_row"), literal(work.id).label("owner"),
                RevisionDerivative.kind.label("slot_kind"), RevisionDerivative.page_index.label("slot_page"),
                RevisionDerivative.spec_key.label("slot_spec")).where(RevisionDerivative.revision_id == snapshot.id)
            kind, page, spec = RevisionDerivative.kind, RevisionDerivative.page_index, RevisionDerivative.spec_key
        asset_queries.append(asset_base.where(asset_role == "image", asset_page == 0))
        derivative_queries.append(derivative_base.where(kind == "thumbnail", page == 0,
            spec.in_(("v1", CARD_MOTION_SPEC)) if animation else spec == "v1"))
        if animation:
            derivative_queries.append(derivative_base.where(kind == "gif", page == 0, spec == "v1"))

    def hydrate(model, queries, columns):
        slots = []
        for begin in range(0, len(queries), 480):
            slots.extend(session.execute(union_all(*queries[begin:begin + 480])))
        physical = sorted({row[0] for row in slots if row[0] is not None})
        row_id = literal_column(f"{model.__tablename__}.rowid")
        records = {}
        # At most 480 row IDs plus 480 owner IDs per query, including SQLite
        # builds with the older 999-variable limit. No per-work hydration.
        for begin in range(0, len(physical), 480):
            chunk = physical[begin:begin + 480]
            chunk_set = set(chunk)
            chunk_owners = {row[1] for row in slots if row[0] in chunk_set}
            statement = select(model, row_id).where(row_id.in_(chunk), model.work_id.in_(chunk_owners)).order_by(row_id)
            statement = statement.options(load_only(*columns, raiseload=True))
            records.update({row: record for record, row in session.execute(statement)})
        return {tuple(slot[1:]): records[slot[0]] for slot in slots
                if slot[0] in records and records[slot[0]].work_id == slot[1]}

    assets = hydrate(Asset, asset_queries, (Asset.id, Asset.work_id, Asset.relative_path, Asset.state))
    derivatives = hydrate(Derivative, derivative_queries, (Derivative.id, Derivative.work_id,
        Derivative.relative_path, Derivative.state, Derivative.sha256, Derivative.source_version))
    return assets, derivatives


def revision_work(work, revision):
    if revision is None:
        return work
    return SimpleNamespace(id=work.id, type=revision.type, page_count=revision.page_count,
                           source_metadata=work.source_metadata)


def apply_snapshot(view, work, revision, *, compact=False):
    view["revision_id"] = revision.id if revision else None
    view["latest_revision_id"] = work.latest_revision_id
    if revision is not None:
        view.update(title=revision.title, type=revision.type, page_count=revision.page_count)
        if not compact:
            from ..security import sanitize_caption
            from .tag_translations import tag_details
            view.update(caption=sanitize_caption(revision.caption), caption_state=revision.caption_state,
                tags=revision.tags, tag_details=tag_details(revision.tags,
                    revision._read_metadata if hasattr(revision, "_read_metadata") else revision.source_metadata))
    return view
