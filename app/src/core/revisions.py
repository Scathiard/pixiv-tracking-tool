"""Immutable work snapshots and explicit references to independently stored media.

The intent generation protects deletion. A work revision identifies what a reader
opened; it never follows changes to the source or the current catalogue projection.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

from sqlalchemy import select, func, case, and_

from .models import Asset, Derivative, RevisionAsset, RevisionDerivative, Work, WorkTag, WorkRevision, new_id


def signature(snapshot):
    payload = {key: snapshot.get(key) for key in ("title", "caption", "caption_state", "type", "page_count", "tags")}
    payload["urls"] = [urlsplit(url.get("original") or "").path for url in snapshot.get("source_metadata", {}).get("urls", [])]
    payload["ugoira"] = snapshot.get("source_metadata", {}).get("ugoira_source")
    payload["tag_translations"] = {name: {key: value for key, value in record.items() if key != "checked_at"}
        for name, record in snapshot.get("source_metadata", {}).get("tag_translations", {}).items()
        if isinstance(record, dict)}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def media_signature(snapshot):
    source = snapshot.get("source_metadata", {})
    ugoira = source.get("ugoira_source", {})
    return (snapshot.get("type"), snapshot.get("page_count"),
        tuple(urlsplit(url.get("original") or "").path for url in source.get("urls", [])),
        urlsplit(ugoira.get("zip_url") or "").path,
        json.dumps(ugoira.get("frames"), sort_keys=True, separators=(",", ":")))


def project_revision(work, revision):
    if revision is None:
        return work
    values = {key: value for key, value in work.__dict__.items() if key != "_sa_instance_state"}
    values.update({key: getattr(revision, key) for key in ("title", "caption", "caption_state", "type", "page_count")})
    values["source_metadata"] = {**(revision.source_metadata or {}),
        "remote_availability": (work.source_metadata or {}).get("remote_availability", {}),
        "original_backfill_requires_selection": (work.source_metadata or {}).get("original_backfill_requires_selection", False)}
    values["artist"] = work.artist
    return SimpleNamespace(**values)


def latest_revision(session, work_id):
    return session.scalar(select(WorkRevision).where(WorkRevision.work_id == work_id,
        WorkRevision.state != "redundant").order_by(WorkRevision.ordinal.desc()).limit(1))


def revision_assets(session, revision):
    if revision is None:
        return []
    return list(session.scalars(select(Asset).join(RevisionAsset, RevisionAsset.asset_id == Asset.id)
        .where(RevisionAsset.revision_id == revision.id).order_by(RevisionAsset.page_index, RevisionAsset.role)))


def revision_derivatives(session, revision):
    if revision is None:
        return []
    return list(session.scalars(select(Derivative).join(RevisionDerivative,
        RevisionDerivative.derivative_id == Derivative.id).where(RevisionDerivative.revision_id == revision.id)))


def asset_entries(session, revision):
    return list(session.execute(select(RevisionAsset, Asset).join(Asset, Asset.id == RevisionAsset.asset_id)
        .where(RevisionAsset.revision_id == revision.id).order_by(RevisionAsset.page_index, RevisionAsset.role)))


def revision_facts(session, revision):
    relevant = RevisionAsset.role.in_(("ugoira_zip", "ugoira_timing")) if revision.type == "ugoira" else and_(
        RevisionAsset.role == "image", RevisionAsset.page_index < revision.page_count)
    present = and_(Asset.relative_path.is_not(None), Asset.relative_path != "")
    verified, readable = session.execute(select(
        func.coalesce(func.sum(case((and_(present, Asset.state == "verified"), 1), else_=0)), 0),
        func.coalesce(func.sum(case((and_(present, Asset.state.in_(("verified", "unknown"))), 1), else_=0)), 0))
        .join(RevisionAsset, RevisionAsset.asset_id == Asset.id)
        .where(RevisionAsset.revision_id == revision.id, relevant)).one()
    expected = 2 if revision.type == "ugoira" else revision.page_count
    complete = verified == expected
    viewable = readable > 0
    if revision.type == "ugoira":
        viewable = bool(session.scalar(select(Derivative.id).join(RevisionDerivative,
            RevisionDerivative.derivative_id == Derivative.id).where(RevisionDerivative.revision_id == revision.id,
            RevisionDerivative.kind == "gif", Derivative.relative_path.is_not(None), Derivative.relative_path != "",
            Derivative.state.in_(("verified", "unknown"))).limit(1)))
    return {"complete": bool(complete), "viewable": bool(viewable),
        "state": "complete" if complete else "partial" if viewable else "pending"}


def selected_revision(session, work_id, revision_id=None):
    if revision_id is not None:
        revision = session.get(WorkRevision, revision_id)
        return revision if revision and revision.work_id == work_id and revision.state != "redundant" else None
    base = select(WorkRevision).where(WorkRevision.work_id == work_id, WorkRevision.state != "redundant")
    return session.scalar(base.order_by(case((and_(WorkRevision.state == "complete", WorkRevision.viewable.is_(True)), 2),
        (WorkRevision.viewable.is_(True), 1), else_=0).desc(), WorkRevision.ordinal.desc()).limit(1))


def bind_asset(session, revision, asset, *, role=None, page_index=None):
    session.flush()
    role, page = role or asset.role, asset.page_index if page_index is None else page_index
    key = (revision.id, role, page)
    binding = session.get(RevisionAsset, key)
    if binding is None:
        session.add(RevisionAsset(revision_id=revision.id, role=role, page_index=page, asset_id=asset.id))
    else:
        binding.asset_id = asset.id


def bind_derivative(session, revision, record):
    session.flush()
    key = (revision.id, record.kind, record.page_index, record.spec_key)
    binding = session.get(RevisionDerivative, key)
    if binding is None:
        session.add(RevisionDerivative(revision_id=revision.id, kind=record.kind,
            page_index=record.page_index, spec_key=record.spec_key, derivative_id=record.id))
    else:
        binding.derivative_id = record.id


def ensure_legacy_revision(session, work):
    revision = latest_revision(session, work.id)
    if revision:
        return revision
    snapshot = dict(title=work.title, caption=work.caption, caption_state=work.caption_state,
        type=work.type, page_count=work.page_count, tags=sorted(tag.tag for tag in work.tags),
        source_metadata=dict(work.source_metadata or {}))
    if work.type != "ugoira" and not snapshot["source_metadata"].get("urls"):
        originals = {asset.page_index: asset for asset in work.assets if asset.role == "image"}
        snapshot["source_metadata"]["urls"] = [{"original": originals[page].source_url or ""}
            if page in originals else {} for page in range(work.page_count)]
    revision = WorkRevision(id=new_id(), work_id=work.id, ordinal=1, source_signature=signature(snapshot),
        change_kind="legacy", **snapshot)
    session.add(revision); session.flush()
    for asset in list(work.assets):
        asset.revision_id = revision.id
        session.add(RevisionAsset(revision_id=revision.id, role=asset.role,
            page_index=asset.page_index, asset_id=asset.id))
    for record in list(work.derivatives):
        record.revision_id = revision.id
        bind_derivative(session, revision, record)
    refresh_revision(session, revision)
    return revision


def observe_revision(session, work, snapshot, *, compare_content=False):
    previous = latest_revision(session, work.id)
    digest = signature(snapshot)
    if previous and previous.source_signature == digest and not compare_content:
        return previous, False
    revision = WorkRevision(id=new_id(), work_id=work.id, ordinal=(previous.ordinal + 1 if previous else 1),
        source_signature=digest, change_kind="compare" if compare_content else "initial" if previous is None else "source",
        **snapshot)
    session.add(revision); session.flush()
    work.latest_revision_id = revision.id
    old_assets = {(entry.role, entry.page_index): asset for entry, asset in asset_entries(session, previous)} if previous else {}
    old_source = previous.source_metadata if previous else {}
    metadata_only = bool(previous and media_signature({**previous.__dict__}) == media_signature(snapshot)
        and not compare_content)
    if previous and previous.type != "ugoira" and previous.type == revision.type and previous.page_count == revision.page_count and not compare_content:
        old_urls = old_source.get("urls", [])
        new_urls = revision.source_metadata.get("urls", [])
        # A legacy unknown URL is no evidence of replacement. Record the first
        # source observation while retaining independently verified local bytes.
        metadata_only = all(not (old_urls[page].get("original") if page < len(old_urls) else None)
            or page < len(new_urls) and urlsplit(old_urls[page].get("original") or "").path == urlsplit(new_urls[page].get("original") or "").path
            for page in range(revision.page_count))
    if previous and previous.type == "ugoira" and revision.type == "ugoira" and not compare_content:
        prior_ugoira = old_source.get("ugoira_source", {})
        incoming = revision.source_metadata.get("ugoira_source", {})
        timing = old_assets.get(("ugoira_timing", 0))
        original = old_assets.get(("ugoira_zip", 0))
        incoming_hash = hashlib.sha256(json.dumps({"frames": incoming.get("frames")}, ensure_ascii=False).encode()).hexdigest()
        # Legacy databases did not retain a remote timing snapshot. A freshly
        # verified local timing digest establishes whether there is a real cue.
        if not prior_ugoira and incoming:
            metadata_only = bool(previous.page_count == revision.page_count
                and (not timing or not timing.sha256 or timing.sha256 == incoming_hash)
                and (not original or not original.source_url or urlsplit(original.source_url).path == urlsplit(incoming.get("zip_url") or "").path))
    roles = [("ugoira_zip", 0), ("ugoira_timing", 0)] if revision.type == "ugoira" else [("image", i) for i in range(revision.page_count)]
    urls = revision.source_metadata.get("urls", [])
    ugoira = revision.source_metadata.get("ugoira_source", {})
    new_assets, new_bindings = [], []
    for role, page in roles:
        requested = urls[page].get("original") if role == "image" and page < len(urls) else ugoira.get("zip_url") if role == "ugoira_zip" else ugoira.get("timing_url")
        old = old_assets.get((role, page))
        same = bool(old and metadata_only)
        if same:
            if requested and not old.source_url:
                old.source_url = requested
            new_bindings.append(RevisionAsset(revision_id=revision.id, role=role, page_index=page, asset_id=old.id))
        else:
            asset = Asset(id=new_id(), work_id=work.id, revision_id=revision.id, role=role, page_index=page,
                state="missing", source_url=requested)
            new_assets.append(asset)
            new_bindings.append(RevisionAsset(revision_id=revision.id, role=role, page_index=page, asset_id=asset.id))
    session.add_all(new_assets)
    session.flush()
    session.add_all(new_bindings)
    if metadata_only:
        revision.change_kind = "metadata"
        for record in revision_derivatives(session, previous):
            bind_derivative(session, revision, record)
    elif previous:
        revision.source_metadata = {**revision.source_metadata, "original_backfill_requires_selection": False}
    refresh_revision(session, revision)
    return revision, True


def refresh_revision(session, revision):
    session.flush()
    if revision.state != "redundant":
        facts = revision_facts(session, revision)
        revision.state, revision.viewable = facts["state"], facts["viewable"]
        session.flush()
        work = session.get(Work, revision.work_id)
        if work:
            latest = latest_revision(session, work.id)
            selected = selected_revision(session, work.id)
            work.latest_revision_id = latest.id if latest else None
            work.default_revision_id = selected.id if selected else None
            if selected:
                for key in ("title", "caption", "caption_state", "type", "page_count"):
                    setattr(work, key, getattr(selected, key))
                existing = {tag.tag: tag for tag in work.tags}
                work.tags = [existing[tag] if tag in existing else WorkTag(tag=tag) for tag in selected.tags]


def refresh_for_record(session, record):
    bindings = RevisionDerivative if isinstance(record, Derivative) else RevisionAsset
    column = bindings.derivative_id if isinstance(record, Derivative) else bindings.asset_id
    for revision_id in session.scalars(select(bindings.revision_id).where(column == record.id)):
        revision = session.get(WorkRevision, revision_id)
        if revision:
            refresh_revision(session, revision)


def revision_directory(work, revision, directory):
    return f"{directory}/{work.pixiv_illust_id}/revisions/{revision.id}" if revision else directory


def history(session, work_id):
    return [{"id": revision.id, "ordinal": revision.ordinal, "title": revision.title,
        "caption": revision.caption, "caption_state": revision.caption_state, "type": revision.type,
        "page_count": revision.page_count, "tags": revision.tags, "created_at": revision.created_at.isoformat(),
        "change_kind": revision.change_kind, **revision_facts(session, revision)}
        for revision in session.scalars(select(WorkRevision).where(WorkRevision.work_id == work_id,
            WorkRevision.state != "redundant").order_by(WorkRevision.ordinal.desc()))]
