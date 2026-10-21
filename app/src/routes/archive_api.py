"""Structured endpoints; blocking reads use admission and preparation waits asynchronously."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from time import monotonic
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import QueryParams

from ..core.library import LibraryFilter
from ..core.read_projection import CursorExpired
from ..core.read_control import local_read, ReadCancelled, LocalReadBusy
from ..core import diagnostics
from ..core.models import utcnow
from ..core.tasks import safe_failure


class TaskSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["check", "metadata", "bookmarks", "repair", "verify", "backfill", "thumbnails", "reconcile", "compare"]
    artist_ids: list[int] | None = Field(default=None, max_length=1000)
    work_ids: list[int] | None = Field(default=None, max_length=1000)


class PreferenceEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hidden: bool | None = None
    rating: int | None = Field(default=None, ge=0, le=5)


class TrackingEdit(BaseModel):
    mode: Literal["active", "paused", "stopped"]


class BookmarkEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    desired: bool
    request_id: UUID
    expected_account_id: str = Field(min_length=1)


class DeleteRequest(BaseModel):
    work_ids: list[int] = Field(min_length=1, max_length=1000)
    confirmed: Literal[True]


class ArtistAdd(BaseModel):
    pixiv_id: str = Field(pattern=r"^[0-9]+$", max_length=32)


LABELS = {"check": "检查新作品", "metadata": "刷新信息", "bookmarks": "同步收藏状态", "repair": "补下载", "verify": "全量核验本地文件",
          "backfill": "补回所选动图原包", "thumbnails": "生成缩略图", "reconcile": "检查并补齐图库", "compare": "重新比对作品原图"}


def parse_filters(parameters) -> LibraryFilter:
    if any(name in parameters and name + "_exclude" in parameters for name in ("artist_ids", "tags")):
        raise ValueError("同一筛选不能同时包含选择和反选")
    def csv(name, numeric=False):
        if name not in parameters:
            return None
        raw = parameters.getlist(name) if hasattr(parameters, "getlist") else parameters[name]
        repeated_tags = name in ("tags", "tags_exclude") and isinstance(raw, (tuple, list)) and len(raw) > 1
        values = tuple(v for v in (raw if repeated_tags else parameters[name].split(",")) if v)
        return tuple(sorted(set(int(v) for v in values))) if numeric else tuple(sorted(set(values)))

    after, before = None, None
    reference = datetime.fromisoformat(parameters["as_of"]) if parameters.get("as_of") else utcnow()
    if reference.tzinfo is not None:
        raise ValueError("快照时间格式无效")
    period = parameters.get("period")
    intervals = {"24h": 1, "week": 7, "month": 30, "half_year": 183, "year": 365}
    if period in intervals:
        after = reference - timedelta(days=intervals[period])
    elif period == "custom":
        if parameters.get("posted_after"):
            after = datetime.fromisoformat(parameters["posted_after"])
        if parameters.get("posted_before"):
            before = datetime.fromisoformat(parameters["posted_before"]) + timedelta(days=1)
    elif period:
        raise ValueError("不支持的日期范围")
    if any(value and value.tzinfo is not None for value in (after, before)):
        raise ValueError("日期筛选请使用 YYYY-MM-DD")
    if after is not None and before is not None and after >= before:
        raise ValueError("起始日期不能晚于结束日期")
    q = parameters.get("q", "").strip()
    if len(q) > 200:
        raise ValueError("搜索文字过长")
    for name in ("favorites", "recent"):
        if parameters.get(name, "0") not in ("0", "1"):
            raise ValueError("筛选条件无效")
    offset = int(parameters.get("tz_offset_minutes", "0")) if period == "custom" else 0
    if not -840 <= offset <= 840:
        raise ValueError("日期时区无效")
    return LibraryFilter(artist_ids=csv("artist_ids", True), types=csv("types"),
        tags=csv("tags"), show_hidden=parameters.get("show_hidden") in ("1", "true"),
        posted_after=after, posted_before=before, sort=parameters.get("sort", "newest"),
        q=q, favorites=parameters.get("favorites") == "1", recent=parameters.get("recent") == "1",
        as_of=reference, artist_ids_exclude=csv("artist_ids_exclude", True), tags_exclude=csv("tags_exclude"), tz_offset_minutes=offset)


def create_router(get_runtime):
    router = APIRouter()
    from .settings_api import create_settings_router
    router.include_router(create_settings_router(get_runtime))
    from .artists_api import create_artists_router
    router.include_router(create_artists_router(get_runtime))

    def runtime():
        value = get_runtime()
        if value is None:
            raise HTTPException(503, "图库尚未准备好；旧数据需要先完成明确的升级步骤")
        return value

    def filters(request):
        try:
            return parse_filters(request.query_params)
        except ValueError as exc:
            raise HTTPException(422, "筛选条件无效") from exc

    @router.get("/api/library")
    @local_read(get_runtime)
    def library(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), ceiling: int | None = Query(None, ge=0), compact: bool = False):
        pairs = list(request.query_params.multi_items())
        if "as_of" not in request.query_params:
            pairs.append(("as_of", utcnow().isoformat()))
        parameters = QueryParams(pairs)
        try:
            selected = parse_filters(parameters)
        except ValueError as exc:
            raise HTTPException(422, "筛选条件无效") from exc
        return {**runtime().library.query(selected, offset=offset, limit=limit, ceiling=ceiling, compact=compact), "as_of": parameters["as_of"]}

    def cursor_failure(exc):
        if isinstance(exc, CursorExpired):
            raise HTTPException(409, {"code": "cursor_expired",
                "message": "图库已变化，当前位置仍保留；重新定位后可继续",
                "recoverable": True}) from exc
        raise HTTPException(422, "游标或筛选条件无效") from exc

    def cursor_filters(request, cursor):
        try:
            parameters = request.query_params
            if cursor and "as_of" not in parameters:
                parameters = QueryParams([*parameters.multi_items(),
                    ("as_of", runtime().library.cursor_reference(cursor))])
            return parse_filters(parameters)
        except ValueError as exc:
            cursor_failure(exc)

    @router.get("/api/library/cursor")
    @local_read(get_runtime)
    def cursor_library(request: Request, limit: int = Query(100, ge=1, le=500),
                       cursor: str | None = Query(None, max_length=8192),
                       direction: Literal["next", "previous"] = "next",
                       ceiling: int | None = Query(None, ge=0), compact: bool = False,
                       anchor_work_id: int | None = Query(None, gt=0)):
        try:
            return runtime().library.list_cursor(cursor_filters(request, cursor), limit=limit, cursor=cursor,
                direction=direction, ceiling=ceiling, compact=compact, anchor_work_id=anchor_work_id)
        except ValueError as exc:
            cursor_failure(exc)
        except KeyError as exc:
            raise HTTPException(404, "当前位置已移出这个范围；请从范围开头继续") from exc

    @router.get("/api/works/{work_id}/cursor-neighbors")
    @local_read(get_runtime)
    def cursor_neighbors(request: Request, work_id: int,
                         ceiling: int | None = Query(None, ge=0),
                         cursor: str | None = Query(None, max_length=8192),
                         from_cursor: str | None = Query(None, max_length=8192),
                         from_direction: Literal["next", "previous"] = "next"):
        try:
            return runtime().library.cursor_neighbors(work_id, cursor_filters(request, cursor),
                ceiling=ceiling, cursor=cursor, from_cursor=from_cursor, from_direction=from_direction)
        except ValueError as exc:
            cursor_failure(exc)
        except KeyError as exc:
            raise HTTPException(404, "作品已移出这个范围；请重新定位") from exc

    @router.get("/api/library/cursor/feed")
    @local_read(get_runtime)
    def cursor_feed(request: Request, limit: int = Query(48, ge=1, le=120),
                    cursor: str | None = Query(None, max_length=8192),
                    direction: Literal["next", "previous"] = "next",
                    ceiling: int | None = Query(None, ge=0),
                    anchor_work_id: int | None = Query(None, gt=0),
                    page: int = Query(0, ge=-1), index: int = Query(0, ge=0),
                    work_index: int = Query(0, ge=0)):
        # Ordinals are browser-local hints; identity, membership and image
        # slots are resolved together by Library in one read snapshot.
        try:
            return runtime().library.cursor_feed(cursor_filters(request, cursor), limit=limit,
                cursor=cursor, direction=direction, ceiling=ceiling, anchor_work_id=anchor_work_id,
                page=page, index=index, work_index=work_index)
        except ValueError as exc:
            cursor_failure(exc)
        except KeyError as exc:
            raise HTTPException(404, "当前位置已移出这个范围；请重新定位") from exc

    @router.get("/api/library/feed")
    @local_read(get_runtime)
    def feed(request: Request, offset: int = Query(0, ge=0), limit: int = Query(48, ge=1, le=120), ceiling: int = Query(..., ge=0)):
        return runtime().library.feed(filters(request), offset=offset, limit=limit, ceiling=ceiling)

    @router.get("/api/library/counts")
    @local_read(get_runtime)
    def counts(request: Request, ceiling: int | None = Query(None, ge=0)):
        return runtime().library.counts(filters(request), ceiling=ceiling)

    @router.post("/api/reading-sessions")
    async def reading_session(request: Request, ceiling: int | None = Query(None, ge=0)):
        # Waiting for the shared worker is not a database read. In particular,
        # four browsers waiting at startup must not occupy all frontend slots.
        current = runtime()
        selected = filters(request)
        pending = waiting = None
        began = monotonic()
        outcome = "ok"
        try:
            pending = current.reading.prepare(selected, ceiling=ceiling)
            waiting = asyncio.wrap_future(pending)
            while not waiting.done():
                await asyncio.wait({waiting}, timeout=.1)
                if await request.is_disconnected():
                    raise ReadCancelled("当前页面已关闭，已取消等待随机清单")
            visit = await waiting
            if await request.is_disconnected():
                raise ReadCancelled("当前页面已关闭，已取消等待随机清单")
            return current.reading.install(visit)
        except (ReadCancelled, asyncio.CancelledError) as exc:
            outcome = "cancelled"
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(499, {"code": "read_cancelled", "message": str(exc)}) from exc
        except LocalReadBusy as exc:
            outcome = "busy"
            raise HTTPException(503, {"code": "read_busy", "message": str(exc)},
                                headers={"Retry-After": "1"}) from exc
        except Exception as exc:
            outcome = "failed"
            diagnostics.emit("reading.prepare.error", error_type=type(exc).__name__)
            raise HTTPException(503, {"code": "reading_prepare_failed",
                "message": "随机清单准备失败，请重试；当前画面仍保留"},
                headers={"Retry-After": "1"}) from exc
        finally:
            # Each consumer owns only its Future; cancellation never stops the
            # runtime's scan, cache refresh or another browser's preparation.
            if pending is not None:
                pending.cancel()
            if waiting is not None:
                waiting.cancel()
                if waiting.done() and not waiting.cancelled():
                    waiting.exception()
            diagnostics.emit("reading.prepare.response", outcome=outcome,
                             total_ms=(monotonic() - began) * 1000)
            request.scope.setdefault("state", {})["diagnostic_business_ready_at"] = monotonic()

    @router.delete("/api/reading-sessions/{session_id}")
    def close_reading(session_id: str):
        runtime().reading.release(session_id)
        return {"ok": True}

    @router.post("/api/reading-sessions/{session_id}/exclude/{work_id}")
    def exclude_reading(session_id: str, work_id: int):
        try:
            runtime().reading.exclude(session_id, work_id)
        except KeyError as exc:
            raise HTTPException(410, "阅读范围已过期，请重新打开图库；本地作品未改变") from exc
        return {"ok": True}

    @router.get("/api/reading-sessions/{session_id}/entry")
    @local_read(get_runtime)
    def reading_entry(session_id: str, index: int = Query(0, ge=0), mode: Literal["sequential", "random"] = "sequential", round: int = Query(0, ge=0)):
        try:
            return runtime().reading.entry(session_id, index, mode=mode, round_number=round)
        except KeyError as exc:
            raise HTTPException(410, "阅读范围已过期或作品已移出，请重新打开图库") from exc

    @router.get("/api/reading-sessions/{session_id}/locate")
    @local_read(get_runtime)
    def reading_locate(session_id: str, work_id: int, page: int = Query(0, ge=0), mode: Literal["sequential", "random"] = "sequential", round: int = Query(0, ge=0)):
        try:
            return runtime().reading.locate(session_id, work_id, page=page, mode=mode, round_number=round)
        except KeyError as exc:
            raise HTTPException(410, "阅读范围已过期或作品已移出，请重新打开图库") from exc

    @router.get("/api/reading-sessions/{session_id}/feed")
    @local_read(get_runtime)
    def reading_feed(session_id: str, offset: int = Query(0, ge=0), limit: int = Query(48, ge=1, le=120), mode: Literal["sequential", "random"] = "sequential"):
        try:
            return runtime().reading.feed(session_id, offset=offset, limit=limit, mode=mode)
        except KeyError as exc:
            raise HTTPException(410, "阅读范围已过期，请重新打开图库；本地作品未改变") from exc

    @router.get("/api/library/locate")
    @local_read(get_runtime)
    def locate(request: Request, work_id: int, page: int = Query(0, ge=0), ceiling: int = Query(..., ge=0)):
        try:
            return runtime().library.locate(work_id, filters(request), ceiling=ceiling, page=page)
        except KeyError as exc:
            raise HTTPException(404, "阅读位置已不在当前范围内") from exc

    @router.get("/api/library/tags")
    @local_read(get_runtime)
    def tags(request: Request, ceiling: int | None = Query(None, ge=0)):
        return runtime().library.tag_catalogue(filters(request), ceiling=ceiling)

    @router.get("/api/library/missing-originals")
    @local_read(get_runtime)
    def missing_originals(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
        return {"items": runtime().library.missing_originals(offset, limit)}

    @router.get("/api/works/{work_id}")
    @local_read(get_runtime)
    def work(work_id: int, page_offset: int = Query(0, ge=0), page_limit: int = Query(100, ge=1, le=200), revision_id: str | None = Query(None, max_length=64)):
        try:
            return runtime().library.detail(work_id, page_offset=page_offset, page_limit=page_limit, revision_id=revision_id)
        except KeyError as exc:
            raise HTTPException(404, "作品不存在或已删除") from exc

    @router.get("/api/works/{work_id}/revisions")
    @local_read(get_runtime)
    def work_revisions(work_id: int, before_ordinal: int | None = Query(None, ge=1),
                       limit: int = Query(100, ge=1, le=100)):
        try:
            return runtime().library.revision_page(work_id, before_ordinal=before_ordinal, limit=limit)
        except KeyError as exc:
            raise HTTPException(404, "作品不存在或已删除") from exc

    @router.get("/api/works/{work_id}/neighbors")
    @local_read(get_runtime)
    def neighbors(request: Request, work_id: int, ceiling: int = Query(..., ge=0)):
        try:
            return runtime().library.neighbors(work_id, filters(request), ceiling=ceiling)
        except KeyError as exc:
            raise HTTPException(404, "作品不在当前范围中") from exc

    @router.patch("/api/works/{work_id}/preferences")
    def preferences(work_id: int, edit: PreferenceEdit):
        try:
            runtime().library.set_preferences(work_id, hidden=edit.hidden, rating=edit.rating)
        except KeyError as exc:
            raise HTTPException(404, "作品不存在") from exc
        return {"ok": True}

    @router.post("/api/works/{work_id}/bookmark")
    def bookmark(work_id: int, edit: BookmarkEdit):
        try:
            return runtime().bookmarks.submit(work_id, edit.desired, str(edit.request_id),
                                              expected_account_id=edit.expected_account_id)
        except LookupError as exc:
            raise HTTPException(404, "作品不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503, safe_failure(exc)["message"]) from exc

    @router.post("/api/works/{work_id}/bookmark/refresh")
    def refresh_bookmark(work_id: int, automatic: bool = False):
        try:
            return runtime().bookmarks.refresh(work_id, automatic=automatic)
        except LookupError as exc:
            raise HTTPException(404, "作品不存在") from exc
        except Exception as exc:
            raise HTTPException(503, safe_failure(exc)["message"]) from exc

    @router.get("/api/works/{work_id}/bookmark")
    @local_read(get_runtime)
    def bookmark_state(work_id: int):
        try:
            return runtime().bookmarks.state(work_id)
        except LookupError as exc:
            raise HTTPException(404, "作品不存在") from exc

    @router.get("/api/bookmark-operations/{operation_id}")
    @local_read(get_runtime)
    def bookmark_operation(operation_id: str):
        try:
            return runtime().bookmarks.operation(operation_id)
        except LookupError as exc:
            raise HTTPException(404, "收藏操作不存在") from exc

    @router.post("/api/works/delete")
    def delete(request: DeleteRequest):
        try:
            return runtime().delete(request.work_ids)
        except KeyError as exc:
            raise HTTPException(404, "部分作品不存在，已提交的删除决定仍保留") from exc

    @router.get("/api/artists")
    @local_read(get_runtime)
    def artists(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                q: str = Query("", max_length=200), compact: bool = False):
        return runtime().library.artist_page(offset=offset, limit=limit, q=q, compact=compact)

    @router.get("/api/artists/search")
    def search_artist(query: str = Query(..., min_length=1, max_length=200)):
        try:
            return {"items": runtime().remote.search_artist(query)}
        except Exception as exc:
            raise HTTPException(503, safe_failure(exc)["message"]) from exc

    @router.get("/api/artists/{artist_id}")
    @local_read(get_runtime)
    def artist(artist_id: int):
        try:
            return runtime().library.artist_detail(artist_id)
        except KeyError as exc:
            raise HTTPException(404, "画师不存在") from exc

    @router.post("/api/artists/{artist_id}/check-source")
    def check_artist_source(artist_id: int):
        try:
            cached = runtime().library.artist_detail(artist_id)
            return runtime().queue.submit("artist_source", {"artist_ids": [artist_id]},
                                          f"核对 {cached['name']} 的主站主页")
        except KeyError as exc:
            raise HTTPException(404, "画师不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(409, safe_failure(exc)["message"]) from exc

    @router.get("/api/library/source-status")
    @local_read(get_runtime)
    def source_overview(kind: Literal["works", "artists"] = "works",
                        group: Literal["all", "deleted", "unavailable"] = "all",
                        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        return runtime().library.source_overview(kind=kind, group=group, offset=offset, limit=limit)

    @router.post("/api/artists")
    def add_artist(request: ArtistAdd):
        try:
            return runtime().add_artist(request.pixiv_id)
        except Exception as exc:
            raise HTTPException(503, safe_failure(exc)["message"]) from exc

    @router.patch("/api/artists/{artist_id}/tracking")
    def tracking(artist_id: int, edit: TrackingEdit):
        try:
            runtime().library.set_tracking(artist_id, edit.mode)
        except KeyError as exc:
            raise HTTPException(404, "画师不存在") from exc
        return {"ok": True, "mode": edit.mode}

    @router.delete("/api/artists/{artist_id}")
    def delete_artist(artist_id: int, delete_files: Literal[True] = Query(...)):
        try:
            return {"ok": True, **runtime().delete_artist(artist_id)}
        except KeyError as exc:
            raise HTTPException(404, "画师不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(409, safe_failure(exc)["message"]) from exc

    @router.post("/api/tasks")
    def submit(request: TaskSubmission):
        if request.work_ids is not None and request.kind in ("check", "bookmarks"):
            raise HTTPException(422, "此操作需要指定画师范围")
        if request.work_ids is not None and request.artist_ids is not None:
            raise HTTPException(422, "请选择一种任务范围")
        if request.kind == "backfill" and not request.work_ids:
            raise HTTPException(422, "先选择需要补回原包的作品")
        if request.kind == "compare" and (not request.work_ids or len(request.work_ids) != 1):
            raise HTTPException(422, "重新比对原图只用于一件指定作品")
        parameters = {}
        for name in ("artist_ids", "work_ids"):
            ids = getattr(request, name)
            if ids is not None:
                if any(value <= 0 for value in ids):
                    raise HTTPException(422, "无效的作品或画师编号")
                parameters[name] = sorted(set(ids))
        return runtime().queue.submit(request.kind, parameters, LABELS[request.kind])

    @router.get("/api/progress")
    @local_read(get_runtime, coalesce=True,
                coalesce_context=lambda current: current.queue.byte_progress_generation)
    def progress(watch_ids: str = Query("", max_length=8192)):
        current = runtime()
        watched = list(dict.fromkeys(ident for ident in watch_ids.split(",") if ident))
        if len(watched) > 64 or any(len(ident) > 128 for ident in watched):
            raise HTTPException(422, "当前任务关注范围过大，请刷新")
        return {**current.queue.snapshot(watch_ids=watched), "recovery_issues": current.recovery_issues()}

    @router.get("/api/tasks/{operation_id}")
    @local_read(get_runtime)
    def task_details(operation_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100), problems_only: bool = False):
        try:
            return runtime().queue.details(operation_id, offset, limit, problems_only)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    @router.post("/api/tasks/{operation_id}/acknowledge")
    def acknowledge(operation_id: str):
        try:
            runtime().queue.acknowledge(operation_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        return {"ok": True}

    @router.get("/api/tasks")
    @local_read(get_runtime)
    def history(before: str | None = None, limit: int = Query(20, ge=1, le=100), attention_only: bool = False):
        try:
            return runtime().queue.history(before, limit, attention_only)
        except KeyError as exc:
            raise HTTPException(404, "任务记录不存在") from exc

    @router.post("/api/tasks/{operation_id}/retry")
    def retry(operation_id: str):
        try:
            return runtime().queue.retry(operation_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/tasks/{operation_id}/pause")
    def pause_task(operation_id: str):
        try:
            return runtime().queue.set_manual_paused(operation_id, True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/api/tasks/{operation_id}/resume")
    def resume_task(operation_id: str):
        try:
            return runtime().queue.set_manual_paused(operation_id, False)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/images/{path:path}")
    @local_read(get_runtime)
    def media(path: str, request: Request):
        file_path = runtime().media_path(path)
        if file_path is None:
            raise HTTPException(404, "图片不存在")
        from .media_response import local_media_response
        return local_media_response(request, file_path)

    return router
