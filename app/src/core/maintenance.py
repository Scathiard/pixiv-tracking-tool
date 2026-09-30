"""Bounded background repair for already recorded archive gaps.

The ledger lives in application_state, so a restart never turns an old HTTP
failure into an immediate burst of new requests. No filesystem inventory is
performed here; ordinary task handlers inspect only the selected works.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import aliased

from .models import ApplicationState, ArchiveIntent, Asset, Derivative, Operation, OperationItem, Work, utcnow
from .tasks import ACTIVE


SCAN_SECONDS = 60
BACKLOG_SECONDS = 10
LOCAL_BATCH = 8
REMOTE_BATCH = 1
LEDGER_PREFIX = "maintenance_work:"
REMOTE_HOLD_KEY = "maintenance_remote_hold"
SCAN_WINDOW = 128
ATTENTION_CODES = frozenset(("work_unavailable", "work_private",
    "ugoira_metadata_unavailable", "work_metadata_conflict"))


def _legacy_attention(track, failure=None, *, work_type=None):
    """Recognize only prior known 404 and metadata-shape evidence."""
    failure = failure if isinstance(failure, dict) else {}
    code = track.get("failure_code") or failure.get("failure_code")
    if code in ATTENTION_CODES:
        return code, track.get("reason") or failure.get("message")
    status = track.get("http_status") or failure.get("status")
    if status == 404 and code in (None, "api_error"):
        if work_type == "ugoira" and track.get("kind") == "repair":
            return "ugoira_metadata_unavailable", "动图时序资料暂时无法获取，已有存档保留"
        return "work_unavailable", "主站暂时无法获取此作品，尚不能确认是否删除；已有存档保留"
    old_conflict = "远端作品的归属、类型或页数发生变化，已有存档保留待核对"
    if (code in (None, "InvalidMedia") and
            (track.get("reason") == old_conflict or failure.get("message") == old_conflict)):
        return "work_metadata_conflict", old_conflict
    return None, None


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _project(ledger):
    tracks = ledger.get("tracks", {})
    chosen = next((tracks.get(name) for name in ("remote", "local")
                   if tracks.get(name, {}).get("status") == "queued"), None)
    if chosen is None:
        chosen = next((tracks.get(name) for name in ("remote", "local")
                       if tracks.get(name, {}).get("status") == "waiting"), None)
    if chosen is None:
        chosen = next((tracks.get(name) for name in ("remote", "local")
                       if tracks.get(name, {}).get("status") == "attention"), None)
    if chosen is None:
        chosen = next((tracks.get(name) for name in ("remote", "local") if tracks.get(name)), {})
    return {**ledger, "status": chosen.get("status", "ready"), "kind": chosen.get("kind"),
            "reason": chosen.get("reason"), "next_retry_at": chosen.get("next_retry_at"),
            "operation_id": chosen.get("operation_id"), "last_attempt_at": chosen.get("last_attempt_at"),
            "attempts": chosen.get("attempts", 0), "failure_code": chosen.get("failure_code"),
            "http_status": chosen.get("http_status")}


class MissingMediaMaintenance:
    def __init__(self, database, queue, *, remote_available, now=utcnow, monotonic=time.monotonic):
        self.database, self.queue = database, queue
        self.remote_available, self.now, self.monotonic = remote_available, now, monotonic
        self._next_scan = 0.0
        self._next_remote = 0.0
        self._cursors = {"remote": 0, "incomplete": 0, "source": 0, "image": 0, "gif": 0, "pair": 0}

    def _reconcile(self, now):
        with self.database.write() as session:
            pending = list(session.scalars(select(ApplicationState).where(
                ApplicationState.key.like(LEDGER_PREFIX + "%"),
                ApplicationState.value["status"].as_string().in_(("queued", "waiting")))))
            for setting in pending:
                ledger = dict(setting.value) if isinstance(setting.value, dict) else {}
                tracks = {name: dict(value) for name, value in ledger.get("tracks", {}).items()}
                if not tracks and ledger.get("kind"):
                    name = "local" if ledger["kind"] == "thumbnails" else "remote"
                    tracks[name] = {key: ledger.get(key) for key in (
                        "status", "kind", "operation_id", "attempts", "last_attempt_at",
                        "next_retry_at", "reason", "failure_code", "http_status")}
                changed = False
                for name, track in tracks.items():
                    if name == "remote" and track.get("status") == "waiting":
                        prior = None
                        if track.get("operation_id"):
                            prior = session.scalar(select(OperationItem.result).where(
                                OperationItem.operation_id == track["operation_id"],
                                OperationItem.state == "failed").order_by(OperationItem.position).limit(1))
                        try:
                            work_id = int(setting.key.removeprefix(LEDGER_PREFIX))
                        except ValueError:
                            work_id = None
                        work = session.get(Work, work_id) if work_id is not None else None
                        code, reason = _legacy_attention(track, prior,
                            work_type=work.type if work else None)
                        if code:
                            track.update(status="attention", next_retry_at=None,
                                         failure_code=code, reason=reason)
                            changed = True
                            continue
                    if track.get("status") != "queued" or not track.get("operation_id"):
                        continue
                    operation = session.get(Operation, track["operation_id"])
                    if operation is None or operation.state != "finished":
                        continue
                    failure = session.scalar(select(OperationItem.result).where(
                        OperationItem.operation_id == operation.id,
                        OperationItem.state.in_(("failed", "not_executed"))).order_by(OperationItem.position).limit(1))
                    failed = operation.result.get("outcome") in ("failed", "partial_failure")
                    attempts = int(track.get("attempts", 0))
                    if failed:
                        failure = failure if isinstance(failure, dict) else {}
                        status = failure.get("status")
                        retry_after = failure.get("retry_after")
                        rate_limited = status == 429 or failure.get("failure_code") in (
                            "api_rate_limit", "image_rate_limit")
                        rejected = status in (401, 403) or failure.get("retryable") is False
                        if name == "remote" and failure.get("failure_code") in ATTENTION_CODES:
                            track.update(status="attention", next_retry_at=None,
                                reason=failure.get("message") or "来源资料暂时无法核对，已有存档保留",
                                failure_code=failure.get("failure_code"), http_status=status)
                            changed = True
                            continue
                        if rejected or rate_limited:
                            delay = (900, 3600, 21600, 86400)[min(attempts - 1, 3)]
                        else:
                            delay = min(86400, 300 * 2 ** min(max(0, attempts - 1), 8))
                        if type(retry_after) in (int, float):
                            delay = max(delay, retry_after)
                        next_retry = now + timedelta(seconds=delay)
                        track.update(status="waiting", next_retry_at=next_retry.isoformat(),
                                     reason=failure.get("message") or "补齐未完成，稍后重试",
                                     failure_code=failure.get("failure_code"), http_status=status)
                        if name == "remote" and (status in (401, 403, 429) or rate_limited):
                            hold = session.get(ApplicationState, REMOTE_HOLD_KEY)
                            # Pause other remote requests briefly; each work's
                            # own retry time remains independent and persisted.
                            global_until = now + timedelta(seconds=max(900, retry_after or 0))
                            value = {"until": global_until.isoformat(), "reason": track["reason"], "status": status}
                            if hold is None:
                                session.add(ApplicationState(key=REMOTE_HOLD_KEY, value=value))
                            elif (_parse_time(hold.value.get("until")) or now) < global_until:
                                hold.value = value
                    else:
                        track.update(status="ready", next_retry_at=(now + timedelta(minutes=5)).isoformat(),
                                     reason=None, failure_code=None, http_status=None)
                    changed = True
                if changed:
                    setting.value = _project({**ledger, "tracks": tracks})

    def _candidates(self):
        with self.database.read() as session:
            eligible = and_(ArchiveIntent.mode == "keep", ArchiveIntent.pixiv_illust_id == Work.pixiv_illust_id)
            windows = {}
            def page(statement, name):
                cursor = self._cursors[name]
                rows = list(session.execute(statement.where(Work.id < cursor).order_by(Work.id.desc()).limit(SCAN_WINDOW))) if cursor else []
                if not rows:
                    rows = list(session.execute(statement.order_by(Work.id.desc()).limit(SCAN_WINDOW)))
                windows[name] = [row.id for row in rows]
                return rows

            missing_assets = page(select(Work.id, Work.type, Work.source_metadata).join(
                Asset, Asset.work_id == Work.id).join(ArchiveIntent, eligible).where(
                    Asset.state.in_(("missing", "failed", "invalid", "mismatch", "source_changed"))).distinct(), "remote")
            # Imported catalogues may omit required page rows entirely.
            asset_count = select(func.count()).select_from(Asset).where(Asset.work_id == Work.id).correlate(Work).scalar_subquery()
            incomplete = page(select(Work.id, Work.type, Work.source_metadata).join(ArchiveIntent, eligible).where(
                asset_count < case((Work.type == "ugoira", 2), else_=Work.page_count)), "incomplete")
            pending_sources = page(select(Work.id, Work.type, Work.source_metadata).join(ArchiveIntent, eligible).where(
                func.json_extract(Work.source_metadata, "$.source_updates").is_not(None),
                func.json_extract(Work.source_metadata, "$.source_updates") != "{}"), "source")
            missing_assets = list({row.id: row for row in missing_assets + incomplete + pending_sources}.values())
            remote = [row.id for row in missing_assets if not (
                row.type == "ugoira" and (row.source_metadata or {}).get("original_backfill_requires_selection"))]

            thumbnail = aliased(Derivative)
            first = aliased(Asset)
            image_ready = [row.id for row in page(select(Work.id).join(ArchiveIntent, eligible).join(
                first, and_(first.work_id == Work.id, first.role == "image", first.page_index == 0,
                            first.state.in_(("verified", "unknown")), first.relative_path.is_not(None))).outerjoin(
                thumbnail, and_(thumbnail.work_id == Work.id, thumbnail.kind == "thumbnail",
                                thumbnail.page_index == 0, thumbnail.spec_key == "v1")).where(
                or_(thumbnail.id.is_(None), thumbnail.state.in_(
                    ("missing", "failed", "invalid", "outdated")))), "image")]
            preview = aliased(Derivative)
            gif_ready = [row.id for row in page(select(Work.id).join(ArchiveIntent, eligible).join(
                preview, and_(preview.work_id == Work.id, preview.kind == "gif", preview.spec_key == "v1",
                              preview.state.in_(("verified", "unknown")), preview.relative_path.is_not(None))).outerjoin(
                thumbnail, and_(thumbnail.work_id == Work.id, thumbnail.kind == "thumbnail",
                                thumbnail.page_index == 0, thumbnail.spec_key == "v1")).where(
                or_(thumbnail.id.is_(None), thumbnail.state.in_(
                    ("missing", "failed", "invalid", "outdated")))), "gif")]
            # A verified ugoira pair can make its preview locally; repair then
            # keeps the existing pair -> GIF -> thumbnail dependency chain.
            zip_asset, timing_asset = aliased(Asset), aliased(Asset)
            gif = aliased(Derivative)
            gif_from_pair = [row.id for row in page(select(Work.id).join(ArchiveIntent, eligible).join(
                zip_asset, and_(zip_asset.work_id == Work.id, zip_asset.role == "ugoira_zip",
                                zip_asset.state == "verified")).join(
                timing_asset, and_(timing_asset.work_id == Work.id, timing_asset.role == "ugoira_timing",
                                   timing_asset.state == "verified")).outerjoin(
                gif, and_(gif.work_id == Work.id, gif.kind == "gif", gif.spec_key == "v1")).where(
                Work.type == "ugoira", or_(gif.id.is_(None), gif.state.in_(
                    ("missing", "failed", "invalid", "outdated")))), "pair")]
            local = sorted(set(image_ready + gif_ready + gif_from_pair), reverse=True)
            candidate_ids = set(remote) | set(local)
            ledgers = {row.key: row.value for row in session.scalars(select(ApplicationState).where(
                ApplicationState.key.in_([LEDGER_PREFIX + str(work_id) for work_id in candidate_ids])))}
            hold = session.get(ApplicationState, REMOTE_HOLD_KEY)
            return remote, local, set(gif_from_pair), ledgers, hold.value if hold else {}, windows

    def tick(self, *, force=False):
        clock = self.monotonic()
        if not force and clock < self._next_scan:
            return []
        self._next_scan = clock + SCAN_SECONDS
        now = self.now()
        self._reconcile(now)
        remote, local, gif_from_pair, ledgers, hold, windows = self._candidates()
        for work_id in set(remote) | set(local):
            key = LEDGER_PREFIX + str(work_id)
            ledger = ledgers.get(key)
            if isinstance(ledger, dict):
                tracks = {name: dict(value) for name, value in ledger.get("tracks", {}).items()}
                changed = False
                for name in (("remote",) if work_id in remote else ()) + (("local",) if work_id in local else ()):
                    track = tracks.get(name)
                    if track and track.get("status") == "ready":
                        track.update(status="waiting", reason="仍有缺项，稍后继续补齐")
                        changed = True
                if not changed:
                    continue
                ledger = _project({**ledger, "tracks": tracks})
                with self.database.write() as session:
                    session.get(ApplicationState, key).value = ledger
                ledgers[key] = ledger
        if not self.queue._accepting:
            return []
        with self.database.read() as session:
            if session.scalar(select(Operation.id).where(Operation.source == "maintenance",
                                                         Operation.state.in_(ACTIVE)).limit(1)):
                self._next_scan = clock + BACKLOG_SECONDS
                return []
        submitted = []
        selected = set()

        def due(work_id, name):
            ledger = ledgers.get(LEDGER_PREFIX + str(work_id)) or {}
            tracks = ledger.get("tracks", {})
            if any(track.get("status") == "queued" for track in tracks.values()):
                return False
            track = tracks.get(name, {})
            if track.get("status") == "attention":
                return False
            retry_at = _parse_time(track.get("next_retry_at"))
            return retry_at is None or retry_at <= now

        remote_open = (clock >= self._next_remote and self.remote_available()
                       and (_parse_time(hold.get("until")) or now) <= now)
        if remote_open:
            for work_id in remote:
                if due(work_id, "remote"):
                    submitted.append((work_id, "repair", "remote"))
                    selected.add(work_id)
                    self._next_remote = clock + SCAN_SECONDS
                    break
        local_count = 0
        for work_id in local:
            if local_count >= LOCAL_BATCH:
                break
            if work_id not in selected and due(work_id, "local"):
                submitted.append((work_id, "repair" if work_id in gif_from_pair else "thumbnails", "local"))
                selected.add(work_id)
                local_count += 1
        if len(local) > local_count:
            self._next_scan = clock + BACKLOG_SECONDS

        # Advance only past submitted entries or a window with no due entry.
        # Busy work and remote cooldown leave the windows untouched.
        selected_remote = [work_id for work_id, _, name in submitted if name == "remote"]
        selected_local = [work_id for work_id, _, name in submitted if name == "local"]
        if remote_open:
            for name in ("remote", "incomplete", "source"):
                window = windows[name]
                chosen = [work_id for work_id in selected_remote if work_id in window]
                if chosen:
                    self._cursors[name] = min(chosen)
                elif window and not any(due(work_id, "remote") for work_id in window):
                    self._cursors[name] = min(window)
        for name in ("image", "gif", "pair"):
            window = windows[name]
            chosen = [work_id for work_id in selected_local if work_id in window]
            if chosen:
                self._cursors[name] = min(chosen)
            elif window and not any(due(work_id, "local") for work_id in window):
                self._cursors[name] = min(window)

        results = []
        for work_id, kind, name in submitted:
            result = self.queue.submit(kind, {"work_ids": [work_id]}, "自动补齐存档", source="maintenance")
            with self.database.write() as session:
                key = LEDGER_PREFIX + str(work_id)
                setting = session.get(ApplicationState, key)
                old = dict(setting.value) if setting and isinstance(setting.value, dict) else {}
                tracks = {name: dict(value) for name, value in old.get("tracks", {}).items()}
                track = tracks.get(name, {})
                tracks[name] = {**track, "status": "queued", "kind": kind, "operation_id": result["operation_id"],
                                "attempts": int(track.get("attempts", 0)) + 1,
                                "last_attempt_at": now.isoformat(), "next_retry_at": None}
                value = _project({**old, "tracks": tracks})
                if setting is None:
                    session.add(ApplicationState(key=key, value=value))
                else:
                    setting.value = value
            results.append(result)
        return results
