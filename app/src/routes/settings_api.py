"""Read-only settings snapshot and explicit user-initiated settings edits."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, String, and_, case, func, or_, select, text, union
import socket
import ipaddress
from datetime import datetime, timezone
from threading import Lock
from time import monotonic

from .. import config
from ..auth import get_pending_login_url
from ..core.models import ArchiveIntent, Artist, Asset, RevisionAsset, Work
from ..core.activity_log import processing_log
from ..core.read_control import current_read, local_read
from ..core import diagnostics
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

    def statistics(current):
        with current.database.read() as session:
            with diagnostics.span("read.settings.counts"):
                artists = session.scalar(select(func.count()).select_from(Artist)) or 0
                works = session.scalar(select(func.count()).select_from(Work)) or 0
            # Seek only selected version shapes in existing covering indexes.
            # Explicit index selection avoids both historical shape scans and
            # SQLite choosing wide caption/metadata rows through the unique ID.
            # Keep NULL, dangling-reference and cross-work selection semantics.
            current_works = text("""SELECT w.id AS work_id,w.pixiv_illust_id AS pixiv_id,
                w.default_revision_id AS revision_id,coalesce(r.type,w.type) AS type,
                coalesce(r.page_count,w.page_count) AS page_count
                FROM works w INDEXED BY ix_works_settings_shape
                LEFT JOIN work_revisions r INDEXED BY ix_revisions_settings_shape
                ON r.id=w.default_revision_id""").columns(work_id=Integer, pixiv_id=String,
                    revision_id=String, type=String, page_count=Integer).cte("current_works").prefix_with("MATERIALIZED")
            # UNION retains the former IN-membership semantics: a reused asset is
            # counted once even if several default revisions reference its ID.
            # Separate the legacy branch instead of repeating a MULTI-INDEX OR
            # and its whole-library ID list for every individual statistic.
            selected_assets = union(
                select(RevisionAsset.asset_id.label("asset_id")).join(current_works, current_works.c.revision_id == RevisionAsset.revision_id),
                select(Asset.id.label("asset_id")).join(current_works, current_works.c.work_id == Asset.work_id).where(
                    current_works.c.revision_id.is_(None), Asset.revision_id.is_(None)),
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
                current_works, current_works.c.work_id == Asset.work_id).where(
                or_(and_(current_works.c.type == "ugoira", Asset.role == "ugoira_zip"),
                    and_(current_works.c.type != "ugoira", Asset.role == "image",
                         Asset.page_index < current_works.c.page_count)))
            originals = originals.group_by(Asset.work_id).subquery()
            required = case((current_works.c.type == "ugoira", 1), else_=current_works.c.page_count)
            gap = required - func.coalesce(originals.c.usable, 0)
            statement = select(
                func.coalesce(func.sum(originals.c.downloaded_pages), 0),
                func.coalesce(func.sum(originals.c.registered_originals), 0),
                func.coalesce(func.sum(originals.c.verified_originals), 0),
                func.coalesce(func.sum(originals.c.unverified_originals), 0),
                func.coalesce(func.sum(case((gap > 0, gap), else_=0)), 0),
            ).select_from(current_works).outerjoin(
                originals, originals.c.work_id == current_works.c.work_id).where(
                    ~current_works.c.pixiv_id.in_(select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed")))
            with diagnostics.span("read.settings.totals.execute"):
                result = session.execute(statement)
            with diagnostics.span("read.settings.totals.consume"):
                totals = result.one()
            downloaded_pages, registered, verified, unknown, missing = totals
        return {"artists": artists, "works": works, "downloaded_pages": downloaded_pages,
                "registered_originals": registered, "verified_originals": verified,
                "missing_originals": missing, "unverified_originals": unknown}

    def snapshot():
        current = runtime()
        status = "logged_in" if current.client is not None else (
            "pending" if get_pending_login_url() else "offline")
        # A GET must not contact Pixiv just to retrieve the current account ID.
        account_id = getattr(current, "account_id", None)
        with diagnostics.span("read.settings.interval"):
            interval = interval_snapshot(current.database)
        return {"version": __version__, "build": __build__,
            "login": {"status": status, "account_id": account_id},
            "automatic_paused": current.queue.automatic_paused(),
            "current_port": config.LISTENING_PORT, "next_port": config.PORT,
            "data_root": str(config.DATA_ROOT),
            "check_interval_minutes": interval["minutes"],
            "next_check_at": interval["next_check_at"],
            "lan_urls": lan_addresses(config.LISTENING_PORT)}

    @router.get("/api/settings")
    @local_read(get_runtime, coalesce=True, coalesce_context=lambda current:
        (config.PORT, config.LISTENING_PORT, bool(get_pending_login_url())))
    def settings():
        return snapshot()

    # The optional statistics read is the only owner of this short-lived cache.
    # These are explicitly timestamped statistics, not current task state.
    # Background writes must not defeat the cache while a queue is busy.
    # Cancellation also applies while waiting for the single statistics read.
    stats_lock = Lock()
    stats_cache = None

    @router.get("/api/settings/stats")
    @local_read(get_runtime, lane="logs", coalesce=True)
    def settings_stats(refresh: bool = False):
        nonlocal stats_cache
        current = runtime()
        budget = current_read.get()
        requested_at = budget.started if budget is not None else monotonic()
        while not stats_lock.acquire(timeout=.1):
            if (budget := current_read.get()) is not None:
                budget.checkpoint()
        try:
            if (stats_cache is not None and stats_cache[0] is current.database
                    and monotonic() - stats_cache[1] < 15
                    and (not refresh or stats_cache[1] >= requested_at)):
                return stats_cache[2]
            value = {"stats": statistics(current),
                     "calculated_at": datetime.now(timezone.utc).isoformat()}
            stats_cache = (current.database, monotonic(), value)
            return value
        finally:
            stats_lock.release()

    @router.get("/api/logs")
    @local_read(get_runtime, lane="logs", coalesce=True)
    def logs(cursor: str | None = Query(None, max_length=16000)):
        try:
            return processing_log(runtime().database, cursor=cursor)
        except ValueError as exc:
            raise HTTPException(422, "日志继续读取位置无效，请刷新") from exc

    @router.patch("/api/settings")
    def edit_settings(edit: SettingsEdit):
        if edit.automatic_paused is None and edit.port is None and edit.check_interval_minutes is None:
            raise HTTPException(422, "请选择要修改的设置")
        current = runtime()
        if edit.port is not None:
            config.set_port(edit.port)
            # Port persistence is outside SQLite, but later GETs still must
            # not join a snapshot taken before this successful settings edit.
            current.database.reads.advance_generation()
        if edit.automatic_paused is not None:
            current.queue.set_automatic_paused(edit.automatic_paused)
        if edit.check_interval_minutes is not None:
            save_interval(current, edit.check_interval_minutes)
        return {"ok": True, **snapshot()}

    return router
