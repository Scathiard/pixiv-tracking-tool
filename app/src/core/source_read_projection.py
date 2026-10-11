"""Persistent source anomalies and latest-local summaries for bounded overviews.

Installation/backfill are explicit candidate operations. Source triggers preserve
old/new identities; maintenance shares the caller's transaction and never commits.
Source selection deliberately follows the legacy SQL JSON scalar/boolean rules,
while presentation still uses availability_snapshot. Offline and current-account
reads select maintained global or NULL/current buckets without reclassifying N.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from .models import Asset, Derivative, RevisionAsset, RevisionDerivative, Work, WorkRevision
from .read_projection import ProjectionUnavailable

TABLES = ("read_source_entries", "read_source_counts", "read_source_work_local", "read_source_artist_totals", "read_source_resource_users", "read_source_slot_facts", "read_source_local_totals")
DIRTY_ENTITIES = {"source_work", "source_local_work", "source_artist"}
_DDL = (
    "CREATE INDEX ix_artists_name_order ON artists(name,id)",
    "CREATE TABLE read_source_entries (kind TEXT NOT NULL, entity_id INTEGER NOT NULL, bucket TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY(kind,entity_id)) WITHOUT ROWID",
    "CREATE INDEX ix_read_source_category ON read_source_entries(kind,category,entity_id)",
    "CREATE INDEX ix_read_source_bucket ON read_source_entries(kind,bucket,entity_id)",
    "CREATE INDEX ix_read_source_bucket_category ON read_source_entries(kind,bucket,category,entity_id)",
    "CREATE TABLE read_source_counts (kind TEXT NOT NULL, bucket TEXT NOT NULL, category TEXT NOT NULL, value INTEGER NOT NULL CHECK(value>=0), PRIMARY KEY(kind,bucket,category)) WITHOUT ROWID",
    "CREATE TABLE read_source_work_local (work_id INTEGER PRIMARY KEY, artist_id INTEGER NOT NULL, included INTEGER NOT NULL CHECK(included IN (0,1)), order_key TEXT NOT NULL, artist_state TEXT NOT NULL, local TEXT NOT NULL)",
    "CREATE INDEX ix_read_source_artist_recent ON read_source_work_local(artist_id,included,order_key DESC,work_id DESC)",
    "CREATE TABLE read_source_slot_facts (work_id INTEGER NOT NULL, scope TEXT NOT NULL, slot TEXT NOT NULL, physical_id TEXT, position INTEGER NOT NULL, role TEXT NOT NULL, page INTEGER, kind TEXT NOT NULL, recorded INTEGER NOT NULL, verified INTEGER NOT NULL, missing INTEGER NOT NULL, unknown INTEGER NOT NULL, healthy INTEGER NOT NULL, PRIMARY KEY(work_id,scope,slot)) WITHOUT ROWID",
    "CREATE INDEX ix_read_source_slot_physical ON read_source_slot_facts(physical_id,work_id,scope,slot)",
    "CREATE INDEX ix_read_source_missing_slots ON read_source_slot_facts(work_id,scope,missing,position)",
    "CREATE TABLE read_source_local_totals (work_id INTEGER PRIMARY KEY, detail_missing INTEGER NOT NULL, detail_unknown INTEGER NOT NULL, original_recorded INTEGER NOT NULL, original_verified INTEGER NOT NULL, original_missing INTEGER NOT NULL, thumbnail_healthy INTEGER NOT NULL, thumbnail_verified INTEGER NOT NULL, gif_healthy INTEGER NOT NULL, gif_verified INTEGER NOT NULL, artist_required INTEGER NOT NULL, artist_ugoira INTEGER NOT NULL, artist_caption_missing INTEGER NOT NULL, artist_caption_unknown INTEGER NOT NULL)",
    "CREATE TABLE read_source_resource_users (physical_id TEXT NOT NULL, work_id INTEGER NOT NULL, PRIMARY KEY(physical_id,work_id)) WITHOUT ROWID",
    "CREATE INDEX ix_read_source_resource_work ON read_source_resource_users(work_id,physical_id)",
    "CREATE TABLE read_source_artist_totals (artist_id INTEGER PRIMARY KEY, total INTEGER NOT NULL DEFAULT 0 CHECK(total>=0), incomplete INTEGER NOT NULL DEFAULT 0 CHECK(incomplete>=0), unknown INTEGER NOT NULL DEFAULT 0 CHECK(unknown>=0))",
)


def _queue(entity, identity, selection=""):
    return f"INSERT OR IGNORE INTO read_model_dirty SELECT '{entity}',CAST({identity} AS TEXT) {selection or 'WHERE ' + identity + ' IS NOT NULL'};"


def _trigger(connection, table, action, body, when=None):
    connection.exec_driver_sql(f"CREATE TRIGGER read_source_capture_{table}_{action.lower()} AFTER {action} ON {table} "
        + (f"WHEN {when} " if when else "") + f"BEGIN {body} END")


def install(connection):
    for statement in _DDL:
        connection.exec_driver_sql(statement)
    connection.exec_driver_sql("INSERT OR IGNORE INTO read_common_counts VALUES('artists',0)")
    for table in ("works", "artists", "archive_intents", "application_state", "work_revisions",
                  "assets", "derivatives", "revision_assets", "revision_derivatives"):
        for action in ("INSERT", "UPDATE", "DELETE"):
            sides = ("NEW",) if action == "INSERT" else ("OLD",) if action == "DELETE" else ("OLD", "NEW")
            body = ""
            for side in sides:
                if table == "works":
                    body += _queue("source_work", f"{side}.id")
                    shape = " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in
                        ("id", "type", "page_count", "caption_state", "default_revision_id", "latest_revision_id"))
                    body += _queue("source_local_work", f"{side}.id", "WHERE " + shape if action == "UPDATE" else "")
                elif table == "artists":
                    body += _queue("source_artist", f"{side}.id")
                    if action != "UPDATE":
                        change = 1 if action == "INSERT" else -1
                        body += "SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM read_common_counts WHERE scope='artists') THEN RAISE(ABORT,'mandatory artist counter is missing') END;"
                        body += f"UPDATE read_common_counts SET value=value+({change}) WHERE scope='artists';"
                elif table == "archive_intents":
                    body += _queue("source_work", "id", f"FROM works WHERE pixiv_illust_id={side}.pixiv_illust_id")
                elif table == "application_state":
                    body += _queue("source_artist", "id", f"FROM artists WHERE substr({side}.key,1,14)='artist_source:' AND pixiv_user_id=substr({side}.key,15)")
                elif table == "work_revisions":
                    body += _queue("source_local_work", "id", f"FROM works WHERE latest_revision_id={side}.id OR default_revision_id={side}.id")
                elif table in ("revision_assets", "revision_derivatives"):
                    body += _queue("source_local_work", "id", f"FROM works WHERE latest_revision_id={side}.revision_id OR default_revision_id={side}.revision_id")
                else:
                    prefix = "asset:" if table == "assets" else "derivative:"
                    # Only current cached dependencies are indexed here. Looking
                    # through a physical record's H historical bindings is not
                    # necessary to find its currently affected source summaries.
                    body += _queue("source_local_work", f"{side}.work_id")
                    body += _queue("source_local_work", "work_id", f"FROM read_source_resource_users WHERE physical_id='{prefix}'||{side}.id")
            when = None
            if action == "UPDATE" and table in ("assets", "derivatives", "work_revisions"):
                fields = {
                    "assets": ("id", "work_id", "revision_id", "role", "page_index"),
                    "derivatives": ("id", "work_id", "revision_id", "kind", "page_index", "spec_key"),
                    "work_revisions": ("id", "work_id", "type", "page_count", "caption_state"),
                }[table]
                when = " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in fields)
            _trigger(connection, table, action, body, when)


def _classification_sql(kind, predicate=""):
    if kind == "works":
        source, owner = "w.source_metadata", "works w"
        path = "$.remote_availability."
        extra = " AND NOT EXISTS(SELECT 1 FROM archive_intents i WHERE i.pixiv_illust_id=w.pixiv_illust_id AND i.mode='suppressed')"
    else:
        source, owner = "s.value", "artists w JOIN application_state s ON s.key='artist_source:'||w.pixiv_user_id"
        path, extra = "$.", ""
    value = lambda name: f"json_extract({source},'{path}{name}')"
    state, confirmed, evidence, policy, account = [value(name) for name in
        ("state", "confirmed", "deletion_evidence", "policy", "account_id")]
    # SQLite IS 1 exactly matches JSON.as_boolean().is_(True), including numeric
    # legacy values; string 'true' is intentionally not promoted to a fact.
    deleted = f"({state}='deleted' AND {confirmed} IS 1 AND {evidence} IS 1)"
    unavailable = f"({state} IN ('unavailable','private','deleted') AND NOT coalesce({deleted},0) AND {confirmed} IS 1 AND {policy}='source-v2')"
    bucket = f"CASE WHEN {account} IS NULL THEN 'null' WHEN typeof({account})='text' THEN 'text:'||{account} ELSE 'other:'||typeof({account})||':'||json_quote({account}) END"
    return f"SELECT w.id,{bucket} bucket,CASE WHEN {deleted} THEN 'deleted' WHEN {unavailable} THEN 'unavailable' END category FROM {owner} WHERE 1=1{extra}{predicate}"


def _classes(connection, kind, ids=None):
    predicate, args = "", ()
    if ids is not None:
        if not ids:
            return {}
        predicate = " AND w.id IN (" + ",".join("?" for _ in ids) + ")"
        args = tuple(ids)
    return {row[0]: (row[1], row[2]) for row in connection.exec_driver_sql(_classification_sql(kind, predicate), args) if row[2]}


def _replace_entry(connection, kind, identity, new):
    old = connection.exec_driver_sql("SELECT bucket,category FROM read_source_entries WHERE kind=? AND entity_id=?", (kind, identity)).first()
    old = tuple(old) if old else None
    if old == new:
        return
    for fact, delta in ((old, -1), (new, 1)):
        if fact is None:
            continue
        for bucket in ("*", fact[0]):
            connection.exec_driver_sql("INSERT OR IGNORE INTO read_source_counts VALUES(?,?,?,0)", (kind, bucket, fact[1]))
            connection.exec_driver_sql("UPDATE read_source_counts SET value=value+? WHERE kind=? AND bucket=? AND category=?", (delta, kind, bucket, fact[1]))
    if new is None:
        connection.exec_driver_sql("DELETE FROM read_source_entries WHERE kind=? AND entity_id=?", (kind, identity))
    else:
        connection.exec_driver_sql("INSERT INTO read_source_entries VALUES(?,?,?,?) ON CONFLICT(kind,entity_id) DO UPDATE SET bucket=excluded.bucket,category=excluded.category", (kind, identity, *new))


def _artist_flags(session, ids):
    """The old artist summary's independent latest-shape/physical-slot oracle.

    Unlike source detail, this intentionally uses Work.caption_state and physical
    slot identity. Narrow work IDs bound the computation on writes/backfill only.
    """
    current_type, current_pages = func.coalesce(WorkRevision.type, Work.type), func.coalesce(WorkRevision.page_count, Work.page_count)
    bound = or_(and_(Work.latest_revision_id.is_(None), Asset.revision_id.is_(None)),
        select(RevisionAsset.asset_id).where(RevisionAsset.revision_id == Work.latest_revision_id, RevisionAsset.asset_id == Asset.id).exists())
    required = and_(bound, or_(and_(current_type != "ugoira", Asset.role == "image", Asset.page_index < current_pages),
        and_(current_type == "ugoira", Asset.role.in_(("ugoira_zip", "ugoira_timing")), Asset.page_index == 0)))
    bad = ("missing", "failed", "invalid", "mismatch", "corrupt")
    counts = select(Asset.work_id.label("work_id"), func.count().label("recorded"),
        func.sum(case((and_(Asset.state == "verified", func.length(Asset.relative_path) > 0), 1), else_=0)).label("verified"),
        func.sum(case((or_(Asset.state.in_(bad), Asset.relative_path.is_(None), Asset.relative_path == ""), 1), else_=0)).label("missing")
        ).join(Work, Work.id == Asset.work_id).outerjoin(WorkRevision, WorkRevision.id == Work.latest_revision_id).where(Work.id.in_(ids), required).group_by(Asset.work_id).subquery()
    derived_bound = or_(and_(Work.latest_revision_id.is_(None), Derivative.revision_id.is_(None)),
        select(RevisionDerivative.derivative_id).where(RevisionDerivative.revision_id == Work.latest_revision_id, RevisionDerivative.derivative_id == Derivative.id).exists())
    def derivative(kind, verified=False):
        return select(Derivative.id).where(Derivative.work_id == Work.id, Derivative.kind == kind, Derivative.page_index == 0,
            Derivative.spec_key == "v1", func.length(Derivative.relative_path) > 0, derived_bound,
            Derivative.state == "verified" if verified else ~Derivative.state.in_(bad)).exists()
    total = case((current_type == "ugoira", 2), else_=current_pages)
    incomplete = or_(func.coalesce(counts.c.recorded, 0) < total, func.coalesce(counts.c.missing, 0) > 0,
        ~derivative("thumbnail"), and_(current_type == "ugoira", ~derivative("gif")), Work.caption_state.in_(("unavailable", "missing", "failed")))
    unknown = or_(func.coalesce(counts.c.verified, 0) < total, ~Work.caption_state.in_(("available", "empty")),
        ~derivative("thumbnail", True), and_(current_type == "ugoira", ~derivative("gif", True)))
    rows = session.execute(select(Work.id, incomplete, unknown).outerjoin(counts, counts.c.work_id == Work.id)
        .outerjoin(WorkRevision, WorkRevision.id == Work.latest_revision_id).where(Work.id.in_(ids)))
    return {row[0]: "incomplete" if row[1] else "unknown" if row[2] else "complete" for row in rows}


def _local_details(session, works):
    from .library import Library
    from .revision_views import media_maps, selected_snapshots
    explicit = {work.id: work.latest_revision_id for work in works if work.latest_revision_id}
    snapshots = selected_snapshots(session, works, explicit)
    assets, derivatives = media_maps(session, works, snapshots)
    asset_groups, derivative_groups = {}, {}
    for key, asset in assets.items():
        asset_groups.setdefault(key[0], []).append(asset)
    for key, record in derivatives.items():
        derivative_groups.setdefault(key[0], []).append(record)
    result = {}
    for work in works:
        snapshot = snapshots.get(work.id)
        shape = snapshot if snapshot is not None else work
        local = Library._local_work(SimpleNamespace(type=shape.type, page_count=shape.page_count, caption_state=shape.caption_state),
            asset_groups.get(work.id, ()), derivative_groups.get(work.id, ()))
        result[work.id] = {**local, "default_revision_id": work.default_revision_id,
            "latest_revision_id": work.latest_revision_id, "has_saved_version": work.default_revision_id is not None}
    return result


def _artist_delta(connection, old, new):
    def fact(row):
        if row is None or not row[1]:
            return None
        return (row[0], (1, int(row[2] == "incomplete"), int(row[2] == "unknown")))
    before, after = fact(old), fact(new)
    if before == after:
        return
    for value, direction in ((before, -1), (after, 1)):
        if value is None:
            continue
        connection.exec_driver_sql("INSERT OR IGNORE INTO read_source_artist_totals(artist_id) VALUES(?)", (value[0],))
        connection.exec_driver_sql("UPDATE read_source_artist_totals SET total=total+?,incomplete=incomplete+?,unknown=unknown+? WHERE artist_id=?",
            (*[direction * count for count in value[1]], value[0]))


def _dependencies(connection, work):
    """Locate only legacy and up to two selected bindings, never all history."""
    values = set()
    selected = tuple(dict.fromkeys(value for value in (work.latest_revision_id, work.default_revision_id) if value))
    for table, binding, field, prefix in (("assets", "revision_assets", "asset_id", "asset:"),
            ("derivatives", "revision_derivatives", "derivative_id", "derivative:")):
        values.update(prefix + str(value) for value in connection.exec_driver_sql(
            f"SELECT id FROM {table} WHERE work_id=? AND revision_id IS NULL", (work.id,)).scalars())
        for revision_id in selected:
            values.update(prefix + str(value) for value in connection.exec_driver_sql(
                f"SELECT b.{field} FROM {binding} b JOIN {table} p ON p.id=b.{field} WHERE b.revision_id=? AND p.work_id=?", (revision_id, work.id)).scalars())
    return values


def _replace_dependencies(connection, work_id, values):
    old = set(connection.exec_driver_sql("SELECT physical_id FROM read_source_resource_users WHERE work_id=?", (work_id,)).scalars())
    for value in sorted(old - values):
        connection.exec_driver_sql("DELETE FROM read_source_resource_users WHERE physical_id=? AND work_id=?", (value, work_id))
    for value in sorted(values - old):
        connection.exec_driver_sql("INSERT INTO read_source_resource_users VALUES(?,?)", (value, work_id))


_TOTAL_FIELDS = ("detail_missing", "detail_unknown", "original_recorded", "original_verified", "original_missing",
    "thumbnail_healthy", "thumbnail_verified", "gif_healthy", "gif_verified")
_BAD = ("missing", "failed", "invalid", "mismatch", "corrupt")


def _resource_values(scope, state, path, *, present=True):
    missing = not present or state in _BAD or not path
    verified = present and bool(path) and state == "verified"
    healthy = present and bool(path) and state not in _BAD
    if scope == "detail":
        return (0, 0, int(missing), int(not missing and state != "verified"), 0)
    if scope == "artist_original":
        return (1, int(verified), int(missing), 0, 0)
    return (0, int(verified), 0, 0, int(healthy))


def _contribution(scope, values):
    recorded, verified, missing, unknown, healthy = values
    if scope == "detail":
        return (missing, unknown, 0, 0, 0, 0, 0, 0, 0)
    if scope == "artist_original":
        return (0, 0, recorded, verified, missing, 0, 0, 0, 0)
    if scope == "artist_thumbnail":
        return (0, 0, 0, 0, 0, healthy, verified, 0, 0)
    return (0, 0, 0, 0, 0, 0, 0, healthy, verified)


def _summary_states(totals):
    missing, unknown, recorded, verified, bad, thumb, thumb_verified, gif, gif_verified, required, animation, caption_missing, caption_unknown = totals
    artist_incomplete = recorded < required or bad > 0 or not thumb or animation and not gif or caption_missing
    artist_unknown = verified < required or caption_unknown or not thumb_verified or animation and not gif_verified
    return ("incomplete" if missing else "unknown" if unknown else "complete",
        "incomplete" if artist_incomplete else "unknown" if artist_unknown else "complete")


def _normalized_local(session, works):
    """Explicit selected-scope rebuild. Only structural writes/backfill call it.

    Detail facts retain the old media-map/physical-slot oracle. Artist facts
    retain the independent latest-shape and physical-record-count semantics.
    Ordinary state/path changes update these rows by physical index instead.
    """
    from .revision_views import media_maps, selected_snapshots
    if not works:
        return {}, {}, {}
    ids = [work.id for work in works]
    snapshots = selected_snapshots(session, works, {work.id: work.latest_revision_id for work in works if work.latest_revision_id})
    assets, derivatives = media_maps(session, works, snapshots)
    asset_groups, derivative_groups = {}, {}
    for key, asset in assets.items():
        asset_groups.setdefault(key[0], {})[(asset.role, asset.page_index)] = asset
    for key, record in derivatives.items():
        derivative_groups.setdefault(key[0], {})[(record.kind, record.page_index, record.spec_key)] = record
    selected_ids = {work.latest_revision_id for work in works if work.latest_revision_id}
    latest = {revision.id: revision for revision in session.scalars(select(WorkRevision).where(WorkRevision.id.in_(selected_ids)))} if selected_ids else {}
    facts, headers, metadata = {}, {}, {}
    for work in works:
        shape = snapshots.get(work.id, work)
        rows = facts[work.id] = {}
        def detail(role, page, kind, position, record=None, caption=None):
            slot = role + ":" + str(page)
            values = ((0, 0, int(caption in ("unavailable", "missing", "failed")),
                int(caption not in ("unavailable", "missing", "failed", "available", "empty")), 0)
                if role == "caption" else _resource_values("detail", record.state if record else None,
                    record.relative_path if record else None, present=record is not None))
            physical = (("asset:" if kind == "original" else "derivative:") + record.id) if record else None
            rows[("detail", slot)] = (physical, position, role, page, kind, *values)
        required = [("ugoira_zip", 0), ("ugoira_timing", 0)] if shape.type == "ugoira" else [("image", page) for page in range(shape.page_count)]
        for position, (role, page) in enumerate(required):
            detail(role, page if role == "image" else None, "original", position,
                asset_groups.get(work.id, {}).get((role, page)))
        position = len(required)
        detail("thumbnail", None, "derivative", position, derivative_groups.get(work.id, {}).get(("thumbnail", 0, "v1")))
        position += 1
        if shape.type == "ugoira":
            detail("gif", None, "derivative", position, derivative_groups.get(work.id, {}).get(("gif", 0, "v1")))
            position += 1
        detail("caption", None, "metadata", position, caption=shape.caption_state)
        headers[work.id] = {"default_revision_id": work.default_revision_id,
            "latest_revision_id": work.latest_revision_id, "has_saved_version": work.default_revision_id is not None}
        revision = latest.get(work.latest_revision_id)
        artist_type = revision.type if revision and revision.type is not None else work.type
        artist_pages = revision.page_count if revision and revision.page_count is not None else work.page_count
        metadata[work.id] = (2 if artist_type == "ugoira" else artist_pages, int(artist_type == "ugoira"),
            int(work.caption_state in ("unavailable", "missing", "failed")),
            int(work.caption_state is not None and work.caption_state not in ("available", "empty")))
    current_type, current_pages = func.coalesce(WorkRevision.type, Work.type), func.coalesce(WorkRevision.page_count, Work.page_count)
    bound = or_(and_(Work.latest_revision_id.is_(None), Asset.revision_id.is_(None)),
        select(RevisionAsset.asset_id).where(RevisionAsset.revision_id == Work.latest_revision_id, RevisionAsset.asset_id == Asset.id).exists())
    required = and_(bound, or_(and_(current_type != "ugoira", Asset.role == "image", Asset.page_index < current_pages),
        and_(current_type == "ugoira", Asset.role.in_(("ugoira_zip", "ugoira_timing")), Asset.page_index == 0)))
    rows = session.execute(select(Asset.work_id, Asset.id, Asset.state, Asset.relative_path).join(Work, Work.id == Asset.work_id)
        .outerjoin(WorkRevision, WorkRevision.id == Work.latest_revision_id).where(Work.id.in_(ids), required))
    for work_id, identity, state, path in rows:
        facts[work_id][("artist_original", identity)] = ("asset:" + identity, 0, "", None, "", *_resource_values("artist_original", state, path))
    bound = or_(and_(Work.latest_revision_id.is_(None), Derivative.revision_id.is_(None)),
        select(RevisionDerivative.derivative_id).where(RevisionDerivative.revision_id == Work.latest_revision_id, RevisionDerivative.derivative_id == Derivative.id).exists())
    rows = session.execute(select(Derivative.work_id, Derivative.id, Derivative.kind, Derivative.state, Derivative.relative_path)
        .join(Work, Work.id == Derivative.work_id).where(Work.id.in_(ids), bound, Derivative.kind.in_(("thumbnail", "gif")), Derivative.page_index == 0, Derivative.spec_key == "v1"))
    for work_id, identity, kind, state, path in rows:
        scope = "artist_" + kind
        facts[work_id][(scope, identity)] = ("derivative:" + identity, 0, "", None, "", *_resource_values(scope, state, path))
    totals = {}
    for work_id, rows in facts.items():
        values = [0] * len(_TOTAL_FIELDS)
        for (scope, slot), row in rows.items():
            values = [a + b for a, b in zip(values, _contribution(scope, row[5:]))]
        totals[work_id] = (*values, *metadata[work_id])
    return facts, headers, totals


def _replace_normalized(connection, identity, facts, totals):
    connection.exec_driver_sql("DELETE FROM read_source_slot_facts WHERE work_id=?", (identity,))
    if facts:
        connection.exec_driver_sql("INSERT INTO read_source_slot_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(identity, scope, slot, *row) for (scope, slot), row in sorted(facts.items())])
    connection.exec_driver_sql("INSERT INTO read_source_local_totals VALUES(" + ",".join("?" for _ in range(14))
        + ") ON CONFLICT(work_id) DO UPDATE SET " + ",".join(name + "=excluded." + name for name in (*_TOTAL_FIELDS,
            "artist_required", "artist_ugoira", "artist_caption_missing", "artist_caption_unknown")), (identity, *totals))


def _refresh_summary(connection, identity):
    old = connection.exec_driver_sql("SELECT artist_id,included,artist_state,local FROM read_source_work_local WHERE work_id=?", (identity,)).first()
    if old is None:
        return
    row = connection.exec_driver_sql("SELECT " + ",".join((*_TOTAL_FIELDS, "artist_required", "artist_ugoira", "artist_caption_missing", "artist_caption_unknown")) + " FROM read_source_local_totals WHERE work_id=?", (identity,)).first()
    if row is None:
        raise ProjectionUnavailable("source slot totals are missing")
    detail_state, artist_state = _summary_states(row)
    local = json.loads(old[3])
    local["state"] = detail_state
    _artist_delta(connection, old, (old[0], old[1], artist_state))
    connection.exec_driver_sql("UPDATE read_source_work_local SET artist_state=?,local=? WHERE work_id=?",
        (artist_state, json.dumps(local, ensure_ascii=False, separators=(",", ":")), identity))


def _physical_deltas(connection, *, structural_ids=()):
    affected = set()
    for entity, table, prefix in (("physical_asset", "assets", "asset:"), ("physical_derivative", "derivatives", "derivative:")):
        identities = connection.exec_driver_sql("SELECT identity FROM read_model_dirty WHERE entity=?", (entity,)).scalars().all()
        for identity in identities:
            physical = connection.exec_driver_sql(f"SELECT state,relative_path FROM {table} WHERE id=?", (identity,)).first()
            rows = connection.exec_driver_sql("SELECT work_id,scope,slot,recorded,verified,missing,unknown,healthy FROM read_source_slot_facts WHERE physical_id=?", (prefix + identity,)).all()
            for work_id, scope, slot, *old in rows:
                if work_id in structural_ids:
                    continue
                values = _resource_values(scope, physical[0] if physical else None, physical[1] if physical else None, present=physical is not None)
                if tuple(old) == values:
                    continue
                delta = [a - b for a, b in zip(_contribution(scope, values), _contribution(scope, old))]
                connection.exec_driver_sql("UPDATE read_source_slot_facts SET recorded=?,verified=?,missing=?,unknown=?,healthy=? WHERE work_id=? AND scope=? AND slot=?", (*values, work_id, scope, slot))
                connection.exec_driver_sql("UPDATE read_source_local_totals SET " + ",".join(name + "=" + name + "+?" for name in _TOTAL_FIELDS) + " WHERE work_id=?", (*delta, work_id))
                affected.add(work_id)
    for identity in sorted(affected):
        _refresh_summary(connection, identity)
    return affected


def _reconcile_works(connection, work_ids, local_ids):
    ids = sorted(set(work_ids) | set(local_ids))
    for start in range(0, len(ids), 160):
        chunk = ids[start:start + 160]
        with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
            works = {work.id: work for work in session.scalars(select(Work).where(Work.id.in_(chunk)))}
            local_works = [work for work in works.values() if work.id in local_ids]
            facts, headers, totals = _normalized_local(session, local_works)
        classes = _classes(connection, "works", chunk)
        placeholders = ",".join("?" for _ in chunk)
        suppressed = set(connection.exec_driver_sql(f"SELECT w.id FROM works w JOIN archive_intents i ON i.pixiv_illust_id=w.pixiv_illust_id WHERE w.id IN ({placeholders}) AND i.mode='suppressed'", tuple(chunk)).scalars())
        for identity in chunk:
            _replace_entry(connection, "works", identity, classes.get(identity))
            old = connection.exec_driver_sql("SELECT artist_id,included,artist_state,local FROM read_source_work_local WHERE work_id=?", (identity,)).first()
            work = works.get(identity)
            if work is None:
                _artist_delta(connection, old, None)
                for table in ("read_source_work_local", "read_source_resource_users", "read_source_slot_facts", "read_source_local_totals"):
                    connection.exec_driver_sql(f"DELETE FROM {table} WHERE work_id=?", (identity,))
                continue
            if identity not in headers and old is None:
                raise ProjectionUnavailable("source local fact missing; explicit repair is required")
            if identity in headers:
                _replace_normalized(connection, identity, facts[identity], totals[identity])
                _replace_dependencies(connection, identity, _dependencies(connection, work))
                detail_state, state = _summary_states(totals[identity])
                local = json.dumps({"state": detail_state, **headers[identity]}, ensure_ascii=False, separators=(",", ":"))
            else:
                state, local = old[2], old[3]
            included = int(identity not in suppressed)
            _artist_delta(connection, old, (work.artist_id, included, state))
            connection.exec_driver_sql("INSERT INTO read_source_work_local VALUES(?,?,?,?,?,?) ON CONFLICT(work_id) DO UPDATE SET artist_id=excluded.artist_id,included=excluded.included,order_key=excluded.order_key,artist_state=excluded.artist_state,local=excluded.local",
                (identity, work.artist_id, included, work.posted_at.isoformat() if work.posted_at else "", state, local))


def maintain(connection, *, work_ids=()):
    rows = connection.exec_driver_sql("SELECT entity,identity FROM read_model_dirty WHERE entity IN ('source_work','source_local_work','source_artist')").all()
    dirty = {name: set() for name in DIRTY_ENTITIES}
    for entity, identity in rows:
        dirty[entity].add(int(identity))
    local = dirty["source_local_work"] | set(work_ids)
    _reconcile_works(connection, dirty["source_work"], local)
    physical_works = _physical_deltas(connection, structural_ids=local)
    artist_ids = sorted(dirty["source_artist"])
    for start in range(0, len(artist_ids), 200):
        ids = artist_ids[start:start + 200]
        classes = _classes(connection, "artists", ids)
        for identity in ids:
            _replace_entry(connection, "artists", identity, classes.get(identity))
    return {"source_works": len(dirty["source_work"] | local | physical_works), "source_artists": len(artist_ids)}


def rebuild(connection):
    """Explicit full candidate backfill; ordinary startup/read never calls this."""
    for table in TABLES:
        connection.exec_driver_sql(f"DELETE FROM {table}")
    connection.exec_driver_sql("INSERT INTO read_common_counts VALUES('artists',(SELECT count(*) FROM artists)) ON CONFLICT(scope) DO UPDATE SET value=excluded.value")
    last = None
    while True:
        ids = (connection.exec_driver_sql("SELECT id FROM works ORDER BY id LIMIT 160") if last is None
            else connection.exec_driver_sql("SELECT id FROM works WHERE id>? ORDER BY id LIMIT 160", (last,))).scalars().all()
        if not ids:
            break
        _reconcile_works(connection, ids, set(ids))
        last = ids[-1]
    for identity, fact in _classes(connection, "artists").items():
        _replace_entry(connection, "artists", identity, fact)


def counts(connection, account_id=None):
    buckets = ("*",) if account_id is None else ("null", "text:" + str(account_id))
    result = {kind: {category: 0 for category in ("deleted", "unavailable")} for kind in ("works", "artists")}
    placeholders = ",".join("?" for _ in buckets)
    # At most eight keyed counters, independent of entity count/account history.
    rows = connection.exec_driver_sql("SELECT kind,category,value FROM read_source_counts WHERE kind IN ('works','artists') AND bucket IN (" + placeholders + ") AND category IN ('deleted','unavailable')", buckets)
    for kind, category, value in rows:
        result[kind][category] += value
    return result


def page_ids(connection, *, kind="works", group="all", account_id=None, offset=0, limit=50):
    """First K is indexed; retained numeric offsets explicitly cost O(offset)."""
    category_sql, category_args = ("", ()) if group == "all" else (" AND category=?", (group,))
    if account_id is None:
        return connection.exec_driver_sql("SELECT entity_id FROM read_source_entries WHERE kind=?" + category_sql
            + " ORDER BY entity_id LIMIT ? OFFSET ?", (kind, *category_args, limit, offset)).scalars().all()
    branch = "SELECT entity_id FROM read_source_entries WHERE kind=? AND bucket=?" + category_sql
    sql = branch + " UNION ALL " + branch + " ORDER BY entity_id LIMIT ? OFFSET ?"
    return connection.exec_driver_sql(sql, (kind, "null", *category_args, kind, "text:" + str(account_id), *category_args, limit, offset)).scalars().all()


def _missing_item(role, page, kind):
    labels = {"ugoira_zip": "动图原包", "ugoira_timing": "动图播放时序", "thumbnail": "缩略图", "gif": "动图预览", "caption": "作品简介资料"}
    return {"role": role, "page": page, "label": f"第 {page + 1} 页原图" if role == "image" else labels[role], "kind": kind}


def work_details(connection, ids):
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = connection.exec_driver_sql("SELECT work_id,local FROM read_source_work_local WHERE work_id IN (" + placeholders + ")", tuple(ids))
    result = {row[0]: {**json.loads(row[1]), "missing": []} for row in rows}
    if len(result) != len(set(ids)):
        raise ProjectionUnavailable("source local summary is incomplete")
    # Only missing output rows are visited. A complete 10,000-page work costs
    # the same keyed lookup as a complete one-page work; full missing detail is
    # naturally output-sensitive and never suppressed to hide its real cost.
    for identity in ids:
        rows = connection.exec_driver_sql("SELECT role,page,kind FROM read_source_slot_facts WHERE work_id=? AND scope='detail' AND missing=1 ORDER BY position", (identity,))
        result[identity]["missing"] = [_missing_item(*row) for row in rows]
    return result


def artist_totals(connection, ids):
    if not ids:
        return {}
    return {row[0]: {"total": row[1], "incomplete": row[2], "unknown": row[3]}
        for row in connection.exec_driver_sql("SELECT artist_id,total,incomplete,unknown FROM read_source_artist_totals WHERE artist_id IN (" + ",".join("?" for _ in ids) + ")", tuple(ids))}


def artist_count(connection):
    row = connection.exec_driver_sql("SELECT value FROM read_common_counts WHERE scope='artists'").first()
    if row is None:
        raise ProjectionUnavailable("mandatory artist counter is missing; explicit repair is required")
    return row[0]


def recent_ids(connection, artist_id, limit=12):
    return connection.exec_driver_sql("SELECT work_id FROM read_source_work_local WHERE artist_id=? AND included=1 ORDER BY order_key DESC,work_id DESC LIMIT ?", (artist_id, limit)).scalars().all()


def verify(connection):
    """Explicit source-oracle reconciliation, never an ordinary read."""
    if artist_count(connection) != connection.exec_driver_sql("SELECT count(*) FROM artists").scalar():
        raise ProjectionUnavailable("artist catalogue count mismatch")
    expected_entries, expected_counts = {}, {}
    for kind in ("works", "artists"):
        for identity, fact in _classes(connection, kind).items():
            expected_entries[(kind, identity)] = fact
            for bucket in ("*", fact[0]):
                key = (kind, bucket, fact[1])
                expected_counts[key] = expected_counts.get(key, 0) + 1
    actual_entries = {(row[0], row[1]): tuple(row[2:]) for row in connection.exec_driver_sql("SELECT kind,entity_id,bucket,category FROM read_source_entries")}
    if actual_entries != expected_entries:
        raise ProjectionUnavailable("source classification membership mismatch")
    actual_counts = {tuple(row[:3]): row[3] for row in connection.exec_driver_sql("SELECT kind,bucket,category,value FROM read_source_counts")}
    if any(actual_counts.get(key, 0) != expected_counts.get(key, 0) for key in actual_counts.keys() | expected_counts.keys()):
        raise ProjectionUnavailable("source classification count mismatch")
    expected_totals, expected_dependencies = {}, set()
    ids = connection.exec_driver_sql("SELECT id FROM works ORDER BY id").scalars().all()
    for table in ("read_source_work_local", "read_source_local_totals"):
        if set(ids) != set(connection.exec_driver_sql(f"SELECT work_id FROM {table}").scalars()):
            raise ProjectionUnavailable("source local membership mismatch")
    if set(ids) != set(connection.exec_driver_sql("SELECT DISTINCT work_id FROM read_source_slot_facts").scalars()):
        raise ProjectionUnavailable("source slot membership mismatch")
    for start in range(0, len(ids), 160):
        chunk = ids[start:start + 160]
        with Session(bind=connection, autoflush=False) as session:
            works = list(session.scalars(select(Work).where(Work.id.in_(chunk))))
            details, flags = _local_details(session, works), _artist_flags(session, chunk)
            normalized, headers, totals = _normalized_local(session, works)
        for identity in chunk:
            actual_facts = {(row[0], row[1]): tuple(row[2:]) for row in connection.exec_driver_sql("SELECT scope,slot,physical_id,position,role,page,kind,recorded,verified,missing,unknown,healthy FROM read_source_slot_facts WHERE work_id=?", (identity,))}
            if actual_facts != normalized[identity]:
                raise ProjectionUnavailable("source normalized slot fact mismatch")
            actual_totals = connection.exec_driver_sql("SELECT " + ",".join((*_TOTAL_FIELDS, "artist_required", "artist_ugoira", "artist_caption_missing", "artist_caption_unknown")) + " FROM read_source_local_totals WHERE work_id=?", (identity,)).first()
            if actual_totals is None or tuple(actual_totals) != totals[identity]:
                raise ProjectionUnavailable("source normalized totals mismatch")
        if details != work_details(connection, chunk):
            raise ProjectionUnavailable("source latest local detail mismatch")
        placeholders = ",".join("?" for _ in chunk)
        suppressed = set(connection.exec_driver_sql(f"SELECT w.id FROM works w JOIN archive_intents i ON i.pixiv_illust_id=w.pixiv_illust_id WHERE w.id IN ({placeholders}) AND i.mode='suppressed'", tuple(chunk)).scalars())
        for work in works:
            expected_dependencies.update((value, work.id) for value in _dependencies(connection, work))
            expected = (work.artist_id, int(work.id not in suppressed), work.posted_at.isoformat() if work.posted_at else "", flags[work.id])
            actual = connection.exec_driver_sql("SELECT artist_id,included,order_key,artist_state FROM read_source_work_local WHERE work_id=?", (work.id,)).first()
            if actual is None or tuple(actual) != expected:
                raise ProjectionUnavailable("source artist/order fact mismatch")
            if work.id not in suppressed:
                total = expected_totals.setdefault(work.artist_id, [0, 0, 0])
                total[0] += 1
                total[1] += flags[work.id] == "incomplete"
                total[2] += flags[work.id] == "unknown"
    if expected_dependencies != set(connection.exec_driver_sql("SELECT physical_id,work_id FROM read_source_resource_users").all()):
        raise ProjectionUnavailable("source current resource dependency mismatch")
    actual_totals = {row[0]: list(row[1:]) for row in connection.exec_driver_sql("SELECT artist_id,total,incomplete,unknown FROM read_source_artist_totals")}
    if any(actual_totals.get(key, [0, 0, 0]) != expected_totals.get(key, [0, 0, 0]) for key in actual_totals.keys() | expected_totals.keys()):
        raise ProjectionUnavailable("source artist aggregate mismatch")
    return {"source_entries": len(expected_entries), "source_local_works": len(ids)}
