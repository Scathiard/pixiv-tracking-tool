"""Structured synchronous endpoints; blocking library/network work uses FastAPI's pool."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ..core.library import LibraryFilter
from ..core.models import utcnow
from ..core.tasks import safe_failure


class TaskSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["check", "metadata", "bookmarks", "repair", "verify", "backfill", "thumbnails"]
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
          "backfill": "补回所选动图原包", "thumbnails": "生成缩略图"}


def parse_filters(parameters) -> LibraryFilter:
    def csv(name, numeric=False):
        if name not in parameters:
            return None
        values = tuple(v for v in parameters[name].split(",") if v)
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
    q = parameters.get("q", "").strip()
    if len(q) > 200:
        raise ValueError("搜索文字过长")
    for name in ("favorites", "recent"):
        if parameters.get(name, "0") not in ("0", "1"):
            raise ValueError("筛选条件无效")
    return LibraryFilter(artist_ids=csv("artist_ids", True), types=csv("types"),
        tags=csv("tags") or (), show_hidden=parameters.get("show_hidden") in ("1", "true"),
        posted_after=after, posted_before=before, sort=parameters.get("sort", "newest"),
        q=q, favorites=parameters.get("favorites") == "1", recent=parameters.get("recent") == "1",
        as_of=reference)


def create_router(get_runtime):
    router = APIRouter()
    from .settings_api import create_settings_router
    router.include_router(create_settings_router(get_runtime))

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
    def library(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), ceiling: int | None = Query(None, ge=0), compact: bool = False):
        parameters = dict(request.query_params)
        parameters.setdefault("as_of", utcnow().isoformat())
        try:
            selected = parse_filters(parameters)
        except ValueError as exc:
            raise HTTPException(422, "筛选条件无效") from exc
        return {**runtime().library.query(selected, offset=offset, limit=limit, ceiling=ceiling, compact=compact), "as_of": parameters["as_of"]}

    @router.get("/api/library/feed")
    def feed(request: Request, offset: int = Query(0, ge=0), limit: int = Query(48, ge=1, le=120), ceiling: int = Query(..., ge=0)):
        return runtime().library.feed(filters(request), offset=offset, limit=limit, ceiling=ceiling)

    @router.get("/api/library/locate")
    def locate(request: Request, work_id: int, page: int = Query(0, ge=0), ceiling: int = Query(..., ge=0)):
        try:
            return runtime().library.locate(work_id, filters(request), ceiling=ceiling, page=page)
        except KeyError as exc:
            raise HTTPException(404, "阅读位置已不在当前范围内") from exc

    @router.get("/api/library/tags")
    def tags(request: Request, ceiling: int | None = Query(None, ge=0)):
        return {"tags": runtime().library.tags(filters(request), ceiling=ceiling)}

    @router.get("/api/library/missing-originals")
    def missing_originals(offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100)):
        return {"items": runtime().library.missing_originals(offset, limit)}

    @router.get("/api/works/{work_id}")
    def work(work_id: int, page_offset: int = Query(0, ge=0), page_limit: int = Query(100, ge=1, le=200)):
        try:
            return runtime().library.detail(work_id, page_offset=page_offset, page_limit=page_limit)
        except KeyError as exc:
            raise HTTPException(404, "作品不存在或已删除") from exc

    @router.get("/api/works/{work_id}/neighbors")
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
    def bookmark_state(work_id: int):
        try:
            return runtime().bookmarks.state(work_id)
        except LookupError as exc:
            raise HTTPException(404, "作品不存在") from exc

    @router.get("/api/bookmark-operations/{operation_id}")
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
    def artists(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                q: str = Query("", max_length=200)):
        return runtime().library.artist_page(offset=offset, limit=limit, q=q)

    @router.get("/api/artists/search")
    def search_artist(query: str = Query(..., min_length=1, max_length=200)):
        try:
            return {"items": runtime().remote.search_artist(query)}
        except Exception as exc:
            raise HTTPException(503, safe_failure(exc)["message"]) from exc

    @router.get("/api/artists/{artist_id}")
    def artist(artist_id: int):
        try:
            return runtime().library.artist_detail(artist_id)
        except KeyError as exc:
            raise HTTPException(404, "画师不存在") from exc

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
        if request.work_ids is not None and request.kind in ("check", "metadata", "bookmarks"):
            raise HTTPException(422, "此操作需要指定画师范围")
        if request.work_ids is not None and request.artist_ids is not None:
            raise HTTPException(422, "请选择一种任务范围")
        if request.kind == "backfill" and not request.work_ids:
            raise HTTPException(422, "先选择需要补回原包的作品")
        parameters = {}
        for name in ("artist_ids", "work_ids"):
            ids = getattr(request, name)
            if ids is not None:
                if any(value <= 0 for value in ids):
                    raise HTTPException(422, "无效的作品或画师编号")
                parameters[name] = sorted(set(ids))
        return runtime().queue.submit(request.kind, parameters, LABELS[request.kind])

    @router.get("/api/progress")
    def progress():
        current = runtime()
        return {**current.queue.snapshot(), "recovery_issues": current.recovery_issues()}

    @router.get("/api/tasks/{operation_id}")
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

    @router.get("/images/{path:path}")
    def media(path: str):
        file_path = runtime().media_path(path)
        if file_path is None:
            raise HTTPException(404, "图片不存在")
        return FileResponse(file_path)

    return router
