"""v0.1.2 compatibility adapter: existing HTML controls use the archive service."""
from __future__ import annotations

import math
import os
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select

from ..core.library import LibraryFilter
from ..core.media import contained_path
from ..core.models import Artist, Asset, TrackingPolicy, Work
from ..core.tasks import safe_failure
from ..query_state import safe_redirect_target
from .archive_api import LABELS, parse_filters

TYPE_LABELS = {"illust": "插画", "manga": "漫画", "ugoira": "动图"}
PERIOD_LABELS = {"24h": "24小时内", "week": "一周内", "month": "一个月内", "half_year": "半年内", "year": "一年内", "custom": "指定期间"}


def _parameters(request):
    params = dict(request.query_params)
    for name in ("tags", "tags_exclude"):
        values = request.query_params.getlist(name)
        if len(values) > 1:
            params[name] = values
    return params


def _local_url(path, params):
    query = urlencode([(key, value) for key, value in params.items() if value is not None], doseq=True)
    return f"{path}?{query}" if query else path


def _work(view):
    return SimpleNamespace(**{**view, "pixiv_illust_id": view["pixiv_id"],
        "primary_file_path": view.get("thumbnail") or view.get("preview"),
        "posted_at": datetime.fromisoformat(view["posted_at"]) if view.get("posted_at") else None})


def _artist(view):
    return SimpleNamespace(**{**view, "pixiv_user_id": view["pixiv_id"], "pixiv_account": view["account"] or "",
        "is_active": view["mode"] == "active", "added_at": datetime.fromisoformat(view["added_at"]) if view.get("added_at") else None,
        "last_checked_at": datetime.fromisoformat(view["last_checked_at"]) if view["last_checked_at"] else None})


