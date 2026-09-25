"""Local catalogue operations. No authentication, scheduler, or HTTP dependency."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import selectinload

from ..security import sanitize_caption

from .models import (
    ArchiveIntent, Artist, Asset, Derivative, LocalPreference, RemoteBookmark,
    TrackingPolicy, Work, WorkTag, utcnow,
)


@dataclass(frozen=True)
class LibraryFilter:
    artist_ids: tuple[int, ...] | None = None
    types: tuple[str, ...] | None = None
    tags: tuple[str, ...] = ()
    show_hidden: bool = False
    posted_after: datetime | None = None
    posted_before: datetime | None = None
    sort: str = "newest"

    def __post_init__(self):
        if self.sort not in ("newest", "oldest", "added"):
            raise ValueError("不支持的排序方式")
        if self.types is not None and any(t not in ("illust", "manga", "ugoira") for t in self.types):
            raise ValueError("不支持的作品类型")


def media_url(relative):
    return "/images/" + quote(relative, safe="/") if relative else None


class Library:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _statement(filters, ceiling):
        statement = select(Work).where(Work.id <= ceiling, ~Work.pixiv_illust_id.in_(
            select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed")))
        if not filters.show_hidden:
            statement = statement.where(~Work.id.in_(select(LocalPreference.work_id).where(LocalPreference.is_hidden.is_(True))))
        if filters.artist_ids is not None:
            statement = statement.where(Work.artist_id.in_(filters.artist_ids))
        if filters.types is not None:
            statement = statement.where(Work.type.in_(filters.types))
        if filters.tags:
            statement = statement.where(Work.id.in_(select(WorkTag.work_id).where(WorkTag.tag.in_(filters.tags))))
        if filters.posted_after is not None:
            statement = statement.where(Work.posted_at >= filters.posted_after)
        if filters.posted_before is not None:
            statement = statement.where(Work.posted_at < filters.posted_before)
        return statement

    @staticmethod
    def _order(filters):
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
    def _work_view(work):
        return {"id": work.id, "pixiv_id": work.pixiv_illust_id, "title": work.title,
            "artist_id": work.artist_id, "artist_name": work.artist.name,
            "type": work.type, "page_count": work.page_count,
            "caption": sanitize_caption(work.caption), "caption_state": work.caption_state,
            "tags": [tag.tag for tag in work.tags],
            "is_hidden": bool(work.preference and work.preference.is_hidden),
            "rating": work.preference.rating if work.preference else 0,
            "is_bookmarked": bool(work.bookmark and work.bookmark.value),
            "bookmark_confirmed_at": work.bookmark.confirmed_at.isoformat() if work.bookmark and work.bookmark.confirmed_at else None,
            "bookmark_count": work.bookmark_count, "view_count": work.view_count,
            "posted_at": work.posted_at.isoformat() if work.posted_at else None,
            "time_basis": work.time_basis, "x_restrict": work.x_restrict,
            "restriction_verified": work.restriction_verified}

    def artists(self):
        with self.database.read() as session:
            artists = session.scalars(select(Artist).options(selectinload(Artist.policy)).order_by(Artist.name, Artist.id))
            return [{"id": artist.id, "pixiv_id": artist.pixiv_user_id, "name": artist.name,
                "account": artist.account, "avatar_url": artist.avatar_url,
                "mode": artist.policy.mode if artist.policy else "stopped",
                "added_at": artist.added_at.isoformat() if artist.added_at else None,
                "last_checked_at": artist.last_checked_at.isoformat() if artist.last_checked_at else None}
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

    def query(self, filters=LibraryFilter(), *, offset=0, limit=100, ceiling=None):
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("分页范围不合法")
        with self.database.read() as session:
            ceiling = ceiling if ceiling is not None else session.scalar(select(func.max(Work.id))) or 0
            statement = self._statement(filters, ceiling)
            total = session.scalar(select(func.count()).select_from(statement.subquery()))
            works = list(session.scalars(self._related(statement).order_by(*self._order(filters)).offset(offset).limit(limit)))
            views = [self._work_view(work) for work in works]
            ids = [work.id for work in works]
            thumbnails = {row.work_id: row for row in session.scalars(select(Derivative).where(
                Derivative.work_id.in_(ids), Derivative.kind == "thumbnail", Derivative.page_index == 0,
                Derivative.spec_key == "v1", Derivative.state == "verified"))}
            first_pages = {row.work_id: row for row in session.scalars(select(Asset).where(
                Asset.work_id.in_(ids), Asset.role == "image", Asset.page_index == 0))}
            animations = {row.work_id: row for row in session.scalars(select(Derivative).where(
                Derivative.work_id.in_(ids), Derivative.kind == "gif", Derivative.spec_key == "v1"))}
            for view in views:
                thumb = thumbnails.get(view["id"])
                preview = animations.get(view["id"]) if view["type"] == "ugoira" else first_pages.get(view["id"])
                view["preview"] = media_url(preview.relative_path) if preview else None
                # Large originals are deliberately not the card fallback.
                view["thumbnail"] = media_url(thumb.relative_path) if thumb else None
            return {"items": views, "total": total, "offset": offset, "has_more": offset + len(views) < total,
                    "ceiling": ceiling}

    def detail(self, work_id, *, page_offset=0, page_limit=100):
        if page_offset < 0 or not 1 <= page_limit <= 200:
            raise ValueError("页面范围不合法")
        with self.database.read() as session:
            work = session.scalar(self._related(self._statement(LibraryFilter(show_hidden=True), work_id)).where(Work.id == work_id))
            if work is None:
                raise KeyError(work_id)
            view = self._work_view(work)
            count = 1 if work.type == "ugoira" else work.page_count
            assets = list(session.scalars(select(Asset).where(Asset.work_id == work_id,
                Asset.page_index >= page_offset, Asset.page_index < page_offset + page_limit)))
            originals = {asset.page_index: asset for asset in assets if asset.role == "image"}
            preview = session.scalar(select(Derivative).where(Derivative.work_id == work_id,
                Derivative.kind == "gif", Derivative.spec_key == "v1")) if work.type == "ugoira" else None
            view["pages"] = []
            for index in range(page_offset, min(count, page_offset + page_limit)):
                record = preview if work.type == "ugoira" else originals.get(index)
                view["pages"].append({"index": index, "src": media_url(record.relative_path) if record else None,
                    "width": record.width if record else None, "height": record.height if record else None,
                    "state": record.state if record else "missing", "error": record.error if record else None})
            view["assets"] = [{"id": asset.id, "role": asset.role, "page": asset.page_index,
                               "state": asset.state, "error": asset.error} for asset in assets]
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

    def missing_originals(self, offset=0, limit=100):
        with self.database.read() as session:
            statement = self._statement(LibraryFilter(show_hidden=True), 2**63 - 1).where(
                Work.type == "ugoira", Work.id.in_(select(Asset.work_id).where(
                    Asset.role.in_(("ugoira_zip", "ugoira_timing")), Asset.state != "verified")))
            works = session.scalars(self._related(statement).order_by(Work.id).offset(max(0, offset)).limit(min(max(1, limit), 100)))
            return [self._work_view(work) for work in works]
