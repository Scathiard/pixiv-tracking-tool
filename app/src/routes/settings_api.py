"""Read-only settings snapshot and explicit user-initiated settings edits."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, case, func, or_, select, union
import socket
import ipaddress

from .. import config
from ..auth import get_pending_login_url
from ..core.models import ArchiveIntent, Artist, Asset, RevisionAsset, Work, WorkRevision
from ..core.activity_log import processing_log
from ..version import __version__, __build__
from ..scheduler import interval_snapshot, save_interval


class SettingsEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    automatic_paused: bool | None = None
    port: int | None = Field(default=None, ge=1024, le=65535)
    check_interval_minutes: int | None = Field(default=None, ge=1, le=43200)


def lan_addresses(port):
    # These are addresses of local adapters, not a hostname lookup. Windows
    # DNS retries must not block the complete settings response while offline.
    try:
        import psutil
        stats = psutil.net_if_stats()
        addresses = psutil.net_if_addrs()
    except OSError:
        return []
    hosts = {entry.address for name, entries in addresses.items()
             if name not in stats or stats[name].isup
             for entry in entries if entry.family == socket.AF_INET}
    return [f"http://{host}:{port}" for host in sorted(hosts)
            if ipaddress.ip_address(host).is_private and not ipaddress.ip_address(host).is_loopback
            and not ipaddress.ip_address(host).is_unspecified]


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
            # UNION retains the former IN-membership semantics: a reused asset is
            # counted once even if several default revisions reference its ID.
            # Separate the legacy branch instead of repeating a MULTI-INDEX OR
            # and its whole-library ID list for every individual statistic.
            selected_assets = union(
                select(RevisionAsset.asset_id.label("asset_id")).join(Work, Work.default_revision_id == RevisionAsset.revision_id),
                select(Asset.id.label("asset_id")).join(Work, Work.id == Asset.work_id).where(
                    Work.default_revision_id.is_(None), Asset.revision_id.is_(None)),
            ).subquery()
            registered = func.length(Asset.relative_path) > 0
            usable = and_(registered, ~Asset.state.in_(("missing", "failed", "invalid", "mismatch", "corrupt")))
            def total_when(condition):
                return func.sum(case((condition, 1), else_=0))
            originals = select(
                Asset.work_id.label("work_id"),
                total_when(and_(Asset.role == "image", Asset.state == "verified")).label("downloaded_pages"),
                total_when(registered).label("registered_originals"),
                total_when(Asset.state == "verified").label("verified_originals"),
                total_when(and_(Asset.state == "unknown", registered)).label("unverified_originals"),
                total_when(usable).label("usable"),
            ).join(selected_assets, selected_assets.c.asset_id == Asset.id).join(
                Work, Work.id == Asset.work_id).outerjoin(WorkRevision, WorkRevision.id == Work.default_revision_id).where(
                or_(and_(func.coalesce(WorkRevision.type, Work.type) == "ugoira", Asset.role == "ugoira_zip"),
                    and_(func.coalesce(WorkRevision.type, Work.type) != "ugoira", Asset.role == "image",
                         Asset.page_index < func.coalesce(WorkRevision.page_count, Work.page_count))))
            originals = originals.group_by(Asset.work_id).subquery()
            required = case((func.coalesce(WorkRevision.type, Work.type) == "ugoira", 1), else_=func.coalesce(WorkRevision.page_count, Work.page_count))
            gap = required - func.coalesce(originals.c.usable, 0)
            totals = session.execute(select(
                func.coalesce(func.sum(originals.c.downloaded_pages), 0),
                func.coalesce(func.sum(originals.c.registered_originals), 0),
                func.coalesce(func.sum(originals.c.verified_originals), 0),
                func.coalesce(func.sum(originals.c.unverified_originals), 0),
                func.coalesce(func.sum(case((gap > 0, gap), else_=0)), 0),
            ).select_from(Work).outerjoin(WorkRevision, WorkRevision.id == Work.default_revision_id).outerjoin(
                originals, originals.c.work_id == Work.id).where(
                    ~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed")))).one()
            downloaded_pages, registered, verified, unknown, missing = totals
        status = "logged_in" if current.client is not None else (
            "pending" if get_pending_login_url() else "offline")
        # A GET must not contact Pixiv just to retrieve the current account ID.
        account_id = getattr(current, "account_id", None)
        return {"version": __version__, "build": __build__,
            "login": {"status": status, "account_id": account_id},
            "automatic_paused": current.queue.automatic_paused(),
            "current_port": config.LISTENING_PORT, "next_port": config.PORT,
            "data_root": str(config.DATA_ROOT),
            "check_interval_minutes": interval_snapshot(current.database)["minutes"],
            "next_check_at": interval_snapshot(current.database)["next_check_at"],
            "lan_urls": lan_addresses(config.LISTENING_PORT),
            "stats": {"artists": artists, "works": works, "downloaded_pages": downloaded_pages,
                      "registered_originals": registered, "verified_originals": verified,
                      "missing_originals": missing, "unverified_originals": unknown}}

    @router.get("/api/settings")
    def settings():
        return snapshot()

    @router.get("/api/logs")
    def logs():
        return processing_log(runtime().database)

    @router.patch("/api/settings")
    def edit_settings(edit: SettingsEdit):
        if edit.automatic_paused is None and edit.port is None and edit.check_interval_minutes is None:
            raise HTTPException(422, "请选择要修改的设置")
        current = runtime()
        if edit.port is not None:
            config.set_port(edit.port)
        if edit.automatic_paused is not None:
            current.queue.set_automatic_paused(edit.automatic_paused)
        if edit.check_interval_minutes is not None:
            save_interval(current, edit.check_interval_minutes)
        return snapshot()

    return router
