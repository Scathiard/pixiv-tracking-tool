"""Bounded, explicit identity preview and local check feedback; no background search."""
from __future__ import annotations
import re
from itertools import islice
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from ..core.models import Artist, Operation
from ..core.tasks import safe_failure


def artist_identity(query):
    query = query.strip()
    if re.fullmatch(r"[0-9]{1,32}", query):
        return query
    if "://" in query:
        parsed = urlparse(query)
        match = re.fullmatch(r"/(?:[a-z]{2}/)?users/([0-9]{1,32})/?", parsed.path)
        if parsed.scheme != "https" or parsed.hostname not in ("pixiv.net", "www.pixiv.net") or not match:
            raise ValueError("请使用 Pixiv 画师主页地址，如 https://www.pixiv.net/users/123")
        return match[1]
    return None


def create_artists_router(get_runtime):
    router = APIRouter()

    def current():
        runtime = get_runtime()
        if runtime is None:
            raise HTTPException(503, "图库尚未准备好")
        return runtime

    def with_local(runtime, values):
        values = list(islice(values,20))
        ids = [str(value.get("user_id", "")) for value in values]
        with runtime.database.read() as session:
            known = dict(session.execute(select(Artist.pixiv_user_id, Artist.id).where(Artist.pixiv_user_id.in_(ids))).all())
        return [{**value, "artist_id": known.get(str(value.get("user_id")))} for value in values]

    def check_views(runtime, ids):
        with runtime.database.read() as session:
            artists = list(session.scalars(select(Artist).where(Artist.id.in_(ids))))
            selected_artist = Operation.parameters["artist_ids"][0].as_integer()
            ranked = select(Operation.id, Operation.state, Operation.result, Operation.finished_at,
                selected_artist.label("artist_id"), func.row_number().over(partition_by=selected_artist,
                    order_by=(Operation.created_at.desc(), Operation.id.desc())).label("position")).where(
                        Operation.kind == "check", selected_artist.in_(ids),
                        func.json_array_length(Operation.parameters["artist_ids"]) == 1).subquery()
            operations = {row["artist_id"]: row for row in session.execute(select(ranked).where(ranked.c.position == 1)).mappings()}
            views = {}
            for artist in artists:
                operation = operations.get(artist.id)
                views[artist.id] = {"checked_at": artist.last_checked_at.isoformat() + "Z" if artist.last_checked_at else None,
                    "operation": {"id": operation["id"], "state": operation["state"], "result": operation["result"],
                                  "finished_at": operation["finished_at"].isoformat() + "Z" if operation["finished_at"] else None} if operation else None}
            return views

    @router.get("/api/artists/check-status")
    def check_status_batch(artist_ids: str = Query(..., min_length=1, max_length=1200)):
        try:
            ids = sorted(set(int(value) for value in artist_ids.split(",")))
            if not 1 <= len(ids) <= 100 or any(value < 1 for value in ids):
                raise ValueError()
        except ValueError as exc:
            raise HTTPException(422, "画师范围无效") from exc
        return {"items": check_views(current(), ids)}

    @router.get("/api/artists/preview")
    def preview(query: str = Query(..., min_length=1, max_length=200)):
        runtime = current()
        try:
            identity = artist_identity(query)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        try:
            values = [runtime.remote.get_artist_detail(identity)] if identity else runtime.remote.search_artist(query.strip())
            return {"items": with_local(runtime, values), "identity": identity is not None}
        except Exception as exc:
            raise HTTPException(503, safe_failure(exc)["message"]) from exc

    @router.get("/api/artists/preview/{pixiv_id}")
    def detail_preview(pixiv_id: str):
        if not re.fullmatch(r"[0-9]{1,32}", pixiv_id):
            raise HTTPException(422, "画师编号无效")
        runtime = current()
        try:
            return with_local(runtime, [runtime.remote.get_artist_detail(pixiv_id)])[0]
        except Exception as exc:
            raise HTTPException(503, safe_failure(exc)["message"]) from exc

    @router.get("/api/artists/{artist_id}/check-status")
    def check_status(artist_id: int):
        runtime = current()
        views = check_views(runtime, [artist_id])
        if artist_id not in views:
            raise HTTPException(404, "画师不存在")
        return views[artist_id]
    return router
