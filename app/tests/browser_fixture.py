"""浏览器回归用的本地夹具应用；调用方必须先设置隔离 DATA_ROOT。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src import config, progress
from src.models import Illustration, IllustrationTag, Session, TrackedArtist, init_db
from src.routes import artists, settings, works
from src.security import NetworkPolicy, RequestBoundaryMiddleware
from src.web import templates


def _seed():
    init_db()
    session = Session()
    if session.query(TrackedArtist).count():
        session.close()
        return
    now = datetime.utcnow()
    for artist_index, name in enumerate(("Alpha, Beta", "夜空"), start=1):
        artist = TrackedArtist(
            pixiv_user_id=str(100 + artist_index),
            name=name,
            pixiv_account=f"artist{artist_index}",
            is_active=True,
        )
        session.add(artist)
        session.flush()
        for work_index in range(12):
            illust = Illustration(
                pixiv_illust_id=str(artist_index * 10000 + work_index),
                artist_id=artist.id,
                title=f"{name} 作品 {work_index}",
                type="manga" if work_index % 4 == 0 else "illust",
                page_count=2 if work_index % 5 == 0 else 1,
                tags=json.dumps(["风景", "tag&value"], ensure_ascii=False),
                posted_at=now - timedelta(days=work_index),
                file_paths='["/static/placeholder.svg"]',
                x_restrict=1 if work_index == 3 else 0,
            )
            session.add(illust)
            session.flush()
            for tag in ("风景", "tag&value"):
                session.add(IllustrationTag(illustration_id=illust.id, tag=tag))
    session.commit()
    session.close()


def build_app():
    _seed()
    app = FastAPI()
    policy = NetworkPolicy.from_interfaces([("127.0.0.1", "255.0.0.0")])
    app.add_middleware(RequestBoundaryMiddleware, policy=policy)
    app.mount("/static", StaticFiles(directory=config.resource_path("static")), name="static")
    app.include_router(artists.router)
    app.include_router(works.router)
    app.include_router(settings.router)
    templates.env.globals["pixiv_logged_in"] = True

    @app.get("/api/progress")
    async def api_progress():
        return JSONResponse(progress.get_state())

    return app
