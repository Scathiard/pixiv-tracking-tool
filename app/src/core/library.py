"""Local catalogue operations. No authentication, scheduler, or HTTP dependency."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import quote

from sqlalchemy import Integer, String, and_, case, cast, func, literal, or_, select
from sqlalchemy.orm import aliased, defer, joinedload, load_only, selectinload

from ..security import sanitize_caption

from .archive_state import work_states
from .read_projection import CursorExpired
from .read_control import track_read_stage
from . import diagnostics
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
    metadata = work._read_metadata if hasattr(work, "_read_metadata") else work.source_metadata
    value = (metadata or {}).get("remote_availability")
    return availability_snapshot(value, account_id)


class Library:
    def __init__(self, database):
        self.database = database
        self.account_id = None
        self.bookmarks = None

    @staticmethod
    def _statement(filters, ceiling, *, for_count=False):
        # The integer PK range otherwise makes SQLite read wide Work table
        # pages for a default count. Counts can scan the existing covering Pixiv
        # ID index instead; row selection and navigation retain their PK range.
        upper_id = cast(Work.id, Integer) if for_count else Work.id
        # Resolve the small deletion-intent set through the unique Pixiv ID
        # index. Candidate rows can then be checked using IDs already present
        # in the sort index, without reading each skipped Work's metadata.
        suppressed_work = aliased(Work)
        suppressed_ids = select(suppressed_work.id).select_from(ArchiveIntent).join(
            suppressed_work, suppressed_work.pixiv_illust_id == ArchiveIntent.pixiv_illust_id).where(
            ArchiveIntent.mode == "suppressed").correlate(None)
        statement = select(Work).where(upper_id <= ceiling, ~Work.id.in_(suppressed_ids))
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
        return statement.options(defer(Work.source_metadata, raiseload=True), selectinload(Work.artist), selectinload(Work.tags),
                                 selectinload(Work.preference), selectinload(Work.bookmark))

    @staticmethod
    def _load_display_metadata(session, works, *, detail=False):
        from .display_metadata import read
        works = list(works)
        metadata = read(session, [work.id for work in works], include_translations=detail)
        for work in works:
            work._read_metadata = metadata[str(work.id)]

    @staticmethod
    def _bookmark_operations(session, ids):
        if not ids:
            return {}
        result = {}
        for begin in range(0, len(ids), 240):
            selected = ids[begin:begin + 240]
            values = ",".join("(?)" for _ in selected)
            winner_ids = list(session.connection().exec_driver_sql(f"""WITH selected(work_id) AS (VALUES {values})
                SELECT (SELECT id FROM bookmark_operations b WHERE b.work_id=selected.work_id
                    ORDER BY version DESC,created_at DESC,id DESC LIMIT 1) FROM selected""",
                tuple(selected)).scalars())
            winner_ids = [identity for identity in winner_ids if identity is not None]
            if winner_ids:
                result.update((operation.work_id, operation) for operation in session.scalars(
                    select(BookmarkOperation).where(BookmarkOperation.id.in_(winner_ids)).options(
                        load_only(BookmarkOperation.id, BookmarkOperation.work_id, BookmarkOperation.desired,
                            BookmarkOperation.kind, BookmarkOperation.account_id, BookmarkOperation.status, BookmarkOperation.version,
                            BookmarkOperation.error, raiseload=True))))
        return result

    def _bookmark_view(self, work, operation=None):
        from .bookmarks import bookmark_snapshot
        # SQL projection is deliberately independent of the bookmark service's
        # memory lock. Add current-account freshness only after returning the
        # read connection, so it cannot form a connection/lock cycle.
        return bookmark_snapshot(work.bookmark, operation,
                                 account_id=self.account_id, work_id=work.id)

    def _refresh_bookmarks(self, views, works, operations):
        if self.bookmarks is None:
            return
        snapshots = {work.id: self.bookmarks.snapshot(work.bookmark, operations.get(work.id), work_id=work.id)
                     for work in works}
        for view in views:
            bookmark = snapshots[view.get("work_id", view.get("id"))]
            view["bookmark"] = bookmark
            if "is_bookmarked" in view:
                view["is_bookmarked"] = bool(bookmark["value"])
            if "bookmark_confirmed_at" in view:
                view["bookmark_confirmed_at"] = bookmark["confirmed_at"]

    def _work_view(self, work, operation=None):
        bookmark = self._bookmark_view(work, operation)
        return {"id": work.id, "pixiv_id": work.pixiv_illust_id, "title": work.title,
            "artist_id": work.artist_id, "artist_name": work.artist.name,
            "type": work.type, "page_count": work.page_count,
            "caption": sanitize_caption(work.caption), "caption_state": work.caption_state,
            "tags": [tag.tag for tag in work.tags],
            "tag_details": tag_details([tag.tag for tag in work.tags],
                work._read_metadata if hasattr(work, "_read_metadata") else work.source_metadata),
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

    def artist_page(self, *, offset=0, limit=50, q="", compact=False):
        if offset < 0 or limit is not None and not 1 <= limit <= 100:
            raise ValueError("画师分页范围不合法")
        with self.database.read() as session:
            if compact:
                # Filter choices need identity and tracking state, never profile
                # text, work totals, version snapshots or recent-work media.
                statement = select(Artist.id, Artist.pixiv_user_id, Artist.name,
                    func.coalesce(TrackingPolicy.mode, "stopped")).outerjoin(
                        TrackingPolicy, TrackingPolicy.artist_id == Artist.id)
                if q.strip():
                    statement = statement.where(or_(Artist.name.contains(q.strip(), autoescape=True),
                        Artist.account.contains(q.strip(), autoescape=True)))
                    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
                else:
                    from .source_read_projection import artist_count
                    total = artist_count(session.connection())
                statement = statement.order_by(Artist.name, Artist.id).offset(offset)
                if limit is not None:
                    statement = statement.limit(limit)
                with track_read_stage("filter_artists"):
                    items = [{"id": row[0], "pixiv_id": row[1], "name": row[2], "mode": row[3]}
                             for row in session.execute(statement)]
                return {"items": items, "total": total, "offset": offset,
                        "limit": limit if limit is not None else total}
            statement = select(Artist).options(selectinload(Artist.policy))
            if q.strip():
                statement = statement.where(or_(Artist.name.contains(q.strip(), autoescape=True),
                    Artist.account.contains(q.strip(), autoescape=True)))
            if not q.strip():
                from .source_read_projection import artist_count
                total = artist_count(session.connection())
            else:
                # Explicit name/account search retains its scope-dependent count.
                total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
            statement = statement.order_by(Artist.name, Artist.id).offset(offset)
            if limit is not None:
                statement = statement.limit(limit)
            artists = list(session.scalars(statement))
            items = self._artist_views(session, artists)
            return {"items": items, "total": total, "offset": offset,
                    "limit": limit if limit is not None else total,
                    "performance": "indexed_seek" if not q.strip() and offset == 0 and limit is not None else "on_demand_scan"}

    def artist_detail(self, artist_id):
        with self.database.read() as session:
            artist = session.scalar(select(Artist).options(selectinload(Artist.policy)).where(Artist.id == artist_id))
            if artist is None:
                raise KeyError(artist_id)
            return self._artist_views(session, [artist])[0]

    def _artist_views(self, session, artists):
        from .source_read_projection import artist_totals, recent_ids
        ids = [artist.id for artist in artists]
        availability = self._artist_availability(session, artists)
        totals = artist_totals(session.connection(), ids)
        # Each author is an indexed top-12 seek, including hidden works and
        # excluding suppressed works. Window functions would visit all N works.
        windows = {artist_id: recent_ids(session.connection(), artist_id) for artist_id in ids}
        work_ids = [work_id for artist_id in ids for work_id in windows[artist_id]]
        preview_works = list(session.scalars(select(Work).options(load_only(Work.id, Work.artist_id,
            Work.pixiv_illust_id, Work.title, Work.type, Work.page_count, Work.x_restrict,
            Work.default_revision_id, Work.latest_revision_id)).where(Work.id.in_(work_ids)))) if work_ids else []
        by_id = {work.id: work for work in preview_works}
        snapshots = selected_snapshots(session, preview_works, compact=True)
        originals, derived = media_maps(session, preview_works, snapshots,
            ranges={work.id: (0, 1) for work in preview_works}, compact=True)
        recent = {artist_id: [] for artist_id in ids}
        for artist_id in ids:
            for work_id in windows[artist_id]:
                work = by_id[work_id]
                thumb = derived.get((work_id, "thumbnail", 0, "v1"))
                if thumb is not None and thumb.state != "verified":
                    thumb = None
                url = media_url(thumb.relative_path) if thumb else readable_page_url(originals.get((work_id, "image", 0)))
                if url and thumb and thumb.sha256:
                    url += "?v=" + thumb.sha256[:16]
                recent[artist_id].append({"id": work_id, "pixiv_id": work.pixiv_illust_id,
                    "title": work.title, "type": work.type, "page_count": work.page_count,
                    "x_restrict": work.x_restrict, "thumbnail": url})
        return [{"id": artist.id, "pixiv_id": artist.pixiv_user_id, "name": artist.name,
            "account": artist.account, "avatar_url": media_url(artist.avatar_path),
            "avatar": media_url(artist.avatar_path), "biography": artist.biography,
            "availability": availability.get(artist.id),
            "profile_refreshed_at": artist.profile_refreshed_at.isoformat() if artist.profile_refreshed_at else None,
            "mode": artist.policy.mode if artist.policy else "stopped",
            "added_at": artist.added_at.isoformat() if artist.added_at else None,
            "last_checked_at": artist.last_checked_at.isoformat() if artist.last_checked_at else None,
            "work_count": totals.get(artist.id, {}).get("total", 0), "recent_works": recent[artist.id]}
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
            work = session.get(Work, work_id, options=[load_only(Work.id, raiseload=True)])
            if work is None:
                raise KeyError(work_id)
            if work.preference is None:
                work.preference = LocalPreference(is_hidden=False, rating=0)
            if hidden is not None:
                work.preference.is_hidden = bool(hidden)
            if rating is not None:
                work.preference.rating = rating

    def _hydrate_window(self, session, window_ids, *, compact=False):
        if compact:
            return self._hydrate_cards(session, window_ids)
        related = self._related(select(Work).where(Work.id.in_(window_ids)))
        with track_read_stage("library_works"):
            by_id = {work.id: work for work in session.scalars(related)} if window_ids else {}
        works = [by_id[work_id] for work_id in window_ids]
        with track_read_stage("library_metadata"):
            self._load_display_metadata(session, works, detail=True)
        with track_read_stage("library_revisions"):
            snapshots = selected_snapshots(session, works, display=True)
        with track_read_stage("library_bookmarks"):
            operations = self._bookmark_operations(session, [work.id for work in works])
        with diagnostics.span('read.library.assemble', selected_count=len(works)):
            views = [self._work_view(work, operations.get(work.id)) for work in works]
            for work, view in zip(works, views):
                apply_snapshot(view, work, snapshots.get(work.id))
        animation_ids = [view["id"] for view in views if view["type"] == "ugoira"]
        with track_read_stage("library_states"):
            states, revision = work_states(session, works)
        with track_read_stage("library_media"):
            assets, derived = media_maps(session, works, snapshots,
                ranges={work.id: (0, 1) for work in works})
        thumbnails = {work.id: derived.get((work.id, "thumbnail", 0, "v1")) for work in works}
        first_pages = {work.id: assets.get((work.id, "image", 0)) for work in works}
        animations = {work_id: derived.get((work_id, "gif", 0, "v1")) for work_id in animation_ids}
        for view in views:
            view.update(states[view["id"]])
            thumb = thumbnails.get(view["id"])
            if thumb is not None and thumb.state != "verified":
                thumb = None
            preview = animations.get(view["id"]) if view["type"] == "ugoira" else first_pages.get(view["id"])
            view["preview"] = readable_page_url(preview, animation=view["type"] == "ugoira")
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
        return views, works, operations, revision

    def _hydrate_cards(self, session, window_ids):
        """Draw a default gallery window without hydrating wide source entities."""
        from .gallery_card_projection import read_cards, card_view
        with track_read_stage("library_works"):
            by_id = read_cards(session, window_ids)
            works = [by_id[identity] for identity in window_ids]
        with track_read_stage("library_bookmarks"):
            preferences = {row.work_id: row for row in session.scalars(
                select(LocalPreference).where(LocalPreference.work_id.in_(window_ids)))} if window_ids else {}
            bookmarks = {row.work_id: row for row in session.scalars(
                select(RemoteBookmark).where(RemoteBookmark.work_id.in_(window_ids)))} if window_ids else {}
            operations = self._bookmark_operations(session, window_ids)
            views = []
            for work in works:
                work.preference, work.bookmark = preferences.get(work.id), bookmarks.get(work.id)
                bookmark = self._bookmark_view(work, operations.get(work.id))
                views.append({**card_view(work, self.account_id),
                    "is_hidden": bool(work.preference and work.preference.is_hidden),
                    "bookmark": bookmark, "is_bookmarked": bool(bookmark["value"])})
        with track_read_stage("library_states"):
            states, revision = work_states(session, works)
            with diagnostics.span('read.library.apply_states', selected_count=len(views)):
                for view in views:
                    view.update(states[view["id"]])
        return views, works, operations, revision

    @staticmethod
    def _fast_scope(filters):
        """Only scopes backed by a matching ordered index promise fixed work."""
        if (filters.show_hidden or filters.recent or filters.q.strip()
                or filters.tags is not None or filters.tags_exclude is not None
                or filters.artist_ids_exclude is not None or filters.posted_after is not None
                or filters.posted_before is not None):
            return None
        selected = []
        if filters.artist_ids is not None:
            if len(filters.artist_ids) != 1:
                return None
            selected.append(f"artist:{filters.artist_ids[0]}")
        if filters.types is not None:
            if len(filters.types) != 1:
                return None
            selected.append(f"type:{filters.types[0]}")
        if filters.favorites:
            selected.append("favorite")
        return (selected[0] if selected else "visible") if len(selected) <= 1 else None

    @staticmethod
    def _filter_key(filters):
        value = asdict(filters)
        # A reference timestamp has no bearing on a non-relative scope. The
        # explicit posted boundaries already bind period/date filters.
        if not filters.recent:
            value["as_of"] = None
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
            separators=(",", ":")).encode()).hexdigest()[:32]

    @staticmethod
    def _encode_cursor(filters, generation, ceiling, work_id, key):
        value = {"v": 1, "filter": Library._filter_key(filters), "generation": generation,
                 "ceiling": ceiling, "id": work_id, "key": key,
                 "as_of": filters.as_of.isoformat() if filters.as_of else None}
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":"),
            ensure_ascii=False).encode()).decode().rstrip("=")

    @staticmethod
    def cursor_reference(token):
        """Recover the relative-filter reference before parsing request filters.

        This is a parsing aid, never authorization; continuation subsequently
        validates the complete token, scope and current ordering boundary.
        """
        try:
            if not isinstance(token, str) or len(token) > 4096:
                raise ValueError()
            value = json.loads(base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True))
            stamp = value.get("as_of")
            if stamp is None:
                return None
            if not isinstance(stamp, str) or datetime.fromisoformat(stamp).tzinfo is not None:
                raise ValueError()
            return stamp
        except (ValueError, TypeError, UnicodeError, AttributeError) as exc:
            raise ValueError("游标格式无效") from exc

    @staticmethod
    def _decode_cursor(token, filters, generation, ceiling=None):
        try:
            if not isinstance(token, str) or len(token) > 4096:
                raise ValueError()
            value = json.loads(base64.b64decode(token + "=" * (-len(token) % 4),
                altchars=b"-_", validate=True))
            if (not isinstance(value, dict) or value.get("v") != 1
                    or type(value.get("id")) is not int or value["id"] < 1
                    or type(value.get("ceiling")) is not int or value["ceiling"] < value["id"]
                    or type(value.get("generation")) is not int
                    or not 0 <= value["generation"] <= generation
                    or not isinstance(value.get("key"), str)
                    or value.get("filter") != Library._filter_key(filters)
                    or ceiling is not None and value["ceiling"] != ceiling):
                raise ValueError()
        except (ValueError, TypeError, UnicodeError, KeyError) as exc:
            raise ValueError("游标与筛选或范围不匹配") from exc
        return value

    def _cursor_boundary(self, session, decoded, filters, ceiling, generation):
        """An unrelated write is not a change to this continuation boundary.

        Revalidate identity, visibility, filters and sort key in the same read
        snapshot as the seek. The original ceiling and relative-filter time
        remain fixed; a moved or removed anchor needs an explicit rebase.
        """
        try:
            current = self._anchor_row(session, decoded["id"], filters, ceiling)
        except KeyError as exc:
            raise CursorExpired("当前作品已移出阅读范围，请重新定位") from exc
        if current[1] != decoded["key"]:
            if decoded["generation"] != generation:
                raise CursorExpired("当前作品排序已变化，请重新定位")
            raise ValueError("游标边界无效")
        return current

    @staticmethod
    def _seek_key(filters):
        if filters.sort == "title":
            return func.lower(Work.title)
        field = Work.downloaded_at if filters.sort == "added" else Work.posted_at
        return func.coalesce(cast(field, String), "")

    def _seek_rows(self, session, filters, ceiling, *, boundary=None,
                   direction="next", inclusive=False, limit=100):
        """Return narrow rows; unsupported filters explicitly retain scan cost."""
        scope = self._fast_scope(filters)
        ascending = filters.sort in ("oldest", "title")
        forward = direction == "next"
        query_ascending = ascending if forward else not ascending
        comparison = ">" if query_ascending else "<"
        if inclusive:
            comparison += "="
        if scope is not None:
            column = {"title": "title_key", "added": "added_key"}.get(filters.sort, "order_key")
            predicates, args = ["visible=1", "work_id<=?"], [ceiling]
            index = {"title": "ix_read_work_title", "added": "ix_read_work_added"}.get(
                filters.sort, "ix_read_work_order")
            if scope.startswith("artist:"):
                predicates.append("artist_id=?"); args.append(int(scope[7:]))
                index = index + "_artist" if column != "order_key" else "ix_read_work_artist"
            elif scope.startswith("type:"):
                predicates.append("work_type=?"); args.append(scope[5:])
                index = index + "_type" if column != "order_key" else "ix_read_work_type"
            elif scope == "favorite":
                predicates.append("favorite=1")
                index = index + "_favorite" if column != "order_key" else "ix_read_work_favorite"
            order = "ASC" if query_ascending else "DESC"
            sql = f"SELECT work_id,{column} FROM read_work_order INDEXED BY {index} WHERE " + " AND ".join(predicates)
            suffix = f" ORDER BY {column} {order},work_id {order} LIMIT ?"
            if boundary is None:
                rows = session.connection().exec_driver_sql(sql + suffix, (*args, limit)).all()
            else:
                # SQLite can seek only the leading key for a row-value range
                # and then walk every tied key to test work_id. Two disjoint
                # index ranges keep even an all-equal sort key bounded.
                # Do not leave two upper bounds on work_id: SQLite may choose
                # the looser ceiling and walk all tied IDs to the anchor.
                equal_sql = f"SELECT work_id,{column} FROM read_work_order INDEXED BY {index} WHERE " + \
                    " AND ".join([predicates[0], *predicates[2:]]) + f" AND {column}=?"
                if query_ascending:
                    lower = boundary[0] if inclusive else boundary[0] + 1
                    equal_sql += " AND work_id>=? AND work_id<=?"
                    equal_args = (*args[1:], boundary[1], lower, ceiling, limit)
                else:
                    upper = min(ceiling, boundary[0] if inclusive else boundary[0] - 1)
                    equal_sql += " AND work_id<=?"
                    equal_args = (*args[1:], boundary[1], upper, limit)
                rows = session.connection().exec_driver_sql(equal_sql + suffix, equal_args).all()
                if len(rows) < limit:
                    strict = ">" if query_ascending else "<"
                    rows += session.connection().exec_driver_sql(sql +
                        f" AND {column} {strict} ?" + suffix,
                        (*args, boundary[1], limit - len(rows))).all()
        else:
            from sqlalchemy import tuple_
            key = self._seek_key(filters)
            statement = self._statement(filters, ceiling).with_only_columns(Work.id, key)
            if boundary is not None:
                statement = statement.where(tuple_(key, Work.id).op(comparison)((boundary[1], boundary[0])))
            order = (key.asc(), Work.id.asc()) if query_ascending else (key.desc(), Work.id.desc())
            rows = session.execute(statement.order_by(*order).limit(limit)).all()
        return list(rows)

    def _anchor_row(self, session, work_id, filters, ceiling):
        scope = self._fast_scope(filters)
        if scope is not None:
            column = {"title": "title_key", "added": "added_key"}.get(filters.sort, "order_key")
            row = session.connection().exec_driver_sql(
                f"SELECT work_id,{column},visible,artist_id,work_type,favorite FROM read_work_order WHERE work_id=?",
                (work_id,)).first()
            if row is None or not row[2] or row[0] > ceiling:
                raise KeyError(work_id)
            if (scope.startswith("artist:") and row[3] != int(scope[7:])
                    or scope.startswith("type:") and row[4] != scope[5:]
                    or scope == "favorite" and not row[5]):
                raise KeyError(work_id)
            return (row[0], row[1])
        row = session.execute(self._statement(filters, ceiling).with_only_columns(
            Work.id, self._seek_key(filters)).where(Work.id == work_id)).first()
        if row is None:
            raise KeyError(work_id)
        return row

    def _cursor_metadata(self, session, filters, rows, ceiling, generation):
        tokens = [self._encode_cursor(filters, generation, ceiling, row[0], row[1]) for row in rows]
        more = bool(rows and self._seek_rows(session, filters, ceiling, boundary=rows[-1], limit=1))
        previous = bool(rows and self._seek_rows(session, filters, ceiling,
            boundary=rows[0], direction="previous", limit=1))
        return {"boundaries": tokens, "next_cursor": tokens[-1] if more else None,
                "previous_cursor": tokens[0] if previous else None,
                "has_more": more, "has_previous": previous, "generation": generation}

    def query_cursor(self, filters=LibraryFilter(), *, limit=100, cursor=None,
                     direction="next", ceiling=None, anchor_work_id=None, compact=True,
                     _session=None, _bookmark_updates=None):
        """Seek a gallery window; arbitrary exact counts are separate opt-in work.

        Single visible/artist/type/favorite scopes with each supported ordering
        use O(log N + K) indexed seeks and O(1) maintained counts. Other filters
        keep their complete semantics, but advertise on-demand scan cost.
        """
        from .read_projection import session_generation, common_count
        if type(limit) is not int or not 1 <= limit <= 500 or direction not in ("next", "previous"):
            raise ValueError("游标分页范围不合法")
        if filters.as_of is None:
            stamp = self.cursor_reference(cursor) if cursor else None
            filters = replace(filters, as_of=datetime.fromisoformat(stamp) if stamp else utcnow())
        with nullcontext(_session) if _session is not None else self.database.read() as session:
            generation = session_generation(session)
            decoded = self._decode_cursor(cursor, filters, generation, ceiling) if cursor else None
            current_ceiling = session.scalar(select(func.max(Work.id))) or 0
            ceiling = decoded["ceiling"] if decoded else current_ceiling if ceiling is None else ceiling
            if type(ceiling) is not int or ceiling < 0:
                raise ValueError("图库范围无效")
            boundary = self._cursor_boundary(session, decoded, filters, ceiling, generation) if decoded else None
            if anchor_work_id is not None:
                if decoded and decoded["id"] != anchor_work_id:
                    raise ValueError("游标与作品不匹配")
                if not decoded:
                    boundary = self._anchor_row(session, anchor_work_id, filters, ceiling)
            rows = self._seek_rows(session, filters, ceiling, boundary=boundary,
                direction=direction, inclusive=anchor_work_id is not None, limit=limit)
            if direction == "previous":
                rows.reverse()
            scope = self._fast_scope(filters)
            indexed = scope is not None and ceiling == current_ceiling
            total = common_count(session.connection(), scope) if indexed else None
            metadata = self._cursor_metadata(session, filters, rows, ceiling, generation)
            views, works, operations, revision = self._hydrate_window(session, [row[0] for row in rows], compact=compact)
            for view, token in zip(views, metadata["boundaries"]):
                view["cursor"] = token
        if _bookmark_updates is not None:
            _bookmark_updates.append((views, works, operations))
        else:
            self._refresh_bookmarks(views, works, operations)
        return {"items": views, "total": total, "offset": 0, "ceiling": ceiling,
                "as_of": (filters.as_of or utcnow()).isoformat(), "catalogue_revision": revision,
                "performance": "indexed_seek" if indexed else "on_demand_scan",
                "count_mode": "indexed" if indexed else "on_demand",
                "continuation_rebased": bool(decoded and decoded["generation"] != generation), **metadata}

    list_cursor = query_cursor

    def cursor_neighbors(self, work_id, filters=LibraryFilter(), *, ceiling=None, cursor=None,
                         from_cursor=None, from_direction="next", _session=None):
        from .read_projection import session_generation
        if filters.as_of is None:
            stamp = self.cursor_reference(cursor) if cursor else None
            filters = replace(filters, as_of=datetime.fromisoformat(stamp) if stamp else utcnow())
        with nullcontext(_session) if _session is not None else self.database.read() as session:
            generation = session_generation(session)
            decoded = self._decode_cursor(cursor, filters, generation, ceiling) if cursor else None
            ceiling = decoded["ceiling"] if decoded else ceiling
            ceiling = ceiling if ceiling is not None else session.scalar(select(func.max(Work.id))) or 0
            if decoded and decoded["id"] != work_id:
                raise ValueError("游标与作品不匹配")
            row = (self._cursor_boundary(session, decoded, filters, ceiling, generation) if decoded
                   else self._anchor_row(session, work_id, filters, ceiling))
            if from_cursor:
                if from_direction not in ("next", "previous"):
                    raise ValueError("切换方向无效")
                origin = self._decode_cursor(from_cursor, filters, generation, ceiling)
                source = self._cursor_boundary(session, origin, filters, ceiling, generation)
                adjacent = self._seek_rows(session, filters, ceiling, boundary=source,
                    direction=from_direction, limit=1)
                if not adjacent or adjacent[0][0] != work_id:
                    raise CursorExpired("相邻作品已变化，请按当前作品重新定位")
            result = {"index": None, "generation": generation, "ceiling": ceiling,
                "continuation_rebased": bool(decoded and decoded["generation"] != generation),
                "anchor_cursor": self._encode_cursor(filters, generation, ceiling, *row)}
            for name, direction in (("previous", "previous"), ("next", "next")):
                near = self._seek_rows(session, filters, ceiling, boundary=row, direction=direction, limit=1)
                if not near:
                    result[name] = None
                    continue
                neighbor = session.scalar(select(Work).where(Work.id == near[0][0]).options(
                    load_only(Work.id, Work.default_revision_id, Work.type, Work.page_count, raiseload=True)))
                snapshot = selected_snapshots(session, [neighbor], compact=True).get(neighbor.id)
                result[name] = {"work_id": neighbor.id, "revision_id": snapshot.id if snapshot else None,
                    "page_count": 1 if (snapshot.type if snapshot else neighbor.type) == "ugoira"
                        else (snapshot.page_count if snapshot else neighbor.page_count),
                    "cursor": self._encode_cursor(filters, generation, ceiling, *near[0])}
            return result

    def cursor_feed(self, filters=LibraryFilter(), *, limit=48, cursor=None,
                    direction="next", ceiling=None, anchor_work_id=None, page=0,
                    index=0, work_index=0):
        """Resolve a bounded page window and its boundaries in one read snapshot."""
        if type(limit) is not int or not 1 <= limit <= 120:
            raise ValueError("阅读窗口过大")
        updates = []
        with self.database.read() as session:
            batch = self.query_cursor(filters, limit=limit + 1, cursor=cursor,
                direction=direction, ceiling=ceiling, compact=True, anchor_work_id=anchor_work_id,
                _session=session, _bookmark_updates=updates)
            # All members use the relative-filter reference established above.
            filters = replace(filters, as_of=datetime.fromisoformat(batch["as_of"]))
            cards = batch["items"]
            positions = {card["id"]: position for position, card in enumerate(cards)}
            tokens = {card["id"]: card["cursor"] for card in cards}
            requests, remaining, ordinal, position = [], limit, index, work_index
            candidates = cards if direction == "next" else list(reversed(cards))
            more = bool(batch["has_more"] if direction == "next" else batch["has_previous"])
            for number, card in enumerate(candidates):
                count = max(1, 1 if card["type"] == "ugoira" else card["page_count"])
                first = page if number == 0 and anchor_work_id is not None else (0 if direction == "next" else count - 1)
                if direction == "next" and first >= count or direction == "previous" and first < 0:
                    position += 1 if direction == "next" else -1
                    continue
                first = min(count - 1, max(0, first))
                take = min(remaining, count - first if direction == "next" else first + 1)
                start_page = first if direction == "next" else first - take + 1
                start_index = ordinal if direction == "next" else ordinal - take + 1
                work_start = start_index - start_page
                requests.append({"work_id": card["id"], "revision_id": card.get("revision_id"),
                    "page_offset": start_page, "page_limit": take, "index": max(0, start_index),
                    "work_index": max(0, position), "round": 0,
                    "previous_work_index": None, "next_work_index": work_start + count})
                remaining -= take
                ordinal += take if direction == "next" else -take
                if not remaining:
                    more = more or (first + take < count if direction == "next" else start_page > 0) or number + 1 < len(candidates)
                    break
                position += 1 if direction == "next" else -1
            if direction == "previous":
                requests.reverse()
            if requests:
                first = requests[0]
                nearby = self.cursor_neighbors(first["work_id"], filters, ceiling=batch["ceiling"],
                    cursor=tokens[first["work_id"]], _session=session)
                predecessor = nearby["previous"]
                previous = (first["index"] - first["page_offset"] - predecessor["page_count"]) if predecessor else None
                for wanted in requests:
                    wanted["previous_work_index"] = max(0, previous) if previous is not None else None
                    previous = wanted["index"] - wanted["page_offset"]
            result = self.reading_window(requests, _session=session, _bookmark_updates=updates)
            items = result["items"]
            if len(items) != sum(wanted["page_limit"] for wanted in requests):
                raise CursorExpired("阅读范围已变化")
            for item in items:
                item["work_cursor"] = tokens[item["work_id"]]
                item["has_next_work"] = positions[item["work_id"]] + 1 < len(cards) or bool(batch["has_more"])
        # Bookmark service locks must never be acquired while holding a read
        # connection. Apply account freshness after the whole snapshot closes.
        for views, works, operations in updates:
            self._refresh_bookmarks(views, works, operations)
        end = items[-1]["index"] + 1 if items else index
        return {**result, "offset": items[0]["index"] if items else index,
            "total": end + (1 if direction == "next" and more else 0),
            "has_more": more if direction == "next" else bool(items),
            "has_previous": more if direction == "previous" else bool(items and items[0]["index"] > 0),
            "ceiling": batch["ceiling"], "as_of": batch["as_of"],
            "generation": batch["generation"], "continuation_rebased": batch["continuation_rebased"],
            "performance": batch["performance"], "count_mode": batch["count_mode"]}

    def query(self, filters=LibraryFilter(), *, offset=0, limit=100, ceiling=None, compact=False):
        """Explicit numeric jump compatibility; deep OFFSET is O(offset + K)."""
        filters = replace(filters, as_of=filters.as_of or utcnow())
        from .read_projection import session_generation, common_count
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("分页范围不合法")
        with self.database.read() as session:
            generation = session_generation(session)
            current_ceiling = session.scalar(select(func.max(Work.id))) or 0
            ceiling = ceiling if ceiling is not None else current_ceiling
            scope = self._fast_scope(filters)
            if scope is not None and ceiling == current_ceiling:
                total = common_count(session.connection(), scope)
            else:
                count_statement = self._statement(filters, ceiling, for_count=True).with_only_columns(Work.id)
                total = session.scalar(select(func.count()).select_from(count_statement.subquery()))
            if offset == 0 and scope is not None:
                rows = self._seek_rows(session, filters, ceiling, limit=limit)
            else:
                statement = self._statement(filters, ceiling)
                window_ids = list(session.scalars(statement.with_only_columns(Work.id).order_by(
                    *self._order(filters)).offset(offset).limit(limit)))
                rows = [self._anchor_row(session, work_id, filters, ceiling) for work_id in window_ids]
            views, works, operations, revision = self._hydrate_window(session, [row[0] for row in rows], compact=compact)
            metadata = self._cursor_metadata(session, filters, rows, ceiling, generation)
            for view, token in zip(views, metadata["boundaries"]):
                view["cursor"] = token
        self._refresh_bookmarks(views, works, operations)
        return {"items": views, "total": total, "offset": offset, **metadata,
                "has_more": offset + len(views) < total, "performance": "offset_scan",
                "ceiling": ceiling, "catalogue_revision": revision}

    def counts(self, filters=LibraryFilter(), *, ceiling=None):
        """Counts do not hydrate any artwork, assets, or processing history."""
        with self.database.read() as session:
            ceiling = ceiling if ceiling is not None else session.scalar(select(func.max(Work.id))) or 0
            filters = replace(filters, as_of=filters.as_of or utcnow())
            result = {"ceiling": ceiling, "as_of": filters.as_of.isoformat(),
                      "catalogue_revision": catalogue_revision(session), "performance": "on_demand_exact"}
            base = replace(filters, favorites=False, recent=False)
            counts_by_filter = {}
            for name, selected in (("all", base), ("recent", replace(base, recent=True)),
                    ("favorites", replace(base, favorites=True)), ("total", filters)):
                # Every other filter, ceiling, and reference time is shared
                # within this request; these are the only varying predicates.
                scope = (selected.favorites, selected.recent)
                if scope not in counts_by_filter:
                    statement = self._statement(selected, ceiling, for_count=True).with_only_columns(Work.id)
                    counts_by_filter[scope] = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
                result[name] = counts_by_filter[scope]
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
        from .projection_maintenance import read_revision_state
        return read_revision_state(session, snapshot.id)

    @staticmethod
    def _raw_revision_archive(session, snapshot):
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

    def reading_window(self, requests, *, _session=None, _bookmark_updates=None):
        """Materialize a bounded set of immutable-version image slots in batches."""
        if sum(request["page_limit"] for request in requests) > 120:
            raise ValueError("阅读窗口过大")
        ids = list({request["work_id"] for request in requests})
        with nullcontext(_session) if _session is not None else self.database.read() as session:
            works = {work.id: work for work in session.scalars(select(Work).where(
                Work.id.in_(ids), ~select(ArchiveIntent.pixiv_illust_id).where(
                    ArchiveIntent.pixiv_illust_id == Work.pixiv_illust_id,
                    ArchiveIntent.mode == "suppressed").exists()).options(
                        load_only(Work.id, Work.pixiv_illust_id, Work.artist_id, Work.title, Work.type,
                            Work.page_count, Work.x_restrict, Work.default_revision_id, Work.latest_revision_id,
                            raiseload=True), selectinload(Work.artist).load_only(Artist.name),
                        selectinload(Work.bookmark)))}
            self._load_display_metadata(session, works.values())
            explicit = {request["work_id"]: request["revision_id"] for request in requests}
            snapshots = selected_snapshots(session, works.values(), explicit, compact=True)
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
            views = {work.id: apply_snapshot({"title": work.title, "artist_id": work.artist_id,
                "artist_name": work.artist.name, "pixiv_id": work.pixiv_illust_id, "type": work.type,
                "page_count": work.page_count, "x_restrict": work.x_restrict,
                "bookmark": self._bookmark_view(work, operations.get(work.id)),
                "availability": remote_availability(work, self.account_id)}, work,
                snapshots.get(work.id), compact=True) for work in works.values()}
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
                         "preview": self._page_preview(derived, work.id, page),
                         "width": record.width if record else None, "height": record.height if record else None,
                         "state": record.state if record else "missing"})
        if _bookmark_updates is not None:
            _bookmark_updates.append((items, list(works.values()), operations))
        else:
            self._refresh_bookmarks(items, works.values(), operations)
        return {"items": items, "catalogue_revision": revision}

    @staticmethod
    def _page_preview(derived, work_id, page):
        # Only page zero has a generated local thumbnail. Never present another
        # page's thumbnail as if it belonged to the requested image.
        thumb = derived.get((work_id, "thumbnail", 0, "v1")) if page == 0 else None
        return media_url(thumb.relative_path) if thumb and thumb.state == "verified" else None

    @staticmethod
    def _revision_page(session, work_id, *, before_ordinal=None, limit=100, default_revision_id=None):
        statement = select(WorkRevision.id, WorkRevision.ordinal, WorkRevision.created_at,
            WorkRevision.change_kind, WorkRevision.state, WorkRevision.page_count).where(
                WorkRevision.work_id == work_id, WorkRevision.state != "redundant")
        if before_ordinal is not None:
            statement = statement.where(WorkRevision.ordinal < before_ordinal)
        rows = list(session.execute(statement.order_by(WorkRevision.ordinal.desc()).limit(limit + 1)))
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [{"id": row.id, "ordinal": row.ordinal, "created_at": row.created_at.isoformat(),
            "change_kind": row.change_kind, "state": row.state, "page_count": row.page_count,
            "current": row.id == default_revision_id} for row in rows], "has_more": has_more,
            "before": rows[-1].ordinal if rows and has_more else None}

    def revision_page(self, work_id, *, before_ordinal=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100 or before_ordinal is not None and (
                type(before_ordinal) is not int or before_ordinal < 1):
            raise ValueError("版本分页范围不合法")
        with self.database.read() as session:
            work = session.get(Work, work_id, options=[load_only(Work.id, Work.pixiv_illust_id,
                Work.default_revision_id, raiseload=True)])
            if work is None or session.scalar(select(ArchiveIntent.mode).where(
                    ArchiveIntent.pixiv_illust_id == work.pixiv_illust_id)) == "suppressed":
                raise KeyError(work_id)
            return self._revision_page(session, work_id, before_ordinal=before_ordinal,
                limit=limit, default_revision_id=work.default_revision_id)

    def detail(self, work_id, *, page_offset=0, page_limit=100, revision_id=None):
        if page_offset < 0 or not 1 <= page_limit <= 200:
            raise ValueError("页面范围不合法")
        with self.database.read() as session:
            work = session.scalar(self._related(select(Work).where(Work.id == work_id,
                ~select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.pixiv_illust_id == Work.pixiv_illust_id,
                    ArchiveIntent.mode == "suppressed").exists())))
            if work is None:
                raise KeyError(work_id)
            self._load_display_metadata(session, [work], detail=True)
            snapshots = selected_snapshots(session, [work], {work_id: revision_id} if revision_id else None, display=True)
            snapshot = snapshots.get(work_id)
            if revision_id is not None and (snapshot is None or snapshot.state == "redundant"):
                raise KeyError(revision_id)
            operations = self._bookmark_operations(session, [work.id])
            view = self._work_view(work, operations.get(work.id))
            apply_snapshot(view, work, snapshot)
            states, revision = work_states(session, [work])
            view.update(states[work_id])
            view["catalogue_revision"] = revision
            count = 1 if view["type"] == "ugoira" else view["page_count"]
            originals, derived = media_maps(session, [work], snapshots, ranges={work.id: (page_offset, page_offset + page_limit)})
            preview = derived.get((work_id, "gif", 0, "v1"))
            if snapshot is not None:
                view["archive"] = self._revision_archive(session, snapshot)
            versions = self._revision_page(session, work_id, default_revision_id=work.default_revision_id)
            view["revisions"] = versions["items"]
            # A pinned old/default revision remains selectable even when its
            # ordinal is older than the first history page.
            if snapshot is not None and snapshot.id not in {row["id"] for row in view["revisions"]}:
                view["revisions"].append({"id": snapshot.id, "ordinal": snapshot.ordinal,
                    "created_at": snapshot.created_at.isoformat(), "change_kind": snapshot.change_kind,
                    "state": snapshot.state, "page_count": snapshot.page_count,
                    "current": snapshot.id == work.default_revision_id})
            view["revisions_has_more"], view["revisions_before"] = versions["has_more"], versions["before"]
            view["pages"] = []
            for index in range(page_offset, min(count, page_offset + page_limit)):
                record = preview if view["type"] == "ugoira" else originals.get((work_id, "image", index))
                view["pages"].append({"index": index, "src": readable_page_url(record, animation=view["type"] == "ugoira"),
                    "preview": self._page_preview(derived, work_id, index),
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
        self._refresh_bookmarks([view], [work], operations)
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

    def _tag_counts(self, session, filters, ceiling):
        if replace(filters, as_of=None, sort="newest", tz_offset_minutes=0) == LibraryFilter(show_hidden=True):
            # The tag-only index cannot supply work_id for suppression checks:
            # scanning it caused a table lookup for every tag relation. Read
            # the narrow two-column table sequentially, then group/sort. The
            # (work_id, tag) primary key makes each row one work, not one page.
            # NOT INDEXED deliberately prevents that non-covering access path.
            statement = """SELECT t.tag, count(*) FROM work_tags t NOT INDEXED
                WHERE t.work_id NOT IN (SELECT w.id FROM archive_intents i
                    JOIN works w ON w.pixiv_illust_id=i.pixiv_illust_id
                    WHERE i.mode='suppressed')"""
            parameters = ()
            if ceiling is not None:
                statement += " AND t.work_id<=?"
                parameters = (ceiling,)
            with track_read_stage("filter_tags"):
                return dict(session.connection().exec_driver_sql(
                    statement + " GROUP BY t.tag ORDER BY t.tag", parameters).all())
        else:
            ceiling = ceiling if ceiling is not None else session.scalar(select(func.max(Work.id))) or 0
            selected = self._statement(filters, ceiling).with_only_columns(Work.id)
            statement = select(WorkTag.tag, func.count()).where(WorkTag.work_id.in_(selected))
        with track_read_stage("filter_tags"):
            return dict(session.execute(statement.group_by(WorkTag.tag).order_by(WorkTag.tag)).all())

    def tags(self, filters=LibraryFilter(), *, ceiling=None):
        with self.database.read() as session:
            return list(self._tag_counts(session, filters, ceiling))

    def tag_catalogue(self, filters=LibraryFilter(), *, ceiling=None):
        """Current local choices and already stored Pixiv translations only."""
        with self.database.read() as session:
            counts = self._tag_counts(session, filters, ceiling)
            names = list(counts)
            wanted, records, dates = set(names), {}, {}
            if names:
                # The display projection contains translations separately from
                # URL/frame manifests. Its PK range excludes all revision history.
                rows = session.connection().exec_driver_sql("""SELECT d.scope,d.translations
                    FROM read_display_metadata d JOIN read_source_work_local l
                      ON l.work_id=CAST(substr(d.scope,6) AS INTEGER) AND l.included=1
                    WHERE d.scope>='work:' AND d.scope<'work;'
                      AND d.translations NOT IN ('null','{}') ORDER BY d.scope""")
                with track_read_stage("filter_translations"):
                    for scope, payload in rows:
                        translations = json.loads(payload)
                        if not isinstance(translations, dict):
                            continue
                        for name, record in translations.items():
                            if (name not in wanted or not isinstance(record, dict)
                                    or record.get("source") != "pixiv" or record.get("language") != "zh-cn"
                                    or not isinstance(record.get("translated_name"), str)
                                    or not record["translated_name"].strip()):
                                continue
                            try:
                                checked = datetime.fromisoformat(record.get("checked_at", ""))
                                checked = checked.replace(tzinfo=timezone.utc) if checked.tzinfo is None else checked
                                stamp = checked.timestamp()
                            except (TypeError, ValueError, OverflowError, OSError):
                                stamp = float("-inf")
                            score = (stamp, scope)
                            if name not in dates or score > dates[name]:
                                records[name], dates[name] = record, score
            return {"tags": names, "tag_counts": counts, "tag_details": tag_details(
                [name for name in names if name in records], {"tag_translations": records})}

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
                          Work.default_revision_id, Work.latest_revision_id, raiseload=True),
                joinedload(Work.artist).load_only(Artist.name), joinedload(Work.bookmark)).where(Work.id.in_(ids)))}
            operations = self._bookmark_operations(session, ids)
            states, revision = work_states(session, works.values())
            self._load_display_metadata(session, works.values())
            bookmark_views = {work_id: self._bookmark_view(work, operations.get(work_id))
                              for work_id, work in works.items()}
            snapshots = selected_snapshots(session, works.values(), compact=True)
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
        self._refresh_bookmarks(items, works.values(), operations)
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
            self._load_display_metadata(session, works, detail=True)
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
        from .source_read_projection import artist_totals
        counts = artist_totals(session.connection(), [artist.id for artist in artists])
        result = {}
        for artist in artists:
            row = counts.get(artist.id, {})
            incomplete_count, unknown_count = row.get("incomplete", 0), row.get("unknown", 0)
            missing = []
            if incomplete_count:
                missing.append({"role": "saved_works", "page": None,
                    "label": f"{incomplete_count} 件已保存作品有本地缺项", "kind": "original"})
            if artist.avatar_url and not artist.avatar_path:
                missing.append({"role": "avatar", "page": None, "label": "画师头像缓存", "kind": "derivative"})
            unknown = bool(unknown_count or artist.profile_refreshed_at is None)
            result[artist.id] = {"work_count": row.get("total", 0), "incomplete_work_count": incomplete_count,
                "local": {"state": "incomplete" if missing else "unknown" if unknown else "complete", "missing": missing}}
        return result

    def source_overview(self, *, kind="works", group="all", offset=0, limit=50):
        from .source_read_projection import counts as source_counts, page_ids, work_details
        if kind not in ("works", "artists") or group not in ("all", "deleted", "unavailable"):
            raise ValueError("来源一览筛选不合法")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("来源一览分页不合法")
        with self.database.read() as session:
            counts = source_counts(session.connection(), self.account_id)
            ids = page_ids(session.connection(), kind=kind, group=group,
                account_id=self.account_id, offset=offset, limit=limit)
            total = counts[kind][group] if group != "all" else sum(counts[kind].values())
            if kind == "works":
                by_id = {work.id: work for work in session.scalars(select(Work).options(load_only(Work.id, Work.pixiv_illust_id, Work.title, Work.type, Work.page_count, Work.artist_id, raiseload=True), joinedload(Work.artist).load_only(Artist.name)).where(Work.id.in_(ids)))} if ids else {}
                works = [by_id[identity] for identity in ids]
                self._load_display_metadata(session, works)
                local = work_details(session.connection(), ids)
                items = [{"kind": "work", "id": work.id, "pixiv_id": work.pixiv_illust_id, "title": work.title,
                    "type": work.type, "page_count": work.page_count, "artist_id": work.artist_id,
                    "artist_name": work.artist.name, "availability": remote_availability(work, self.account_id),
                    "local": local[work.id]} for work in works]
            else:
                rows = list(session.execute(select(Artist, ApplicationState.value).join(ApplicationState,
                    ApplicationState.key == literal(ARTIST_PREFIX) + Artist.pixiv_user_id).where(Artist.id.in_(ids)))) if ids else []
                by_id = {artist.id: artist for artist, value in rows}
                artists = [by_id[identity] for identity in ids]
                availability = {artist.id: availability_snapshot(value, self.account_id) for artist, value in rows}
                local = self._artist_local_summaries(session, artists)
                items = [{"kind": "artist", "id": artist.id, "pixiv_id": artist.pixiv_user_id, "name": artist.name,
                    "avatar": media_url(artist.avatar_path), "availability": availability.get(artist.id),
                    **local[artist.id]} for artist in artists]
            return {"items": items, "total": total, "offset": offset, "limit": limit,
                    "counts": counts, "catalogue_revision": catalogue_revision(session)}
