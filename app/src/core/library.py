"""Local catalogue operations. No authentication, scheduler, or HTTP dependency."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import quote

from sqlalchemy import String, and_, case, cast, func, literal, or_, select
from sqlalchemy.orm import joinedload, load_only, selectinload

from ..security import sanitize_caption

from .archive_state import work_states
from .media import CARD_MOTION_SPEC
from .tag_translations import tag_details
from .source_status import ARTIST_PREFIX, availability_snapshot
from .catalogue_revision import catalogue_revision
from .revision_views import selected_snapshots, media_maps, apply_snapshot
from .models import (
    ApplicationState, ArchiveIntent, Artist, Asset, BookmarkOperation, Derivative, LocalPreference, RemoteBookmark,
    TrackingPolicy, Work, WorkTag, WorkRevision, RevisionAsset, RevisionDerivative, utcnow,
)


@dataclass(frozen=True)
class LibraryFilter:
    artist_ids: tuple[int, ...] | None = None
    types: tuple[str, ...] | None = None
    tags: tuple[str, ...] | None = None
    show_hidden: bool = False
    posted_after: datetime | None = None
    posted_before: datetime | None = None
    sort: str = "newest"
    q: str = ""
    favorites: bool = False
    recent: bool = False
    as_of: datetime | None = None
    artist_ids_exclude: tuple[int, ...] | None = None
    tags_exclude: tuple[str, ...] | None = None
    tz_offset_minutes: int = 0

    def __post_init__(self):
        if self.sort not in ("newest", "oldest", "added", "title"):
            raise ValueError("不支持的排序方式")
        if self.types is not None and any(t not in ("illust", "manga", "ugoira") for t in self.types):
            raise ValueError("不支持的作品类型")


def media_url(relative):
    return "/images/" + quote(relative, safe="/") if relative else None


def readable_page_url(record, *, animation=False):
    if record is None or not record.relative_path or record.state == "invalid":
        return None
    # A legacy GIF may exist before its archive/timing pair is verified. Keep it
    # readable while the originals are reported as a separate repair problem.
    if record.state == "missing" and not animation:
        return None
    return media_url(record.relative_path)


def remote_availability(work, account_id=None):
    value = (work.source_metadata or {}).get("remote_availability")
    return availability_snapshot(value, account_id)


class Library:
    def __init__(self, database):
        self.database = database
        self.account_id = None
        self.bookmarks = None

    @staticmethod
    def _statement(filters, ceiling):
        statement = select(Work).where(Work.id <= ceiling, ~Work.pixiv_illust_id.in_(
            select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed")))
        if not filters.show_hidden:
            statement = statement.where(~Work.id.in_(select(LocalPreference.work_id).where(LocalPreference.is_hidden.is_(True))))
        if filters.artist_ids is not None:
            statement = statement.where(Work.artist_id.in_(filters.artist_ids))
        if filters.artist_ids_exclude is not None:
            statement = statement.where(~Work.artist_id.in_(filters.artist_ids_exclude))
        if filters.types is not None:
            statement = statement.where(Work.type.in_(filters.types))
        if filters.tags is not None:
            statement = statement.where(Work.id.in_(select(WorkTag.work_id).where(WorkTag.tag.in_(filters.tags))))
        if filters.tags_exclude is not None:
            # Inverting tag choices still matches ANY selected tag. A work with
            # A+B remains in the inverse of A because its B tag is selected.
            statement = statement.where(Work.id.in_(select(WorkTag.work_id).where(~WorkTag.tag.in_(filters.tags_exclude))))
        if filters.q:
            term = filters.q.strip()
            if term:
                statement = statement.where(or_(Work.title.contains(term, autoescape=True),
                    Work.artist.has(Artist.name.contains(term, autoescape=True)),
                    Work.id.in_(select(WorkTag.work_id).where(WorkTag.tag.contains(term, autoescape=True)))))
        if filters.favorites:
            statement = statement.where(Work.id.in_(select(RemoteBookmark.work_id).where(
                RemoteBookmark.value.is_(True))))
        if filters.recent:
            reference = filters.as_of or utcnow()
            statement = statement.where(Work.downloaded_at >= reference - timedelta(days=30),
                                        Work.downloaded_at <= reference)
        if filters.posted_after is not None:
            utc_after = filters.posted_after + timedelta(minutes=filters.tz_offset_minutes)
            statement = statement.where(or_(and_(Work.time_basis == "legacy_unknown", Work.posted_at >= filters.posted_after),
                and_(Work.time_basis != "legacy_unknown", Work.posted_at >= utc_after)))
        if filters.posted_before is not None:
            utc_before = filters.posted_before + timedelta(minutes=filters.tz_offset_minutes)
            statement = statement.where(or_(and_(Work.time_basis == "legacy_unknown", Work.posted_at < filters.posted_before),
                and_(Work.time_basis != "legacy_unknown", Work.posted_at < utc_before)))
        return statement

    @staticmethod
    def _order(filters):
        if filters.sort == "title":
            return func.lower(Work.title).asc(), Work.id.asc()
        if filters.sort == "oldest":
            return Work.posted_at.asc(), Work.id.asc()
        if filters.sort == "added":
            return Work.downloaded_at.desc(), Work.id.desc()
        return Work.posted_at.desc(), Work.id.desc()

    @staticmethod
    def _related(statement):
        return statement.options(selectinload(Work.artist), selectinload(Work.tags),
                                 selectinload(Work.preference), selectinload(Work.bookmark))

    @staticmethod
    def _bookmark_operations(session, ids):
        if not ids:
            return {}
        ranked = select(BookmarkOperation.id.label("id"), BookmarkOperation.work_id.label("work_id"),
            func.row_number().over(partition_by=BookmarkOperation.work_id,
                order_by=(BookmarkOperation.version.desc(), BookmarkOperation.created_at.desc(),
                          BookmarkOperation.id.desc())).label("position")).where(
            BookmarkOperation.work_id.in_(ids)).subquery()
        return {operation.work_id: operation for operation in session.scalars(select(BookmarkOperation).join(
            ranked, BookmarkOperation.id == ranked.c.id).where(ranked.c.position == 1))}

    def _bookmark_view(self, work, operation=None):
        from .bookmarks import bookmark_snapshot
        if self.bookmarks is not None:
            return self.bookmarks.snapshot(work.bookmark, operation, work_id=work.id)
        return bookmark_snapshot(work.bookmark, operation,
                                 account_id=self.account_id, work_id=work.id)

    def _work_view(self, work, operation=None):
        bookmark = self._bookmark_view(work, operation)
        return {"id": work.id, "pixiv_id": work.pixiv_illust_id, "title": work.title,
            "artist_id": work.artist_id, "artist_name": work.artist.name,
            "type": work.type, "page_count": work.page_count,
            "caption": sanitize_caption(work.caption), "caption_state": work.caption_state,
            "tags": [tag.tag for tag in work.tags],
            "tag_details": tag_details([tag.tag for tag in work.tags], work.source_metadata),
            "is_hidden": bool(work.preference and work.preference.is_hidden),
            "rating": work.preference.rating if work.preference else 0,
            "is_bookmarked": bool(bookmark["value"]), "bookmark": bookmark,
            "bookmark_confirmed_at": bookmark["confirmed_at"],
            "bookmark_count": work.bookmark_count, "view_count": work.view_count,
            "posted_at": work.posted_at.isoformat() if work.posted_at else None,
            "time_basis": work.time_basis, "x_restrict": work.x_restrict,
            "availability": remote_availability(work, self.account_id),
            "restriction_verified": work.restriction_verified}

    def artists(self):
        # Existing HTML pages need the full selector, but no artwork previews.
        with self.database.read() as session:
            artists = list(session.scalars(select(Artist).options(selectinload(Artist.policy)).order_by(Artist.name, Artist.id)))
            availability = self._artist_availability(session, artists)
            return [{"id": artist.id, "pixiv_id": artist.pixiv_user_id, "name": artist.name,
                "account": artist.account, "avatar_url": media_url(artist.avatar_path),
                "mode": artist.policy.mode if artist.policy else "stopped",
                "availability": availability.get(artist.id),
                "added_at": artist.added_at.isoformat() if artist.added_at else None,
                "last_checked_at": artist.last_checked_at.isoformat() if artist.last_checked_at else None}
                for artist in artists]

    def artist_page(self, *, offset=0, limit=50, q=""):
        if offset < 0 or limit is not None and not 1 <= limit <= 100:
            raise ValueError("画师分页范围不合法")
        with self.database.read() as session:
            statement = select(Artist).options(selectinload(Artist.policy))
            if q.strip():
                statement = statement.where(or_(Artist.name.contains(q.strip(), autoescape=True),
                    Artist.account.contains(q.strip(), autoescape=True)))
            total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            statement = statement.order_by(Artist.name, Artist.id).offset(offset)
            if limit is not None:
                statement = statement.limit(limit)
            artists = list(session.scalars(statement))
            items = self._artist_views(session, artists)
            return {"items": items, "total": total, "offset": offset,
                    "limit": limit if limit is not None else total}

    def artist_detail(self, artist_id):
        with self.database.read() as session:
            artist = session.scalar(select(Artist).options(selectinload(Artist.policy)).where(Artist.id == artist_id))
            if artist is None:
                raise KeyError(artist_id)
            return self._artist_views(session, [artist])[0]

    def _artist_views(self, session, artists):
        ids = [artist.id for artist in artists]
        availability = self._artist_availability(session, artists)
        counts = dict(session.execute(select(Work.artist_id, func.count()).where(
            Work.artist_id.in_(ids), ~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(
                ArchiveIntent.mode == "suppressed"))).group_by(Work.artist_id)).all()) if ids else {}
        ranked = select(Work.id.label("id"), Work.artist_id.label("artist_id"),
            Work.pixiv_illust_id.label("pixiv_id"), Work.title.label("title"),
            Work.type.label("type"), Work.page_count.label("page_count"), Work.x_restrict.label("x_restrict"),
            func.row_number().over(partition_by=Work.artist_id,
                order_by=(Work.posted_at.desc(), Work.id.desc())).label("position")).where(
            Work.artist_id.in_(ids), ~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(
                ArchiveIntent.mode == "suppressed"))).subquery()
        recent_rows = list(session.execute(select(ranked).where(ranked.c.position <= 12).order_by(
            ranked.c.artist_id, ranked.c.position)).mappings()) if ids else []
        work_ids = [row["id"] for row in recent_rows]
        preview_works = list(session.scalars(select(Work).options(load_only(Work.id, Work.default_revision_id,
            Work.latest_revision_id)).where(Work.id.in_(work_ids)))) if work_ids else []
        snapshots = selected_snapshots(session, preview_works)
        originals, derived = media_maps(session, preview_works, snapshots, ranges={work.id: (0, 1) for work in preview_works})
        thumbs = {work_id: derived.get((work_id, "thumbnail", 0, "v1")) for work_id in work_ids}
        recent = {artist_id: [] for artist_id in ids}
        for row in recent_rows:
            thumb = thumbs.get(row["id"])
            if thumb is not None and thumb.state != "verified":
                thumb = None
            url = media_url(thumb.relative_path) if thumb else readable_page_url(originals.get((row["id"], "image", 0)))
            if url and thumb and thumb.sha256:
                url += "?v=" + thumb.sha256[:16]
            recent[row["artist_id"]].append({"id": row["id"], "pixiv_id": row["pixiv_id"],
                "title": row["title"], "type": row["type"], "page_count": row["page_count"],
                "x_restrict": row["x_restrict"],
                "thumbnail": url})
        return [{"id": artist.id, "pixiv_id": artist.pixiv_user_id, "name": artist.name,
            "account": artist.account, "avatar_url": media_url(artist.avatar_path),
            "avatar": media_url(artist.avatar_path), "biography": artist.biography,
            "availability": availability.get(artist.id),
            "profile_refreshed_at": artist.profile_refreshed_at.isoformat() if artist.profile_refreshed_at else None,
            "mode": artist.policy.mode if artist.policy else "stopped",
            "added_at": artist.added_at.isoformat() if artist.added_at else None,
            "last_checked_at": artist.last_checked_at.isoformat() if artist.last_checked_at else None,
            "work_count": counts.get(artist.id, 0), "recent_works": recent[artist.id]}
            for artist in artists]

    def set_tracking(self, artist_id, mode):
        if mode not in ("active", "paused", "stopped"):
            raise ValueError("无效的追踪状态")
        with self.database.write() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                raise KeyError(artist_id)
            if artist.policy is None:
                artist.policy = TrackingPolicy(mode=mode)
            else:
                artist.policy.mode, artist.policy.updated_at = mode, utcnow()

    def set_preferences(self, work_id, *, hidden=None, rating=None):
        if rating is not None and (type(rating) is not int or not 0 <= rating <= 5):
            raise ValueError("评分必须为 0 到 5")
        with self.database.write() as session:
            work = session.get(Work, work_id)
            if work is None:
                raise KeyError(work_id)
            if work.preference is None:
                work.preference = LocalPreference(is_hidden=False, rating=0)
            if hidden is not None:
                work.preference.is_hidden = bool(hidden)
            if rating is not None:
                work.preference.rating = rating

    def query(self, filters=LibraryFilter(), *, offset=0, limit=100, ceiling=None, compact=False):
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("分页范围不合法")
        with self.database.read() as session:
            ceiling = ceiling if ceiling is not None else session.scalar(select(func.max(Work.id))) or 0
            statement = self._statement(filters, ceiling)
            total = session.scalar(select(func.count()).select_from(statement.subquery()))
            related = statement.options(load_only(Work.id, Work.pixiv_illust_id, Work.artist_id, Work.title,
                Work.type, Work.page_count, Work.posted_at, Work.x_restrict, Work.source_metadata,
                Work.default_revision_id, Work.latest_revision_id), selectinload(Work.artist).load_only(Artist.name),
                selectinload(Work.preference), selectinload(Work.bookmark)) if compact else self._related(statement)
            works = list(session.scalars(related.order_by(*self._order(filters)).offset(offset).limit(limit)))
            snapshots = selected_snapshots(session, works)
            operations = self._bookmark_operations(session, [work.id for work in works])
            views = [{"id": work.id, "pixiv_id": work.pixiv_illust_id, "artist_id": work.artist_id,
                      "artist_name": work.artist.name, "title": work.title, "type": work.type,
                      "page_count": work.page_count, "x_restrict": work.x_restrict,
                      "is_hidden": bool(work.preference and work.preference.is_hidden),
                      "posted_at": work.posted_at.isoformat() if work.posted_at else None,
                      "bookmark": self._bookmark_view(work, operations.get(work.id)),
                      "availability": remote_availability(work, self.account_id)} for work in works] if compact else [self._work_view(work, operations.get(work.id)) for work in works]
            if compact:
                for view in views:
                    view["is_bookmarked"] = bool(view["bookmark"]["value"])
            for work, view in zip(works, views):
                apply_snapshot(view, work, snapshots.get(work.id))
                if compact:
                    for name in ("caption", "caption_state", "tags", "tag_details"):
                        view.pop(name, None)
            ids = [work.id for work in works]
            animation_ids = [work.id for work in works if work.type == "ugoira"]
            states, revision = work_states(session, works)
            assets, derived = media_maps(session, works, snapshots, ranges={work.id: (0, 1) for work in works})
            thumbnails = {work.id: derived.get((work.id, "thumbnail", 0, "v1")) for work in works}
            first_pages = {work.id: assets.get((work.id, "image", 0)) for work in works}
            animations = {work_id: derived.get((work_id, "gif", 0, "v1")) for work_id in animation_ids}
            card_animations = {work_id: derived.get((work_id, "thumbnail", 0, CARD_MOTION_SPEC)) for work_id in animation_ids}
            for view in views:
                view.update(states[view["id"]])
                thumb = thumbnails.get(view["id"])
                if thumb is not None and thumb.state != "verified":
                    thumb = None
                preview = animations.get(view["id"]) if view["type"] == "ugoira" else first_pages.get(view["id"])
                if compact and view["type"] == "ugoira":
                    source = preview
                    preview = card_animations.get(view["id"])
                    if (preview is None or preview.state != "verified" or not preview.sha256 or source is None
                            or source.state != "verified" or not source.sha256 or preview.source_version != source.sha256):
                        preview = None
                view["preview"] = readable_page_url(preview, animation=view["type"] == "ugoira")
                if compact and view["type"] == "ugoira" and view["preview"]:
                    view["preview"] += "?v=" + preview.sha256[:16]
                if view["preview"]:
                    view["preview_status"] = "ready"
                elif preview and preview.state == "failed":
                    view["preview_status"] = "failed"
                elif preview and preview.state in ("invalid", "mismatch"):
                    view["preview_status"] = "unavailable"
                else:
                    view["preview_status"] = "pending"
                view["thumbnail"] = media_url(thumb.relative_path) if thumb else None
                if view["thumbnail"] and thumb.sha256:
                    view["thumbnail"] += "?v=" + thumb.sha256[:16]
            return {"items": views, "total": total, "offset": offset, "has_more": offset + len(views) < total,
                    "ceiling": ceiling, "catalogue_revision": revision}

    def counts(self, filters=LibraryFilter(), *, ceiling=None):
        """Counts do not hydrate any artwork, assets, or processing history."""
        with self.database.read() as session:
            ceiling = ceiling if ceiling is not None else session.scalar(select(func.max(Work.id))) or 0
            result = {"ceiling": ceiling, "as_of": (filters.as_of or utcnow()).isoformat(),
                      "catalogue_revision": catalogue_revision(session)}
            base = replace(filters, favorites=False, recent=False)
            for name, selected in (("all", base), ("recent", replace(base, recent=True)),
                    ("favorites", replace(base, favorites=True)), ("total", filters)):
                statement = self._statement(selected, ceiling).with_only_columns(Work.id)
                result[name] = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            return result

    def reading_manifest(self, filters=LibraryFilter(), *, ceiling=None):
        with self.database.read() as session:
            ceiling = ceiling if ceiling is not None else session.scalar(select(func.max(Work.id))) or 0
            pages = case((Work.type == "ugoira", 1), else_=Work.page_count)
            statement = self._statement(filters, ceiling).with_only_columns(Work.id,
                Work.default_revision_id, pages).order_by(*self._order(filters))
            return [{"work_id": row[0], "revision_id": row[1], "page_count": max(1, row[2])}
                    for row in session.execute(statement)]

    @staticmethod
    def _revision_archive(session, snapshot):
        total = 2 if snapshot.type == "ugoira" else snapshot.page_count
        required = RevisionAsset.role.in_(("ugoira_zip", "ugoira_timing")) if snapshot.type == "ugoira" else and_(
            RevisionAsset.role == "image", RevisionAsset.page_index < snapshot.page_count)
        bad = or_(Asset.state.in_(("missing", "failed", "invalid", "mismatch", "corrupt")),
                  Asset.relative_path.is_(None), Asset.relative_path == "")
        count, verified, missing = session.execute(select(func.count(), func.sum(case((and_(Asset.state == "verified", ~bad), 1), else_=0)),
            func.sum(case((bad, 1), else_=0))).select_from(RevisionAsset).join(Asset, Asset.id == RevisionAsset.asset_id).where(
                RevisionAsset.revision_id == snapshot.id, required)).one()
        missing = (missing or 0) + max(0, total - count)
        verified = verified or 0
        unknown = max(0, total - verified - missing)
        return {"state": "partial" if missing else "unknown" if unknown else "complete",
                "total": total, "verified": verified, "missing": missing, "unknown": unknown}

    def reading_window(self, requests):
        """Materialize a bounded set of immutable-version image slots in batches."""
        if sum(request["page_limit"] for request in requests) > 120:
            raise ValueError("阅读窗口过大")
        ids = list({request["work_id"] for request in requests})
        with self.database.read() as session:
            works = {work.id: work for work in session.scalars(self._related(self._statement(
                LibraryFilter(show_hidden=True), 2**63 - 1)).where(Work.id.in_(ids)))}
            explicit = {request["work_id"]: request["revision_id"] for request in requests}
            snapshots = selected_snapshots(session, works.values(), explicit)
            ranges = {}
            for request in requests:
                start, end = request["page_offset"], request["page_offset"] + request["page_limit"]
                if request["work_id"] in ranges:
                    first, last = ranges[request["work_id"]]
                    start, end = min(first, start), max(last, end)
                ranges[request["work_id"]] = (start, end)
            assets, derived = media_maps(session, works.values(), snapshots, ranges=ranges)
            states, revision = work_states(session, works.values())
            operations = self._bookmark_operations(session, ids)
            views = {work.id: apply_snapshot(self._work_view(work, operations.get(work.id)), work,
                snapshots.get(work.id)) for work in works.values()}
            items = []
            for request in requests:
                work = works.get(request["work_id"])
                if work is None:
                    continue
                snapshot = snapshots.get(work.id)
                if request["revision_id"] is not None and (snapshot is None or snapshot.id != request["revision_id"]):
                    continue
                view = views[work.id]
                count = 1 if view["type"] == "ugoira" else view["page_count"]
                state = states[work.id]
                if snapshot and snapshot.id != work.default_revision_id:
                    state = {**state, "archive": self._revision_archive(session, snapshot)}
                for page in range(request["page_offset"], min(count, request["page_offset"] + request["page_limit"])):
                    record = derived.get((work.id, "gif", 0, "v1")) if view["type"] == "ugoira" else assets.get((work.id, "image", page))
                    items.append({key: view[key] for key in ("title", "artist_id", "artist_name", "pixiv_id", "type", "x_restrict", "bookmark", "availability", "revision_id")} |
                        {"index": request["index"] + page - request["page_offset"], "work_id": work.id,
                         "work_index": request["work_index"], "page": page, "page_count": count,
                         "round": request["round"], "next_work_index": request["next_work_index"],
                         "previous_work_index": request["previous_work_index"], **state,
                         "src": readable_page_url(record, animation=view["type"] == "ugoira"),
                         "width": record.width if record else None, "height": record.height if record else None,
                         "state": record.state if record else "missing"})
            return {"items": items, "catalogue_revision": revision}

    def detail(self, work_id, *, page_offset=0, page_limit=100, revision_id=None):
        if page_offset < 0 or not 1 <= page_limit <= 200:
            raise ValueError("页面范围不合法")
        with self.database.read() as session:
            work = session.scalar(self._related(self._statement(LibraryFilter(show_hidden=True), work_id)).where(Work.id == work_id))
            if work is None:
                raise KeyError(work_id)
            snapshots = selected_snapshots(session, [work], {work_id: revision_id} if revision_id else None)
            snapshot = snapshots.get(work_id)
            if revision_id is not None and (snapshot is None or snapshot.state == "redundant"):
                raise KeyError(revision_id)
            view = self._work_view(work, self._bookmark_operations(session, [work.id]).get(work.id))
            apply_snapshot(view, work, snapshot)
            states, revision = work_states(session, [work])
            view.update(states[work_id])
            view["catalogue_revision"] = revision
            count = 1 if view["type"] == "ugoira" else view["page_count"]
            originals, derived = media_maps(session, [work], snapshots, ranges={work.id: (page_offset, page_offset + page_limit)})
            preview = derived.get((work_id, "gif", 0, "v1"))
            if snapshot is not None:
                view["archive"] = self._revision_archive(session, snapshot)
            view["revisions"] = [{"id": row.id, "ordinal": row.ordinal, "created_at": row.created_at.isoformat(),
                "change_kind": row.change_kind, "state": row.state, "page_count": row.page_count,
                "current": row.id == work.default_revision_id} for row in session.scalars(select(WorkRevision).where(
                    WorkRevision.work_id == work_id, WorkRevision.state != "redundant").order_by(WorkRevision.ordinal.desc()))]
            view["pages"] = []
            for index in range(page_offset, min(count, page_offset + page_limit)):
                record = preview if view["type"] == "ugoira" else originals.get((work_id, "image", index))
                view["pages"].append({"index": index, "src": readable_page_url(record, animation=view["type"] == "ugoira"),
                    "width": record.width if record else None, "height": record.height if record else None,
                    "state": record.state if record else "missing", "error": record.error if record else None})
            view["assets"] = [{"id": asset.id, "role": role, "page": page,
                               "state": asset.state, "error": asset.error} for (_, role, page), asset in originals.items()]
            alternates = [record for (_, kind, _, _), record in derived.items() if kind == "legacy_alternate"]
            variant_pages = sorted({record.page_index for record in alternates})
            variant_primaries = {page: record for (_, role, page), record in originals.items() if role == "image" and page in variant_pages}
            view["legacy_variants"] = []
            for index in variant_pages:
                primary = variant_primaries.get(index)
                files = []
                if primary and primary.relative_path:
                    files.append({"src": readable_page_url(primary),
                                  "name": PurePosixPath(primary.relative_path).name, "default": True})
                files.extend({"src": readable_page_url(record),
                              "name": PurePosixPath(record.relative_path).name, "default": False}
                             for record in alternates if record.page_index == index and record.relative_path)
                view["legacy_variants"].append({"page": index, "files": files})
            view["display_page_count"], view["has_more_pages"] = count, page_offset + len(view["pages"]) < count
            return view

    def neighbors(self, work_id, filters=LibraryFilter(), *, ceiling):
        with self.database.read() as session:
            statement = self._statement(filters, ceiling)
            ranked = statement.with_only_columns(Work.id,
                func.row_number().over(order_by=self._order(filters)).label("position")).subquery()
            position = session.scalar(select(ranked.c.position).where(ranked.c.id == work_id))
            if position is None:
                raise KeyError(work_id)
            nearby = dict(session.execute(select(ranked.c.position, ranked.c.id).where(
                ranked.c.position.in_((position - 1, position + 1)))).all())
            return {"previous": nearby.get(position - 1), "next": nearby.get(position + 1), "index": position - 1}

    def tags(self, filters=LibraryFilter(), *, ceiling=None):
        with self.database.read() as session:
            ceiling = ceiling if ceiling is not None else session.scalar(select(func.max(Work.id))) or 0
            selected = self._statement(filters, ceiling).with_only_columns(Work.id)
            return list(session.scalars(select(WorkTag.tag).where(WorkTag.work_id.in_(selected)).distinct().order_by(WorkTag.tag)))

    def _page_ranges(self, filters, ceiling):
        pages = case((Work.type == "ugoira", 1), else_=Work.page_count)
        order = self._order(filters)
        return self._statement(filters, ceiling).with_only_columns(Work.id.label("work_id"),
            pages.label("page_count"), (func.row_number().over(order_by=order) - 1).label("work_index"),
            (func.sum(pages).over(order_by=order, rows=(None, 0)) - pages).label("page_start")).subquery()

    def locate(self, work_id, filters=LibraryFilter(), *, ceiling, page=0):
        """Resolve an identity anchor in the exact gallery/feed ordering."""
        with self.database.read() as session:
            ranges = self._page_ranges(filters, ceiling)
            row = session.execute(select(ranges).where(ranges.c.work_id == work_id)).mappings().first()
            if row is None:
                raise KeyError(work_id)
            page = max(0, min(page, row["page_count"] - 1))
            return {"work_id": work_id, "work_index": row["work_index"], "page": page,
                    "index": row["page_start"] + page, "next_work_index": row["page_start"] + row["page_count"]}

    def feed(self, filters=LibraryFilter(), *, offset=0, limit=48, ceiling=None):
        """Bounded image-page window, including missing pages and very long works.

        SQL computes ranges; neither all IDs nor all pages of one work are loaded
        into Python. The browser can slide this window across the whole library.
        """
        if offset < 0 or not 1 <= limit <= 120:
            raise ValueError("流式分页范围不合法")
        with self.database.read() as session:
            ceiling = ceiling if ceiling is not None else session.scalar(select(func.max(Work.id))) or 0
            ranges = self._page_ranges(filters, ceiling)
            total = session.scalar(select(func.max(ranges.c.page_start + ranges.c.page_count))) or 0
            rows = list(session.execute(select(ranges).where(ranges.c.page_start < offset + limit,
                ranges.c.page_start + ranges.c.page_count > offset).order_by(ranges.c.page_start)).mappings())
            ids = [row["work_id"] for row in rows]
            works = {work.id: work for work in session.scalars(select(Work).options(
                load_only(Work.id, Work.pixiv_illust_id, Work.artist_id, Work.title, Work.type, Work.page_count, Work.x_restrict,
                          Work.source_metadata, Work.default_revision_id, Work.latest_revision_id),
                joinedload(Work.artist).load_only(Artist.name), joinedload(Work.bookmark)).where(Work.id.in_(ids)))}
            operations = self._bookmark_operations(session, ids)
            states, revision = work_states(session, works.values())
            bookmark_views = {work_id: self._bookmark_view(work, operations.get(work_id))
                              for work_id, work in works.items()}
            snapshots = selected_snapshots(session, works.values())
            originals, previews = media_maps(session, works.values(), snapshots, ranges={row["work_id"]:
                (max(0, offset - row["page_start"]), min(row["page_count"], offset + limit - row["page_start"])) for row in rows})
            items = []
            for row in rows:
                work = works[row["work_id"]]
                for page in range(max(0, offset - row["page_start"]), min(row["page_count"], offset + limit - row["page_start"])):
                    media = previews.get((work.id, "gif", 0, "v1")) if work.type == "ugoira" else originals.get((work.id, "image", page))
                    items.append({"index": row["page_start"] + page, "work_id": work.id, "work_index": row["work_index"],
                        "page": page, "page_count": row["page_count"], "title": work.title,
                        "revision_id": snapshots[work.id].id if work.id in snapshots else None,
                        "artist_name": work.artist.name, "artist_id": work.artist_id, "pixiv_id": work.pixiv_illust_id,
                        "type": work.type, "x_restrict": work.x_restrict,
                        "next_work_index": row["page_start"] + row["page_count"],
                        "bookmark": bookmark_views[work.id],
                        "availability": remote_availability(work, self.account_id),
                        **states[work.id],
                        "src": readable_page_url(media, animation=work.type == "ugoira"),
                        "width": media.width if media else None, "height": media.height if media else None,
                        "state": media.state if media else "missing"})
            return {"items": items, "total": total, "offset": offset, "ceiling": ceiling,
                    "has_more": offset + len(items) < total, "catalogue_revision": revision}

    def missing_originals(self, offset=0, limit=100):
        with self.database.read() as session:
            latest_type = func.coalesce(WorkRevision.type, Work.type)
            bound = or_(and_(Work.latest_revision_id.is_(None), Asset.revision_id.is_(None)),
                select(RevisionAsset.asset_id).where(RevisionAsset.revision_id == Work.latest_revision_id,
                    RevisionAsset.asset_id == Asset.id).exists())
            gaps = select(Asset.id).where(Asset.work_id == Work.id, bound,
                Asset.role.in_(("ugoira_zip", "ugoira_timing")), Asset.state != "verified").exists()
            statement = self._statement(LibraryFilter(show_hidden=True), 2**63 - 1).outerjoin(
                WorkRevision, WorkRevision.id == Work.latest_revision_id).where(latest_type == "ugoira", gaps)
            works = list(session.scalars(self._related(statement).order_by(Work.id).offset(max(0, offset)).limit(min(max(1, limit), 100))))
            operations = self._bookmark_operations(session, [work.id for work in works])
            states, _ = work_states(session, works)
            return [{**self._work_view(work, operations.get(work.id)), **states[work.id]} for work in works]

    def _artist_availability(self, session, artists):
        keys = {ARTIST_PREFIX + artist.pixiv_user_id: artist.id for artist in artists}
        if not keys:
            return {}
        return {keys[row.key]: availability_snapshot(row.value, self.account_id)
                for row in session.scalars(select(ApplicationState).where(ApplicationState.key.in_(keys)))}

    def _source_statement(self, kind):
        if kind == "works":
            statement = select(Work).where(~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(
                ArchiveIntent.mode == "suppressed")))
            value = Work.source_metadata["remote_availability"]
        else:
            statement = select(Artist).join(ApplicationState,
                ApplicationState.key == literal(ARTIST_PREFIX) + Artist.pixiv_user_id)
            value = ApplicationState.value
        state = value["state"].as_string()
        scoped = value["account_id"].as_string()
        account_matches = literal(True) if self.account_id is None else or_(scoped.is_(None), scoped == str(self.account_id))
        deleted = and_(state == "deleted", value["confirmed"].as_boolean().is_(True),
                       value["deletion_evidence"].as_boolean().is_(True), account_matches)
        unavailable = and_(state.in_(("unavailable", "private", "deleted")), ~func.coalesce(deleted, False),
                           value["confirmed"].as_boolean().is_(True), value["policy"].as_string() == "source-v2", account_matches)
        return statement, deleted, unavailable

    @staticmethod
    def _local_work(work, assets, derivatives):
        assets = {(item.role, item.page_index): item for item in assets}
        derivatives = {(item.kind, item.page_index, item.spec_key): item for item in derivatives}
        required = [("ugoira_zip", 0), ("ugoira_timing", 0)] if work.type == "ugoira" else [("image", page) for page in range(work.page_count)]
        missing, unknown = [], False
        bad = ("missing", "failed", "invalid", "mismatch", "corrupt")
        for role, page in required:
            record = assets.get((role, page))
            if record is None or record.state in bad or not record.relative_path:
                label = f"第 {page + 1} 页原图" if role == "image" else "动图原包" if role == "ugoira_zip" else "动图播放时序"
                missing.append({"role": role, "page": page if role == "image" else None,
                                "label": label, "kind": "original"})
            elif record.state != "verified":
                unknown = True
        required_derivatives = [("thumbnail", 0, "v1", "缩略图")]
        if work.type == "ugoira":
            required_derivatives.append(("gif", 0, "v1", "动图预览"))
        for role, page, spec, label in required_derivatives:
            record = derivatives.get((role, page, spec))
            if record is None or record.state in bad or not record.relative_path:
                missing.append({"role": role, "page": None, "label": label, "kind": "derivative"})
            elif record.state != "verified":
                unknown = True
        if work.caption_state in ("unavailable", "missing", "failed"):
            missing.append({"role": "caption", "page": None, "label": "作品简介资料", "kind": "metadata"})
        elif work.caption_state not in ("available", "empty"):
            unknown = True
        return {"state": "incomplete" if missing else "unknown" if unknown else "complete", "missing": missing}

    @staticmethod
    def _artist_local_summaries(session, artists):
        ids = [artist.id for artist in artists]
        if not ids:
            return {}
        # Aggregate local work facts in SQL. No per-artist/per-work queries or
        # loading every image-page record just to obtain two summary counts.
        current_type, current_pages = func.coalesce(WorkRevision.type, Work.type), func.coalesce(WorkRevision.page_count, Work.page_count)
        asset_bound = or_(and_(Work.latest_revision_id.is_(None), Asset.revision_id.is_(None)),
            select(RevisionAsset.asset_id).where(RevisionAsset.revision_id == Work.latest_revision_id,
                RevisionAsset.asset_id == Asset.id).exists())
        required = and_(asset_bound, or_(and_(current_type != "ugoira", Asset.role == "image", Asset.page_index < current_pages),
            and_(current_type == "ugoira", Asset.role.in_(("ugoira_zip", "ugoira_timing")), Asset.page_index == 0)))
        counts = select(Asset.work_id.label("work_id"), func.count().label("recorded"),
            func.sum(case((and_(Asset.state == "verified", func.length(Asset.relative_path) > 0), 1), else_=0)).label("verified"),
            func.sum(case((or_(Asset.state.in_(("missing", "failed", "invalid", "mismatch", "corrupt")),
                                  Asset.relative_path.is_(None), Asset.relative_path == ""), 1), else_=0)).label("missing")
            ).join(Work, Work.id == Asset.work_id).outerjoin(WorkRevision, WorkRevision.id == Work.latest_revision_id).where(Work.artist_id.in_(ids), required).group_by(Asset.work_id).subquery()
        total = case((current_type == "ugoira", 2), else_=current_pages)
        derivative_bound = or_(and_(Work.latest_revision_id.is_(None), Derivative.revision_id.is_(None)),
            select(RevisionDerivative.derivative_id).where(RevisionDerivative.revision_id == Work.latest_revision_id,
                RevisionDerivative.derivative_id == Derivative.id).exists())
        thumbnail_base = select(Derivative.id).where(Derivative.work_id == Work.id, Derivative.kind == "thumbnail",
            Derivative.page_index == 0, Derivative.spec_key == "v1", func.length(Derivative.relative_path) > 0, derivative_bound)
        gif_base = select(Derivative.id).where(Derivative.work_id == Work.id, Derivative.kind == "gif", Derivative.page_index == 0,
            Derivative.spec_key == "v1", func.length(Derivative.relative_path) > 0, derivative_bound)
        bad = ("missing", "failed", "invalid", "mismatch", "corrupt")
        thumbnail = thumbnail_base.where(~Derivative.state.in_(bad)).exists()
        gif = gif_base.where(~Derivative.state.in_(bad)).exists()
        thumbnail_verified = thumbnail_base.where(Derivative.state == "verified").exists()
        gif_verified = gif_base.where(Derivative.state == "verified").exists()
        incomplete = or_(func.coalesce(counts.c.recorded, 0) < total, func.coalesce(counts.c.missing, 0) > 0,
                         ~thumbnail, and_(current_type == "ugoira", ~gif), Work.caption_state.in_(("unavailable", "missing", "failed")))
        unknown = or_(func.coalesce(counts.c.verified, 0) < total, ~Work.caption_state.in_(("available", "empty")),
                      ~thumbnail_verified, and_(current_type == "ugoira", ~gif_verified))
        rows = session.execute(select(Work.artist_id, func.count().label("total"),
            func.sum(case((incomplete, 1), else_=0)).label("incomplete"),
            func.sum(case((and_(~incomplete, unknown), 1), else_=0)).label("unknown")
            ).outerjoin(counts, counts.c.work_id == Work.id).outerjoin(WorkRevision, WorkRevision.id == Work.latest_revision_id).where(Work.artist_id.in_(ids),
            ~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed")))
            .group_by(Work.artist_id)).all()
        counts_by_artist = {row.artist_id: row for row in rows}
        result = {}
        for artist in artists:
            row = counts_by_artist.get(artist.id)
            incomplete_count, unknown_count = (row.incomplete or 0, row.unknown or 0) if row else (0, 0)
            missing = []
            if incomplete_count:
                missing.append({"role": "saved_works", "page": None,
                                "label": f"{incomplete_count} 件已保存作品有本地缺项", "kind": "original"})
            if artist.avatar_url and not artist.avatar_path:
                missing.append({"role": "avatar", "page": None, "label": "画师头像缓存", "kind": "derivative"})
            unknown = bool(unknown_count or artist.profile_refreshed_at is None)
            result[artist.id] = {"work_count": row.total if row else 0, "incomplete_work_count": incomplete_count,
                "local": {"state": "incomplete" if missing else "unknown" if unknown else "complete", "missing": missing}}
        return result

    def source_overview(self, *, kind="works", group="all", offset=0, limit=50):
        if kind not in ("works", "artists") or group not in ("all", "deleted", "unavailable"):
            raise ValueError("来源一览筛选不合法")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("来源一览分页不合法")
        with self.database.read() as session:
            counts = {}
            for category in ("works", "artists"):
                base, deleted, unavailable = self._source_statement(category)
                count = session.execute(base.with_only_columns(func.sum(case((deleted, 1), else_=0)),
                    func.sum(case((unavailable, 1), else_=0)))).one()
                counts[category] = {"deleted": count[0] or 0, "unavailable": count[1] or 0}
            base, deleted, unavailable = self._source_statement(kind)
            selected = base.where(deleted if group == "deleted" else unavailable if group == "unavailable" else or_(deleted, unavailable))
            total = counts[kind][group] if group != "all" else sum(counts[kind].values())
            if kind == "works":
                works = list(session.scalars(selected.options(selectinload(Work.artist)).order_by(Work.id).offset(offset).limit(limit)))
                requests = {work.id: work.latest_revision_id for work in works if work.latest_revision_id}
                snapshots = selected_snapshots(session, works, requests)
                asset_map, derivative_map = media_maps(session, works, snapshots)
                from .revisions import project_revision
                projected = {work.id: project_revision(work, snapshots.get(work.id)) for work in works}
                asset_groups, derivative_groups = {}, {}
                for key, asset in asset_map.items(): asset_groups.setdefault(key[0], []).append(asset)
                for key, record in derivative_map.items(): derivative_groups.setdefault(key[0], []).append(record)
                items = [{"kind": "work", "id": work.id, "pixiv_id": work.pixiv_illust_id, "title": work.title,
                    "type": work.type, "page_count": work.page_count, "artist_id": work.artist_id,
                    "artist_name": work.artist.name, "availability": remote_availability(work, self.account_id),
                    "local": {**self._local_work(projected[work.id], asset_groups.get(work.id, []), derivative_groups.get(work.id, [])),
                        "default_revision_id": work.default_revision_id, "latest_revision_id": work.latest_revision_id,
                        "has_saved_version": work.default_revision_id is not None}} for work in works]
            else:
                artists = list(session.scalars(selected.order_by(Artist.id).offset(offset).limit(limit)))
                availability = self._artist_availability(session, artists)
                local = self._artist_local_summaries(session, artists)
                items = [{"kind": "artist", "id": artist.id, "pixiv_id": artist.pixiv_user_id, "name": artist.name,
                    "avatar": media_url(artist.avatar_path), "availability": availability.get(artist.id),
                    **local[artist.id]} for artist in artists]
            return {"items": items, "total": total, "offset": offset, "limit": limit,
                    "counts": counts, "catalogue_revision": catalogue_revision(session)}
