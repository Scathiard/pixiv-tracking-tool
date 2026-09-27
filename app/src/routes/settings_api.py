"""Read-only settings snapshot and explicit user-initiated settings edits."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .. import config
from ..auth import get_pending_login_url
from ..core.models import Artist, Asset, Work


class SettingsEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    automatic_paused: bool | None = None
    port: int | None = Field(default=None, ge=1024, le=65535)


def create_settings_router(get_runtime):
    router = APIRouter()

    def runtime():
        current = get_runtime()
        if current is None:
            raise HTTPException(503, "图库尚未准备好")
        return current

    def snapshot():
        current = runtime()
        with current.database.read() as session:
            artists = session.scalar(select(func.count()).select_from(Artist)) or 0
            works = session.scalar(select(func.count()).select_from(Work)) or 0
            downloaded_pages = session.scalar(select(func.count()).select_from(Asset).where(
                Asset.role == "image", Asset.state == "verified")) or 0
        status = "logged_in" if current.client is not None else (
            "pending" if get_pending_login_url() else "offline")
        # A GET must not contact Pixiv just to retrieve the current account ID.
        account_id = getattr(current, "account_id", None)
        return {"login": {"status": status, "account_id": account_id},
            "automatic_paused": current.queue.automatic_paused(),
            "current_port": config.LISTENING_PORT, "next_port": config.PORT,
            "data_root": str(config.DATA_ROOT),
            "check_interval_minutes": config.CHECK_INTERVAL_HOURS * 60 + (config.CHECK_INTERVAL_MINUTES or 0),
            "stats": {"artists": artists, "works": works, "downloaded_pages": downloaded_pages}}

    @router.get("/api/settings")
    def settings():
        return snapshot()

    @router.patch("/api/settings")
    def edit_settings(edit: SettingsEdit):
        if edit.automatic_paused is None and edit.port is None:
            raise HTTPException(422, "请选择要修改的设置")
        current = runtime()
        if edit.port is not None:
            config.set_port(edit.port)
        if edit.automatic_paused is not None:
            current.queue.set_automatic_paused(edit.automatic_paused)
        return snapshot()

    return router