def create_pages(get_runtime, templates, config, *, switch_root=None):
    router = APIRouter()

    def runtime():
        value = get_runtime()
        if value is None:
            raise HTTPException(503, "旧图库需要先完成明确的升级步骤；原数据未改动")
        return value

    def render(request, name, context, status=200):
        return templates.TemplateResponse(request, name, {"archive_runtime": True,
            "pixiv_logged_in": get_runtime() is not None and get_runtime().client is not None, **context}, status_code=status)

    def submission(kind, parameters, label=None):
        result = runtime().queue.submit(kind, parameters, label or LABELS[kind])
        return JSONResponse({"ok": True, "job_id": result["operation_id"], **result})

    def gallery_context(request, *, offset=None):
        current = runtime()
        params = _parameters(request)
        if "artist_id" in params and "artist_ids" not in params:
            params["artist_ids"] = params.pop("artist_id")
        if "type" in params and "types" not in params:
            params["types"] = params.pop("type")
        filters = parse_filters(params)
        page_size = config.PAGE_SIZE
        page = max(1, int(params.get("page", 1)))
        offset = max(0, int(params.get("offset", (page - 1) * page_size))) if offset is None else offset
        ceiling = int(params["ceiling"]) if params.get("ceiling") else None
        result = current.library.query(filters, offset=offset, limit=page_size, ceiling=ceiling)
        params["ceiling"] = result["ceiling"]
        for key in ("offset", "fragment", "page", "page_offset"):
            params.pop(key, None)

        def url(path="/", **overrides):
            return _local_url(path, {**params, **overrides})

        artists = [_artist(view) for view in current.library.artists()]
        all_ids, all_types = {artist.id for artist in artists}, set(TYPE_LABELS)
        selected_ids = set(filters.artist_ids) if filters.artist_ids is not None else None
        selected_types = set(filters.types) if filters.types is not None else None

        def toggle(name, selected, universe, target):
            changed = (universe if selected is None else selected) ^ {target}
            return url(**{name: None if changed == universe else ",".join(map(str, sorted(changed)))})

        total_pages = max(1, math.ceil(result["total"] / page_size))
        current_page = offset // page_size + 1
        context = {"artists": artists, "all_artists": artists, "all_artist_ids": all_ids,
            "artists_map": {artist.id: artist for artist in artists}, "illustrations": [_work(v) for v in result["items"]],
            "type_labels": TYPE_LABELS, "selected_artist_ids": selected_ids, "selected_types": selected_types,
            "toggle_urls": {aid: toggle("artist_ids", selected_ids, all_ids, aid) for aid in all_ids},
            "type_toggle_urls": {kind: toggle("types", selected_types, all_types, kind) for kind in all_types},
            "artist_invert_url": url(artist_ids=",".join(map(str, sorted(all_ids - (all_ids if selected_ids is None else selected_ids))))),
            "type_invert_url": url(types=",".join(sorted(all_types - (all_types if selected_types is None else selected_types)))),
            "all_url": url(artist_ids=None, types=None), "preserve_url": lambda **kwargs: url(**kwargs),
            "show_hidden": filters.show_hidden, "show_hidden_toggle_url": url(show_hidden=None if filters.show_hidden else "1"),
            "period": params.get("period"), "posted_after": params.get("posted_after"), "posted_before": params.get("posted_before"),
            "period_labels": PERIOD_LABELS, "tag_list": current.library.tags(filters, ceiling=result["ceiling"]),
            "selected_tags": set(filters.tags or ()), "detail_query": url(""),
            "offset": offset, "page_size": page_size, "total_count": result["total"], "has_more": result["has_more"],
            "pagination_mode": config.PAGINATION_MODE, "max_visible_items": config.MAX_VISIBLE_ITEMS,
            "current_page": current_page, "total_pages": total_pages,
            "page_range": list(range(max(1, current_page - 3), min(total_pages, current_page + 3) + 1)),
            "filter_summary": f"画师:{'全部' if selected_ids is None else str(len(selected_ids)) + '位'} · {result['total']} 件作品"}
        return context, filters, result["ceiling"], url

    @router.get("/")
    def index(request: Request):
        if get_runtime() is None:
            return render(request, "error.html", {"message": "此旧图库尚未升级。请先在隔离副本核对转换结果，再明确批准切换；原数据未改动。"}, 503)
        context, _, _, _ = gallery_context(request)
        response = render(request, "gallery_fragment.html" if request.query_params.get("fragment") == "1" else "index.html", context)
        response.headers["X-Has-More"] = "1" if context["has_more"] else "0"
        return response

    @router.get("/artist/{artist_id}")
    def artist_gallery(request: Request, artist_id: int):
        params = _parameters(request)
        params["artist_ids"] = artist_id
        params.pop("artist_ids_exclude", None)
        if "type" in params:
            params["types"] = params.pop("type")
        return RedirectResponse(_local_url("/", params), 303)

    @router.get("/illust/{work_id}")
    def detail(request: Request, work_id: int):
        context, filters, ceiling, url = gallery_context(request, offset=0)
        page_offset = max(0, int(request.query_params.get("page_offset", 0)))
        try:
            view = runtime().library.detail(work_id, page_offset=page_offset, page_limit=200)
        except KeyError:
            return render(request, "error.html", {"message": "作品不存在或已删除"}, 404)
        try:
            neighbors = runtime().library.neighbors(work_id, filters, ceiling=ceiling)
        except KeyError:
            neighbors = {"previous": None, "next": None, "index": -1}
        previous, following = neighbors["previous"], neighbors["next"]
        context.update({"illust": _work(view), "artist": context["artists_map"].get(view["artist_id"]),
            "tags": view["tags"], "caption_html": view["caption"], "pages": view["pages"],
            "asset_states": view["assets"],
            "paths": [page["src"] for page in view["pages"]], "display_page_count": view["display_page_count"],
            "more_pages_url": url(f"/illust/{work_id}", page_offset=page_offset + 200) if view["has_more_pages"] else None,
            "page_offset": page_offset, "back_url": url(page=neighbors["index"] // config.PAGE_SIZE + 1),
            "self_url": url(f"/illust/{work_id}"), "prev_id": previous, "next_id": following,
            "prev_url": url(f"/illust/{previous}") if previous else None,
            "next_url": url(f"/illust/{following}") if following else None,
            "current_idx": neighbors["index"] + 1,
            "detail_toggle_urls": context["toggle_urls"], "detail_type_urls": context["type_toggle_urls"]})
        return render(request, "illust_detail.html", context)

    @router.get("/artists")
    def artists(request: Request, search: str = ""):
        current = runtime()
        results, error = [], None
        if search:
            try:
                results = [current.remote.get_artist_detail(search)] if search.isdigit() else current.remote.search_artist(search)
            except Exception as exc:
                error = safe_failure(exc)["message"]
        with current.database.read() as session:
            counts = dict(session.execute(select(Work.artist_id, func.count()).group_by(Work.artist_id)).all())
        return render(request, "artists.html", {"artists": [_artist(a) for a in current.library.artists()],
            "results": results, "search_term": search, "artist_counts": counts, "error": error})

    @router.post("/artists/add")
    def add_artist(user_id: str = Form(...)):
        try:
            return {"ok": True, **runtime().add_artist(user_id)}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": safe_failure(exc)["message"]}, 503)

    @router.post("/artists/{artist_id}/toggle")
    def toggle_tracking(artist_id: int):
        current = runtime()
        with current.database.read() as session:
            policy = session.get(TrackingPolicy, artist_id)
            mode = "paused" if policy and policy.mode == "active" else "active"
        current.library.set_tracking(artist_id, mode)
        return {"ok": True, "mode": mode, "message": "当前已启动批次继续完成；新设置对下一次自动检查生效"}

    @router.post("/artists/{artist_id}/remove-keep-files")
    def stop_tracking(artist_id: int):
        runtime().library.set_tracking(artist_id, "stopped")
        return {"ok": True, "mode": "stopped", "message": "已停止追踪，画师与已有作品保留在图库"}

    @router.post("/artists/{artist_id}/refresh")
    def refresh_artist(artist_id: int):
        return submission("metadata", {"artist_ids": [artist_id]})

    @router.post("/artists/{artist_id}/remove-and-files")
    def remove_artist_files(artist_id: int):
        from ..core.runtime import LibraryBusy
        try:
            return {"ok": True, **runtime().delete_artist(artist_id)}
        except LibraryBusy as exc:
            return JSONResponse({"ok": False, "error": exc.user_message}, 409)

    @router.post("/refresh")
    @router.post("/settings/check-now")
    def refresh_all(artist_ids: str | None = Form(None)):
        parameters = {} if artist_ids is None else {"artist_ids": sorted({int(part) for part in artist_ids.split(",") if part})}
        return submission("check", parameters)

    @router.post("/settings/download-pending")
    def repair_all():
        return submission("repair", {})

    @router.post("/settings/validate-and-fix")
    def validate():
        return submission("verify", {})

    @router.post("/settings/refresh-metadata")
    def refresh_metadata():
        return submission("metadata", {"artist_ids": [artist["id"] for artist in runtime().library.artists()]})

    @router.post("/api/sync-bookmarks")
    def sync_bookmarks():
        return submission("bookmarks", {})

    @router.post("/illust/{work_id}/hide")
    def hide(work_id: int):
        runtime().library.set_preferences(work_id, hidden=True)
        return {"ok": True, "message": "已隐藏"}

    @router.post("/illust/{work_id}/unhide")
    def unhide(work_id: int):
        runtime().library.set_preferences(work_id, hidden=False)
        return {"ok": True, "message": "已取消隐藏"}

    @router.post("/illust/{work_id}/delete")
    def delete(work_id: int):
        return {"ok": True, **runtime().delete([work_id])}

    @router.post("/illust/{work_id}/download-single")
    def repair(work_id: int):
        return submission("repair", {"work_ids": [work_id]})

    @router.post("/illust/{work_id}/bookmark")
    def bookmark(work_id: int):
        try:
            return runtime().bookmark(work_id)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": safe_failure(exc)["message"]}, 503)

    @router.post("/illust/{work_id}/open-folder")
    def open_folder(work_id: int):
        current = runtime()
        with current.database.read() as session:
            asset = session.scalar(select(Asset).where(Asset.work_id == work_id, Asset.relative_path.is_not(None)))
            if asset is None:
                raise HTTPException(404, "此作品还没有本地文件夹")
            folder = contained_path(current.images, asset.relative_path).parent
        os.startfile(str(folder))
        return {"ok": True}

    @router.get("/settings")
    def settings(request: Request):
        current = runtime()
        with current.database.read() as session:
            artist_count = session.scalar(select(func.count()).select_from(Artist))
            work_count = session.scalar(select(func.count()).select_from(Work))
            verified = session.scalar(select(func.count()).select_from(Work).where(
                Work.id.in_(select(Asset.work_id)), ~Work.id.in_(select(Asset.work_id).where(Asset.state != "verified"))))
        from ..networking import access_urls
        return render(request, "settings.html", {"artist_count": artist_count, "illust_count": work_count, "downloaded": verified,
            "check_interval_hours": config.CHECK_INTERVAL_HOURS, "check_interval_minutes": config.CHECK_INTERVAL_MINUTES,
            "port": config.PORT, "data_root": str(config.DATA_ROOT), "page_size": config.PAGE_SIZE,
            "pagination_mode": config.PAGINATION_MODE, "max_visible_items": config.MAX_VISIBLE_ITEMS,
            "network_urls": access_urls(getattr(config, "LISTENING_PORT", config.PORT)),
            "listening_port": getattr(config, "LISTENING_PORT", config.PORT)})

    @router.post("/settings/pagination")
    def pagination(page_size: int = Form(200), pagination_mode: str = Form("scroll"), max_visible_items: int = Form(0)):
        config.set_pagination(page_size, pagination_mode, max_visible_items)
        return {"ok": True, "message": "已保存翻页设置"}

    @router.post("/settings/port")
    def port(port: int = Form(...)):
        config.set_port(port)
        return {"ok": True, "message": f"已保存端口 {port}；重启前仍使用当前访问地址"}

    @router.post("/settings/change-path")
    def change_path(new_path: str = Form(...)):
        if switch_root is None:
            raise HTTPException(503, "此运行实例不提供数据目录切换")
        return switch_root(new_path)

    @router.post("/settings/browse-path")
    def browse_path():
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        try:
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(parent=root, initialdir=str(config.DATA_ROOT), title="选择数据存储目录")
            return {"path": path}
        finally:
            root.destroy()

    return router
