"""Archive task handlers: discover explicit assets, then execute the frozen manifest."""
from __future__ import annotations

from .read_control import LocalReadError

import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .catalogue_revision import bump_catalogue_revision
from .download import AssetDownloader
from .media import CARD_MOTION_SPEC, InvalidMedia, Publisher, SuppressedWork, build_card_motion, build_gif, build_thumbnail, contained_path, verify_media, verify_ugoira_pair
from .models import (
    ApplicationState, ArchiveIntent, Artist, Asset, Derivative, LocalPreference, Operation, RemoteBookmark,
    TrackingPolicy, Work, WorkTag, WorkRevision, RevisionAsset, RevisionDerivative, utcnow,
)
from .tasks import ItemResult, ItemSpec, safe_failure
from .revisions import (latest_revision, ensure_legacy_revision, observe_revision, revision_assets, revision_derivatives, bind_derivative, bind_asset, revision_directory, refresh_for_record, refresh_revision, project_revision, asset_entries)
from .tag_translations import merge_tag_translations, stored_translations


class WorkMetadataConflict(InvalidMedia):
    failure_code = "work_metadata_conflict"
    retryable = False


class ArchiveHandler:
    def __init__(self, kind, database, publisher: Publisher, client, downloader=None, *, bookmarks=None, profiles=None, source_status=None):
        self.kind, self.database, self.publisher, self.client = kind, database, publisher, client
        self.downloader = downloader or AssetDownloader()
        self.bookmarks, self.profiles = bookmarks, profiles
        self.source_status = source_status

    def _assets_statement(self, session, work_id, revision_id=None):
        revision = session.get(WorkRevision, revision_id) if revision_id else latest_revision(session, work_id)
        if revision:
            return select(Asset).join(RevisionAsset, RevisionAsset.asset_id == Asset.id).where(RevisionAsset.revision_id == revision.id)
        return select(Asset).where(Asset.work_id == work_id)

    def _derivatives_statement(self, session, work_id, revision_id=None):
        revision = session.get(WorkRevision, revision_id) if revision_id else latest_revision(session, work_id)
        if revision:
            return select(Derivative).join(RevisionDerivative, RevisionDerivative.derivative_id == Derivative.id).where(RevisionDerivative.revision_id == revision.id)
        return select(Derivative).where(Derivative.work_id == work_id)

    def _bookmark_account(self):
        if self.bookmarks is None:
            return None
        try:
            return self.client.get_account_id()
        except Exception:
            # An unknown account must not turn a metadata field into a fact.
            return None

    def _bookmark_scope(self, artist_id):
        if self.bookmarks is None:
            return {}, None
        account = self._bookmark_account()
        if account is None:
            return {}, None
        with self.database.read() as session:
            works = list(session.execute(select(Work.id, Work.pixiv_illust_id).where(
                Work.artist_id == artist_id, ~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(
                    ArchiveIntent.mode == "suppressed")))))
        versions = self.bookmarks.begin_observations([row.id for row in works])
        return {row.pixiv_illust_id: versions.get(row.id) for row in works}, account

    def _source_scope(self, artist_id, account):
        """Reserve only anomalous existing source records before listing I/O."""
        if self.source_status is None:
            return {}
        with self.database.read() as session:
            records = list(session.execute(select(Work.id, Work.pixiv_illust_id, ArchiveIntent.revision).join(
                ArchiveIntent, ArchiveIntent.pixiv_illust_id == Work.pixiv_illust_id).where(
                Work.artist_id == artist_id, ArchiveIntent.mode == "keep",
                Work.source_metadata["remote_availability"]["state"].as_string().in_(("unavailable", "private", "deleted")))))
        versions = self.source_status.begin_many("work", [row.id for row in records], account)
        return {row.pixiv_illust_id: {"work_id": row.id, "revision": row.revision,
            "version": versions.get(row.id)} for row in records}

    def _apply_bookmark(self, work_id, data, version, account):
        if work_id is not None and account is not None and self._bookmark_account() == account:
            return self.bookmarks.apply_observation(work_id, data.get("is_bookmarked"), account, version)
        return False

    def _begin_work_observation(self, work_id, account):
        """One short reservation transaction before remote work starts."""
        account_context = self.bookmarks._account_context() if self.bookmarks else None
        with self.database.write() as session:
            bookmark_version = (self.bookmarks._begin_observations(session, [work_id]).get(work_id)
                if account is not None else None)
            source_version = self.source_status._begin(session, "work", work_id, account) if self.source_status else None
            work = session.get(Work, work_id)
            intent = session.get(ArchiveIntent, work.pixiv_illust_id) if work else None
            revision = intent.revision if intent else None
        return bookmark_version, source_version, revision, account_context

    def _save_work_observation(self, artist_id, data, *, work_id, revision, bookmark_version,
            source_version, account, account_context, metadata=None, compare_content=False,
            source_account=..., context=None, metadata_error=None):
        # Credential resolution may wait or contact Pixiv: finish it before the
        # writer. Inside the transaction only the existing memory context is read.
        if self._bookmark_account() != account:
            return None
        if source_account is ...:
            source_account = account
        source_current = (not self.source_status or self.source_status.remote is None
            or self.source_status.account_context() == source_account)
        fresh = False
        with self.database.write() as session:
            if self.bookmarks and self.bookmarks._account_context() != account_context:
                return None
            result = self._save_remote_work_in_session(session, artist_id, data, metadata=metadata,
                context=context, metadata_error=metadata_error,
                expected_work_id=work_id, expected_revision=revision,
                expected_source_version=source_version, compare_content=compare_content, account_id=account)
            if result is None:
                return None
            if self.source_status:
                if source_current and source_version is not None:
                    self.source_status._apply_available(session, "work", result, data, source_version, source_account)
            else:
                self._record_availability_in_session(session, result, "available", None, account)
            if self.bookmarks and account is not None:
                fresh = self.bookmarks._apply_observation(session, result,
                    data.get("is_bookmarked"), account, bookmark_version)
        # Never publish a fact from a rolled-back transaction or another account
        # epoch. A newer bookmark version remains protected by _mark_fresh.
        if fresh:
            self.bookmarks._mark_fresh(result, bookmark_version, account,
                expected_context=account_context)
        return result

    def _refresh_work(self, artist_id, remote_id, work_id):
        account = self._bookmark_account()
        version, source_version, revision, account_context = self._begin_work_observation(work_id, account)
        data = None
        try:
            data = self.client.get_illust_detail(remote_id)
            if str(data.get("illust_id", "")) != str(remote_id):
                from .source_status import SourceIdentityMismatch
                raise SourceIdentityMismatch()
            result = self._save_work_observation(artist_id, data, work_id=work_id,
                revision=revision, source_version=source_version, bookmark_version=version,
                account=account, account_context=account_context)
        except Exception as exc:
            if isinstance(exc, LocalReadError):
                raise
            # A valid source can disagree with the retained local page count.
            # Keep that positive fact without replacing any archived pages.
            if (isinstance(exc, WorkMetadataConflict) and isinstance(data, dict)
                    and str(data.get("illust_id", "")) == str(remote_id)):
                if self.source_status:
                    if self._bookmark_account() == account:
                        self.source_status.apply_available("work", work_id, data, source_version, account)
                else:
                    self._record_availability(work_id, "available", None)
            elif self.source_status:
                if self._bookmark_account() == account:
                    self.source_status.apply_failure("work", work_id, exc, source_version, account)
            elif getattr(exc, "failure_code", None) in ("work_unavailable", "work_private"):
                state = "private" if exc.failure_code == "work_private" else "unavailable"
                reason = ("主站提示作品不可公开获取，已有存档保留" if state == "private" else
                          "主站暂时无法获取此作品，尚不能确认是否删除；已有存档保留")
                self._record_availability(work_id, state, reason)
            raise
        return result

    def _record_availability(self, work_id, state, reason):
        if work_id is None:
            return
        account = self._bookmark_account()
        with self.database.write() as session:
            self._record_availability_in_session(session, work_id, state, reason, account)

    def _record_availability_in_session(self, session, work_id, state, reason, account):
        work = session.get(Work, work_id)
        if work is not None:
            prior = (work.source_metadata or {}).get("remote_availability", {})
            changed = (prior.get("state"), prior.get("reason")) != (state, reason)
            work.source_metadata = {**(work.source_metadata or {}), "remote_availability": {
                "state": state, "checked_at": utcnow().replace(tzinfo=timezone.utc).isoformat(),
                "reason": reason, "confirmed": True, "policy": "source-v2",
                "account_id": account}}
            if changed:
                bump_catalogue_revision(session)

    def _clear_remote_attention_if_complete(self, work_id):
        with self.database.write() as session:
            missing = session.scalar(select(Asset.id).where(Asset.work_id == work_id,
                Asset.state.in_(("missing", "failed", "invalid", "mismatch", "source_changed"))).limit(1))
            setting = session.get(ApplicationState, f"maintenance_work:{work_id}")
            if missing or setting is None or not isinstance(setting.value, dict):
                return
            ledger = dict(setting.value)
            tracks = {name: dict(value) for name, value in ledger.get("tracks", {}).items()}
            remote = tracks.get("remote")
            if (remote and remote.get("status") in ("attention", "waiting") and
                    remote.get("failure_code") != "work_metadata_conflict"):
                remote.update(status="ready", next_retry_at=None, reason=None,
                              failure_code=None, http_status=None)
                from .maintenance import _project
                setting.value = _project({**ledger, "tracks": tracks})

    def _resume_ugoira_attention(self, work_id):
        with self.database.write() as session:
            setting = session.get(ApplicationState, f"maintenance_work:{work_id}")
            if setting is None or not isinstance(setting.value, dict):
                return
            ledger = dict(setting.value)
            tracks = {name: dict(value) for name, value in ledger.get("tracks", {}).items()}
            remote = tracks.get("remote")
            if (remote and remote.get("status") == "attention" and
                    remote.get("failure_code") == "ugoira_metadata_unavailable"):
                remote.update(status="waiting", next_retry_at=None, reason="动图资料已恢复，等待继续补齐",
                              failure_code=None, http_status=None)
                from .maintenance import _project
                setting.value = _project({**ledger, "tracks": tracks})

    def _artist_ids(self, parameters, context):
        if "started_artist_ids" in parameters:
            return parameters["started_artist_ids"]
        if "artist_ids" in parameters:
            result = sorted(set(parameters["artist_ids"]))
        else:
            with self.database.read() as session:
                from .models import Operation
                operation = session.get(Operation, context.operation_id)
                modes = (("active",) if operation.source == "automatic" else
                         ("active", "paused", "stopped") if self.kind == "bookmarks" else ("active", "paused"))
                result = list(session.scalars(select(Artist.id).join(TrackingPolicy).where(
                    TrackingPolicy.mode.in_(modes)).order_by(Artist.id)))
        context.checkpoint_parameters({**parameters, "started_artist_ids": result})
        return result

    def _save_remote_work(self, artist_id, data, *, context=None, metadata=None, metadata_error=None, expected_work_id=None, expected_revision=None, expected_source_version=None, compare_content=False):
        account_id = self._bookmark_account()
        with self.database.write() as session:
            return self._save_remote_work_in_session(session, artist_id, data, context=context,
                metadata=metadata, metadata_error=metadata_error, expected_work_id=expected_work_id,
                expected_revision=expected_revision, expected_source_version=expected_source_version,
                compare_content=compare_content, account_id=account_id)

    def _save_remote_work_in_session(self, session, artist_id, data, *, context=None, metadata=None, metadata_error=None, expected_work_id=None, expected_revision=None, expected_source_version=None, compare_content=False, account_id=None):
        remote_id = str(data["illust_id"])
        if not remote_id.isdigit() or data.get("type") not in ("illust", "manga", "ugoira"):
            raise ValueError("无效作品信息")
        count = int(data["page_count"])
        if count <= 0:
            raise ValueError("无效页数")
        intent = session.get(ArchiveIntent, remote_id)
        if intent and intent.mode == "suppressed":
            return None
        if intent is None:
            intent = ArchiveIntent(pixiv_illust_id=remote_id)
            session.add(intent)
        work = session.scalar(select(Work).where(Work.pixiv_illust_id == remote_id))
        if expected_work_id is not None and (work is None or work.id != expected_work_id or
                intent.mode != "keep" or intent.revision != expected_revision):
            return None
        if expected_source_version is not None and (work is None or
                (work.source_metadata or {}).get("remote_availability", {}).get("version") != expected_source_version):
            return None
        if work and work.artist_id != artist_id:
            raise WorkMetadataConflict("主站画师身份与本地作品不一致；已有存档保留")
        if work is None:
            posted = datetime.fromisoformat(data["posted_at"]) if data.get("posted_at") else None
            work = Work(pixiv_illust_id=remote_id, artist_id=artist_id, title=data["title"],
                type=data["type"], page_count=count, caption="", caption_state="unknown",
                posted_at=posted.astimezone(timezone.utc).replace(tzinfo=None) if posted and posted.tzinfo else posted,
                time_basis="utc" if posted and posted.tzinfo else "unknown", source_metadata={})
            work.preference, work.bookmark = LocalPreference(), RemoteBookmark()
            session.add(work); session.flush()
        else:
            ensure_legacy_revision(session, work)
        availability = (work.source_metadata or {}).get("remote_availability")
        if not self.source_status or availability is None:
            availability = {"state": "available", "confirmed": True,
                "checked_at": utcnow().replace(tzinfo=timezone.utc).isoformat(), "reason": None,
                "policy": "source-v2", "account_id": account_id}
        source = {**(work.source_metadata or {}), "urls": data.get("urls", []), "remote_availability": availability}
        source = merge_tag_translations(source, data.get("tag_details"),
            checked_at=utcnow().replace(tzinfo=timezone.utc).isoformat())
        # Pending updates belonged to mutable 0002 records. Revision references
        # now preserve their originals; new records carry their own source.
        pending_source_change = bool(source.pop("source_updates", None))
        if metadata is not None and data["type"] == "ugoira":
            source["ugoira_source"] = {"zip_url": metadata.get("zip_urls", {}).get("original") or metadata.get("zip_urls", {}).get("medium"),
                "timing_url": f"https://app-api.pixiv.net/v1/ugoira/metadata?illust_id={remote_id}",
                "frames": metadata["frames"]}
        previous = latest_revision(session, work.id)
        caption = data.get("caption") if data.get("caption") is not None else previous.caption if previous else work.caption
        snapshot = dict(title=data["title"], type=data["type"], page_count=count,
            caption=caption, caption_state=("available" if caption else "empty") if data.get("caption") is not None else previous.caption_state if previous else work.caption_state,
            tags=sorted({tag for tag in data.get("tags", previous.tags if previous else []) if isinstance(tag, str)}), source_metadata=source)
        revision, changed = observe_revision(session, work, snapshot,
            compare_content=compare_content or pending_source_change)
        # Work is a current-source search projection, never the reader's media snapshot.
        work.title, work.type, work.page_count = snapshot["title"], snapshot["type"], count
        work.caption, work.caption_state = snapshot["caption"], snapshot["caption_state"]
        # An unchanged immutable revision can carry an older availability
        # snapshot. Its projection must retain this observation's reservation.
        work.source_metadata = {**revision.source_metadata, "remote_availability": availability}
        existing_tags = {tag.tag: tag for tag in work.tags}
        work.tags = [existing_tags[tag] if tag in existing_tags else WorkTag(tag=tag) for tag in snapshot["tags"]]
        work.bookmark_count, work.view_count = data.get("bookmark_count"), data.get("view_count")
        if data.get("x_restrict") is not None:
            work.x_restrict, work.restriction_verified = data["x_restrict"], True
        if work.bookmark is None:
            work.bookmark = RemoteBookmark()
        session.flush()
        refresh_revision(session, revision)
        if changed:
            bump_catalogue_revision(session)
        if context is not None:
            specs = self._discovery_specs(session, work, metadata)
            if metadata_error:
                specs.append(ItemSpec(f"remote-error-{work.id}", "error", {**metadata_error, "work_id": work.id}, "downloading", "assets"))
            if specs:
                parent = session.get(Operation, context.operation_id)
                requests = [{"asset_id": spec.target["asset_id"], "requested_url": spec.target.get("requested_url"),
                    "expected_fingerprint": spec.target.get("expected_fingerprint"), "revision": spec.target.get("revision"),
                    "work_revision_id": revision.id} for spec in specs if spec.target.get("asset_id")]
                context.queue.enqueue_in_transaction(session, "repair", {"work_ids": [work.id], "asset_requests": requests},
                    f"保存 {work.title}", parent.source, parent_id=context.operation_id, specs=specs)
        return work.id

    @staticmethod
    def _timing_fingerprint(metadata):
        return hashlib.sha256(json.dumps({"frames": metadata["frames"]}, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _remember_timing_source(self, session, work, metadata):
        previous = latest_revision(session, work.id)
        if previous is None:
            previous = ensure_legacy_revision(session, work)
        source = {**(previous.source_metadata or {}), "ugoira_source": {
            "zip_url": metadata.get("zip_urls", {}).get("original") or metadata.get("zip_urls", {}).get("medium"),
            "timing_url": f"https://app-api.pixiv.net/v1/ugoira/metadata?illust_id={work.pixiv_illust_id}",
            "frames": metadata["frames"]}}
        snapshot = {key: getattr(previous, key) for key in ("title", "caption", "caption_state", "type", "page_count", "tags")}
        pending_change = bool(source.pop("source_updates", None))
        revision, _ = observe_revision(session, work, {**snapshot, "source_metadata": source}, compare_content=pending_change)
        work.source_metadata = {**(work.source_metadata or {}), "ugoira_source": source["ugoira_source"], "source_updates": {}}
        for asset in revision_assets(session, revision):
            if not asset.source_url:
                asset.source_url = source["ugoira_source"]["zip_url"] if asset.role == "ugoira_zip" else source["ugoira_source"]["timing_url"]

    def _discovery_specs(self, session, work, metadata=None):
        """Pure catalogue planning: never stat or decode established originals."""
        deferred = bool((work.source_metadata or {}).get("original_backfill_requires_selection"))
        intent = session.get(ArchiveIntent, work.pixiv_illust_id)
        assets = list(session.scalars(self._assets_statement(session, work.id).order_by(Asset.page_index)))
        if deferred:
            # Discovery may fill local caches without authorizing old ZIP/timing.
            assets = []
        work_revision = latest_revision(session, work.id)
        work = project_revision(work, work_revision)
        deferred = bool((work.source_metadata or {}).get("original_backfill_requires_selection"))
        if deferred:
            assets = []
        directory = revision_directory(work, work_revision, work.artist.directory or f"artist_{work.artist.pixiv_user_id}")
        updates = (work.source_metadata or {}).get("source_updates", {})
        specs, required = [], []
        for asset in assets:
            if asset.state not in ("missing", "failed", "invalid", "mismatch", "source_changed") and asset.id not in updates:
                continue
            target = {"work_id": work.id, "asset_id": asset.id, "revision": intent.revision, "work_revision_id": work_revision.id if work_revision else None}
            if asset.role == "ugoira_timing":
                if not metadata:
                    continue
                target.update(frames=metadata["frames"], requested_url=asset.source_url, expected_fingerprint=self._timing_fingerprint(metadata), path=asset.relative_path or f"{directory}/{work.pixiv_illust_id}.frames.json")
                kind = "timing"
            else:
                url = (metadata.get("zip_urls", {}).get("original") or metadata.get("zip_urls", {}).get("medium")) if asset.role == "ugoira_zip" and metadata else asset.source_url
                if not url:
                    continue
                extension = ".zip" if asset.role == "ugoira_zip" else PurePosixPath(urlsplit(url).path).suffix.lower()
                if extension not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip"):
                    raise InvalidMedia("原件的文件类型无法识别")
                target.update(url=url, requested_url=url, path=asset.relative_path or f"{directory}/{work.pixiv_illust_id}_p{asset.page_index}{extension}")
                kind = "download"
            key = f"asset-{asset.id}"
            required.append(key)
            specs.append(ItemSpec(key, kind, target, "downloading", "assets"))
        if work.type == "ugoira":
            preview = session.scalar(self._derivatives_statement(session, work.id).where( Derivative.kind == "gif", Derivative.spec_key == "v1"))
            gif_ready = bool(preview and preview.relative_path and preview.state in ("verified", "unknown"))
            pair_ready = len(assets) >= 2 and all(asset.state == "verified" for asset in assets)
            if not required and not pair_ready and not gif_ready:
                return specs
            if required:
                key = f"pair-{work.id}"
                specs.append(ItemSpec(key, "ugoira_pair", {"work_id": work.id, "revision": intent.revision, "work_revision_id": work_revision.id if work_revision else None}, "verifying", "works", tuple(required)))
                required = [key]
            if required or not gif_ready:
                if preview is None:
                    preview = Derivative(work_id=work.id, revision_id=work_revision.id if work_revision else None, kind="gif")
                    session.add(preview); session.flush()
                    if work_revision: bind_derivative(session, work_revision, preview)
                key = f"preview-{preview.id}"
                specs.append(ItemSpec(key, "gif", {"work_id": work.id, "record_id": preview.id, "path": preview.relative_path or f"{directory}/{work.pixiv_illust_id}.gif", "revision": intent.revision, "work_revision_id": work_revision.id if work_revision else None, "force": bool(required)}, "converting", "previews", tuple(required)))
                required = [key]
        else:
            first = next((asset for asset in assets if asset.page_index == 0), None)
            required = [key for key in required if first and key == f"asset-{first.id}"]
            if not required and (first is None or first.state != "verified"):
                return specs
        thumbnail = session.scalar(self._derivatives_statement(session, work.id).where( Derivative.kind == "thumbnail", Derivative.page_index == 0, Derivative.spec_key == "v1"))
        if required or thumbnail is None or thumbnail.state not in ("verified", "unknown"):
            if thumbnail is None:
                thumbnail = Derivative(work_id=work.id, revision_id=work_revision.id if work_revision else None, kind="thumbnail")
                session.add(thumbnail); session.flush()
                if work_revision: bind_derivative(session, work_revision, thumbnail)
            specs.append(ItemSpec(f"thumbnail-{thumbnail.id}", "thumbnail", {"work_id": work.id, "record_id": thumbnail.id, "revision": intent.revision, "work_revision_id": work_revision.id if work_revision else None, "path": f"{directory}/.thumbnails/{work.pixiv_illust_id}_p0_v1.webp", "force": bool(required)}, "thumbnails", "previews", tuple(required)))
        if work.type == "ugoira":
            motion = session.scalar(self._derivatives_statement(session, work.id).where(
                Derivative.kind == "thumbnail", Derivative.page_index == 0, Derivative.spec_key == CARD_MOTION_SPEC))
            if required or motion is None or motion.state != "verified" or not preview.sha256 or motion.source_version != preview.sha256:
                if motion is None:
                    motion = Derivative(work_id=work.id, revision_id=work_revision.id if work_revision else None, kind="thumbnail", spec_key=CARD_MOTION_SPEC)
                    session.add(motion); session.flush()
                    if work_revision: bind_derivative(session, work_revision, motion)
                specs.append(ItemSpec(f"thumbnail-{motion.id}", "thumbnail", {
                    "work_id": work.id, "record_id": motion.id, "revision": intent.revision, "work_revision_id": work_revision.id if work_revision else None,
                    "path": f"{directory}/.thumbnails/{work.pixiv_illust_id}_card_motion_v1.webp", "force": bool(required)},
                    "thumbnails", "previews", tuple(required)))
        return specs

    def plan_batch(self, parameters, context, cursor):
        from .task_planning import plan_batch
        return plan_batch(self, parameters, context, cursor)

    def plan(self, parameters, context):
        specs = []
        if self.kind == "delete":
            # The API already commits suppression immediately; repeating it is idempotent.
            for work_id in sorted(set(parameters["work_ids"])):
                for journal_id in self.publisher.suppress(work_id):
                    specs.append(ItemSpec(f"delete-{journal_id}", "delete", {"work_id": work_id, "journal_id": journal_id}, "deleting", "files"))
            return specs
        if self.kind in ("metadata", "bookmarks"):
            if self.kind == "metadata" and "work_ids" in parameters:
                return [ItemSpec(f"metadata-work-{work_id}", "metadata_work",
                    {"work_id": work_id}, "metadata", "works") for work_id in self._work_ids(parameters)]
            ids = self._artist_ids(parameters, context)
            for artist_id in ids:
                specs.append(ItemSpec(f"{self.kind}-artist-{artist_id}",
                    "metadata_artist" if self.kind == "metadata" else "bookmark_artist",
                    {"artist_id": artist_id}, "metadata" if self.kind == "metadata" else "bookmarks", "artists"))
                if self.kind == "metadata":
                    for work_id in self._work_ids({"artist_ids": [artist_id]}):
                        specs.append(ItemSpec(f"metadata-work-{work_id}", "metadata_work",
                            {"work_id": work_id}, "captions", "works"))
            return specs
        if self.kind == "check":
            ids = self._artist_ids(parameters, context)
            context.report("checking", 0, len(ids), "artists", "正在检查画师")
            for index, artist_id in enumerate(ids):
                try:
                    with self.database.read() as session:
                        artist = session.get(Artist, artist_id)
                        if artist is None:
                            raise KeyError(artist_id)
                        remote_id, name = artist.pixiv_user_id, artist.name
                    context.report("checking", index, len(ids), "artists", f"正在检查 {name}")
                    if self.profiles is not None and (artist.profile_refreshed_at is None or artist.profile_refreshed_at < utcnow() - timedelta(days=1)):
                        with self.database.write() as session:
                            parent = session.get(Operation, context.operation_id)
                            context.queue.enqueue_in_transaction(session, "metadata", {"artist_ids": [artist_id]},
                                f"刷新 {name} 的资料", parent.source, parent_id=context.operation_id,
                                specs=[ItemSpec(f"metadata-artist-{artist_id}", "metadata_artist", {"artist_id": artist_id}, "metadata", "artists")])
                    versions, account = self._bookmark_scope(artist_id)
                    source_account = self.source_status.account_context() if self.source_status else account
                    sources = self._source_scope(artist_id, source_account)
                    for discovery_index, data in enumerate(self.client.get_all_artist_illusts(remote_id)):
                        try:
                            metadata, metadata_error = None, None
                            if data.get("type") == "ugoira":
                                with self.database.read() as session:
                                    existing = session.scalar(select(Work).where(Work.pixiv_illust_id == str(data["illust_id"])))
                                    deferred = existing and (existing.source_metadata or {}).get("original_backfill_requires_selection")
                                    needs_metadata = existing is None or bool(session.scalar(select(Asset.id).where(Asset.work_id == existing.id, Asset.state.in_(("missing", "failed", "invalid", "mismatch", "source_changed"))).limit(1))) or bool(existing and (existing.source_metadata or {}).get("source_updates")) or bool(existing and data.get("urls", []) != (existing.source_metadata or {}).get("urls", []))
                                if needs_metadata and not deferred:
                                    try:
                                        metadata = self.client.get_ugoira_metadata(str(data["illust_id"]))
                                    except Exception as exc:
                                        if isinstance(exc, LocalReadError):
                                            raise
                                        metadata_error = safe_failure(exc)
                            observation = sources.get(str(data["illust_id"]), {})
                            work_id = self._save_remote_work(artist_id, data, context=context, metadata=metadata, metadata_error=metadata_error,
                                expected_work_id=observation.get("work_id"), expected_revision=observation.get("revision"),
                                expected_source_version=observation.get("version"))
                            if observation and work_id is not None:
                                self.source_status.apply_available("work", work_id, data, observation["version"], source_account)
                        except WorkMetadataConflict as exc:
                            failure = {**safe_failure(exc), "pixiv_id": str(data["illust_id"])}
                            specs.append(ItemSpec(f"work-conflict-{artist_id}-{discovery_index}",
                                "error", failure, "checking", "works"))
                            continue
                        # New works begin at version zero; any newer user action
                        # wins before this older discovery response is applied.
                        self._apply_bookmark(work_id, data, versions.get(str(data["illust_id"]), 0), account)
                    specs.append(ItemSpec(f"checked-artist-{artist_id}", "checked", {"artist_id": artist_id}, "checking", "artists"))
                    with self.database.write() as session:
                        session.get(Artist, artist_id).last_checked_at = utcnow()
                except Exception as exc:
                    if isinstance(exc, LocalReadError):
                        raise
                    specs.append(ItemSpec(f"artist-error-{artist_id}", "error", safe_failure(exc), "checking", "artists"))
                context.report("checking", index + 1, len(ids), "artists", "已检查画师")
            return specs
        else:
            work_ids = self._work_ids(parameters)
        context.report("inventory", 0, len(work_ids), "works", "正在清点本地文件")
        for index, work_id in enumerate(work_ids):
            try:
                work_specs = self._plan_work(work_id, parameters, context)
                specs.extend(work_specs)
                with self.database.write() as session:
                    context.queue.materialize_in_transaction(session, context.operation_id, work_specs)
            except SuppressedWork:
                specs.append(ItemSpec(f"suppressed-{work_id}", "skip", {"work_id": work_id, "message": "作品已删除，旧任务不再处理"}, "checking", "works"))
            except Exception as exc:
                if isinstance(exc, LocalReadError):
                    raise
                specs.append(ItemSpec(f"work-error-{work_id}", "error", {**safe_failure(exc), "work_id": work_id}, "checking", "works"))
            context.report("inventory", index + 1, len(work_ids), "works", "已清点作品")
        return specs

    def _work_ids(self, parameters):
        with self.database.read() as session:
            statement = select(Work.id).where(~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed")))
            if "work_ids" in parameters:
                statement = statement.where(Work.id.in_(parameters["work_ids"]))
            elif "artist_ids" in parameters:
                statement = statement.where(Work.artist_id.in_(parameters["artist_ids"]))
            return list(session.scalars(statement.order_by(Work.id)))

    @staticmethod
    def _retain_legacy_source_update(session, asset):
        if asset.state != "source_changed":
            return False
        work = session.get(Work, asset.work_id)
        updates = dict((work.source_metadata or {}).get("source_updates", {}))
        updates.setdefault(asset.id, {"requested_url": asset.source_url, "reason": "历史来源变更尚未补齐，现有文件保留"})
        work.source_metadata = {**work.source_metadata, "source_updates": updates}
        asset.state = "unknown" if asset.relative_path else "missing"
        asset.error = None
        return True

    def _inspect_asset(self, asset, force=False):
        if asset.state in ("source_changed", "mismatch") and not force:
            return False
        try:
            if not asset.relative_path:
                raise InvalidMedia("缺少原件")
            path = contained_path(self.publisher.root, asset.relative_path)
            stat = path.stat()
            if (not force and asset.state == "verified" and stat.st_size > 0
                    and (stat.st_size, stat.st_mtime_ns, stat.st_ino)
                    == (asset.byte_size, asset.file_mtime_ns, asset.file_identity)):
                return True
            verified = verify_media(path, asset.role)
            with self.database.write() as session:
                stored = session.get(Asset, asset.id)
                if stored is None:
                    return False
                before = (stored.state, stored.error, stored.byte_size, stored.sha256, stored.width, stored.height, stored.file_mtime_ns, stored.file_identity)
                self._retain_legacy_source_update(session, stored)
                stored.state, stored.error, stored.verified_at = "verified", None, utcnow()
                stored.byte_size, stored.sha256 = verified.byte_size, verified.sha256
                stored.width, stored.height = verified.width, verified.height
                stored.file_mtime_ns, stored.file_identity = stat.st_mtime_ns, stat.st_ino
                refresh_for_record(session, stored)
                after = (stored.state, stored.error, stored.byte_size, stored.sha256, stored.width, stored.height, stored.file_mtime_ns, stored.file_identity)
                if before != after:
                    bump_catalogue_revision(session)
            return True
        except (InvalidMedia, OSError, ValueError) as exc:
            with self.database.write() as session:
                stored = session.get(Asset, asset.id)
                if stored is None:
                    return False
                # A thumbnail inventory may observe the same absent path after a
                # download failed. Keep the last download result until a new
                # attempt succeeds or produces a newer failure.
                if stored.state != "failed":
                    stored.state = "invalid" if asset.relative_path else "missing"
                    stored.error = getattr(exc, "user_message", "原件缺失或无法读取")
                    refresh_for_record(session, stored)
                    bump_catalogue_revision(session)
            return False

    def _plan_work(self, work_id, parameters, context):
        with self.database.write() as session:
            stored_work = session.get(Work, work_id)
            if stored_work is None:
                raise SuppressedWork()
            intent = session.get(ArchiveIntent, stored_work.pixiv_illust_id)
            if intent is None or intent.mode != "keep":
                raise SuppressedWork()
            legacy_changed = bool([self._retain_legacy_source_update(session, asset) for asset in stored_work.assets if asset.state == "source_changed"])
            work_revision = ensure_legacy_revision(session, stored_work)
            updates = (stored_work.source_metadata or {}).get("source_updates", {})
            if updates and work_revision.type != "ugoira" and self.kind not in ("verify", "thumbnails"):
                snapshot = {key: getattr(work_revision, key) for key in (
                    "title", "caption", "caption_state", "type", "page_count", "tags")}
                source = {**work_revision.source_metadata, "source_updates": {}}
                urls = [dict(url) for url in source.get("urls", [])]
                while len(urls) < work_revision.page_count:
                    urls.append({})
                for entry, asset in asset_entries(session, work_revision):
                    update = updates.get(asset.id)
                    if entry.role == "image" and update:
                        urls[entry.page_index]["original"] = update.get("requested_url") or asset.source_url or ""
                source["urls"] = urls
                snapshot["source_metadata"] = source
                work_revision, _ = observe_revision(session, stored_work, snapshot, compare_content=True)
                stored_work.source_metadata = {**work_revision.source_metadata, "source_updates": {}}
            stored_work = project_revision(stored_work, work_revision)
            known = {(entry.role, entry.page_index) for entry in session.scalars(select(RevisionAsset).where(RevisionAsset.revision_id == work_revision.id))}
            roles = [("ugoira_zip", 0), ("ugoira_timing", 0)] if stored_work.type == "ugoira" else [("image", index) for index in range(stored_work.page_count)]
            added = False
            urls = (stored_work.source_metadata or {}).get("urls", [])
            for role, index in roles:
                if (role, index) not in known:
                    missing_asset = Asset(work_id=work_id, revision_id=work_revision.id, role=role, page_index=index, state="missing", source_url=urls[index].get("original") if role == "image" and index < len(urls) else None)
                    session.add(missing_asset); bind_asset(session, work_revision, missing_asset)
                    added = True
            if added or legacy_changed:
                bump_catalogue_revision(session)
        with self.database.read() as session:
            work = session.scalar(select(Work).options(selectinload(Work.assets), selectinload(Work.artist)).where(Work.id == work_id))
            intent = session.get(ArchiveIntent, work.pixiv_illust_id)
            if intent is None or intent.mode != "keep":
                raise SuppressedWork()
            revision = intent.revision
            work_revision = latest_revision(session, work.id)
            work = project_revision(work, work_revision)
            directory = revision_directory(work, work_revision, work.artist.directory or f"artist_{work.artist.pixiv_user_id}")
            assets = list(session.scalars(self._assets_statement(session, work.id)))
            operation = session.get(Operation, context.operation_id)
            maintenance = operation.source == "maintenance"
            # A manual repair selects the originals of every work in its scope.
            # Automatic checks and maintenance retain the legacy opt-in boundary.
            selected_originals = self.kind == "repair" and operation.source == "manual"
            explicit_recheck = operation.source == "manual" and work_id in parameters.get("work_ids", ())
            availability = (work.source_metadata or {}).get("remote_availability", {})
            availability = availability if isinstance(availability, dict) else {}
            from .source_status import availability_snapshot
            source_account = self.source_status.account_context() if self.source_status else self._bookmark_account()
            source_fact = availability_snapshot(availability, source_account) or {}
            known_unavailable = source_fact.get("effective") and source_fact.get("state") in ("unavailable", "private", "deleted")
            ledger = session.get(ApplicationState, f"maintenance_work:{work_id}")
            remote_track = ((ledger.value.get("tracks", {}).get("remote", {})
                if ledger and isinstance(ledger.value, dict) else {}))
            blocked_remote = remote_track if self.kind == "check" and remote_track.get("status") == "attention" else None
        specs, required = [], []
        if self.kind == "verify":
            for asset in assets:
                specs.append(ItemSpec(f"verify-{asset.id}", "verify_asset", {"asset_id": asset.id, "work_id": work_id, "revision": revision, "work_revision_id": work_revision.id if work_revision else None}, "verifying", "assets"))
            if work.type == "ugoira":
                specs.append(ItemSpec(f"verify-pair-{work_id}", "ugoira_pair", {"work_id": work_id, "revision": revision, "work_revision_id": work_revision.id if work_revision else None}, "verifying", "works", tuple(spec.key for spec in specs)))
                with self.database.read() as session:
                    preview = session.scalar(self._derivatives_statement(session, work_id).where( Derivative.kind == "gif", Derivative.spec_key == "v1"))
                specs.append(ItemSpec(f"verify-gif-{work_id}", "verify_derivative", {"work_id": work_id, "record_id": preview.id if preview else None, "role": "gif", "revision": revision, "work_revision_id": work_revision.id if work_revision else None}, "verifying", "previews"))
            return specs
        remote_failure = None
        defer_originals = work.source_metadata.get("original_backfill_requires_selection") and not (self.kind == "backfill" or selected_originals)
        local_only = self.kind in ("verify", "thumbnails")
        if self.kind != "thumbnails" and not defer_originals:
            context.detail(f"正在核验 {work.title} · {len(assets)} 项原件")
            # Routine maintenance trusts recorded verified siblings. It only
            # inspects the explicit gap; full-file verification is a separate
            # user action and must not be hidden in an automatic repair.
            updates = (work.source_metadata or {}).get("source_updates", {})
            inspected = (asset for asset in assets if asset.state in ("missing", "failed", "invalid", "mismatch", "source_changed") or asset.id in updates or not maintenance and self.kind in ("repair", "backfill") or self.kind == "backfill" and asset.state == "missing_deferred")
            missing = [asset for asset in inspected if asset.id in updates or asset.state == "source_changed" or not self._inspect_asset(asset)]
            metadata = None
            work_fact_checked = False
            if missing and not local_only and known_unavailable and not explicit_recheck:
                remote_failure = {"message": availability.get("reason") or "原作已无法访问，已有本地内容保留",
                    "failure_code": "work_private" if availability["state"] == "private" else "work_unavailable",
                    "retryable": False}
                specs.append(ItemSpec(f"remote-skip-{work_id}", "skip", {**remote_failure, "work_id": work_id},
                                      "downloading", "works"))
            if missing and not local_only and blocked_remote and not remote_failure:
                remote_failure = {"message": blocked_remote.get("reason") or "来源缺项等待手动核对",
                    "failure_code": blocked_remote.get("failure_code"), "retryable": False}
                specs.append(ItemSpec(f"remote-error-{work_id}", "error", {**remote_failure, "work_id": work_id},
                                      "downloading", "assets"))
            if missing and not local_only and not remote_failure:
                try:
                    if known_unavailable and explicit_recheck:
                        work_fact_checked = True
                        self._refresh_work(work.artist_id, work.pixiv_illust_id, work_id)
                    if work.type == "ugoira":
                        metadata = self.client.get_ugoira_metadata(work.pixiv_illust_id)
                        requested = metadata.get("zip_urls", {}).get("original") or metadata.get("zip_urls", {}).get("medium")
                        with self.database.write() as session:
                            owner = session.get(Work, work_id)
                            source_updates = dict((owner.source_metadata or {}).get("source_updates", {}))
                            for missing_asset in missing:
                                if missing_asset.role == "ugoira_zip" and requested:
                                    stored = session.get(Asset, missing_asset.id)
                                    stored.source_url = requested
                                    if stored.id in source_updates:
                                        source_updates[stored.id] = {**source_updates[stored.id], "requested_url": requested}
                            owner.source_metadata = {**owner.source_metadata, "source_updates": source_updates}
                            self._remember_timing_source(session, owner, metadata)
                        with self.database.read() as session:
                            owner = session.get(Work, work_id)
                            work_revision = latest_revision(session, work_id)
                            work = project_revision(owner, work_revision)
                            directory = revision_directory(work, work_revision, work.artist.directory or f"artist_{work.artist.pixiv_user_id}")
                            assets = revision_assets(session, work_revision)
                            missing = [asset for asset in assets if asset.state != "verified"]
                        self._resume_ugoira_attention(work_id)
                    elif any(not asset.source_url for asset in missing):
                        self._refresh_work(work.artist_id, work.pixiv_illust_id, work_id)
                        with self.database.read() as session:
                            owner = session.get(Work, work_id)
                            work_revision = latest_revision(session, work_id)
                            work = project_revision(owner, work_revision)
                            directory = revision_directory(work, work_revision, work.artist.directory or f"artist_{work.artist.pixiv_user_id}")
                            assets = revision_assets(session, work_revision)
                            missing = [asset for asset in assets if asset.state != "verified"]
                except Exception as exc:
                    if isinstance(exc, LocalReadError):
                        raise
                    # Animation subresource failure does not establish a work fact.
                    # Check detail once for terminal metadata absence; temporary
                    # network/auth/rate limits retain their own failure cause.
                    if (work.type == "ugoira" and not work_fact_checked
                            and getattr(exc, "failure_code", None) == "ugoira_metadata_unavailable"
                            and callable(getattr(self.client, "get_illust_detail", None))):
                        try:
                            self._refresh_work(work.artist_id, work.pixiv_illust_id, work_id)
                        except Exception as detail_exc:
                            if isinstance(detail_exc, LocalReadError):
                                raise
                            exc = detail_exc
                    remote_failure = safe_failure(exc)
                    with self.database.write() as session:
                        for asset in missing:
                            stored = session.get(Asset, asset.id)
                            if stored is not None and stored.state in ("missing", "failed", "invalid", "mismatch"):
                                stored.error = remote_failure["message"]
                    specs.append(ItemSpec(f"remote-error-{work_id}", "error", {**remote_failure, "work_id": work_id},
                                          "downloading", "assets"))
            for asset in missing:
                if remote_failure:
                    break
                key = f"asset-{asset.id}"
                required.append(key)
                if local_only:
                    specs.append(ItemSpec(key, "error", {"work_id": work_id, "message": "原件缺失或损坏，可选择补下载"}, "verifying", "pages"))
                    continue
                target = {"work_id": work_id, "asset_id": asset.id, "revision": revision, "work_revision_id": work_revision.id if work_revision else None}
                if asset.role == "ugoira_timing":
                    target.update({"frames": metadata["frames"], "requested_url": asset.source_url, "expected_fingerprint": self._timing_fingerprint(metadata), "path": asset.relative_path or f"{directory}/{work.pixiv_illust_id}.frames.json"})
                    kind = "timing"
                else:
                    url = (metadata.get("zip_urls", {}).get("original") or metadata["zip_urls"]["medium"]) if asset.role == "ugoira_zip" else asset.source_url
                    if not url:
                        raise InvalidMedia("缺少此页原件的下载地址")
                    extension = ".zip" if asset.role == "ugoira_zip" else PurePosixPath(urlsplit(url).path).suffix.lower()
                    if extension not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip"):
                        raise InvalidMedia("原件的文件类型无法识别")
                    target.update({"url": url, "requested_url": url, "path": asset.relative_path or f"{directory}/{work.pixiv_illust_id}_p{asset.page_index}{extension}"})
                    kind = "download"
                specs.append(ItemSpec(key, kind, target, "downloading", "assets"))
        if self.kind == "thumbnails" and work.type != "ugoira":
            first_page_ready = False
            for asset in assets:
                if asset.page_index == 0 and asset.role == "image":
                    first_page_ready = self._inspect_asset(asset)
            if not first_page_ready:
                # A local-only request cannot fetch the first page. A later
                # check/repair round will retain its download -> thumbnail edge.
                return specs
        if work.type == "ugoira":
            if not defer_originals and self.kind != "thumbnails":
                if required and not local_only:
                    key = f"pair-{work_id}"
                    specs.append(ItemSpec(key, "ugoira_pair", {"work_id": work_id, "revision": revision, "work_revision_id": work_revision.id if work_revision else None},
                        "verifying", "works", tuple(required)))
                    required = [key]
                elif not required and not remote_failure:
                    self._verify_pair(work_id)
            with self.database.read() as session:
                preview = session.scalar(self._derivatives_statement(session, work_id).where( Derivative.kind == "gif", Derivative.spec_key == "v1"))
            usable = self._derivative_usable(preview, "gif")
            pair_ready = all(asset.state == "verified" for asset in assets
                             if asset.role in ("ugoira_zip", "ugoira_timing")) and len(assets) >= 2
            if (not usable or required) and not defer_originals and not local_only and (not remote_failure or pair_ready):
                preview_id = self._ensure_derivative(work_id, "gif", for_rebuild=bool(required))
                key = f"preview-{preview_id}"
                specs.append(ItemSpec(key, "gif", {"work_id": work_id, "record_id": preview_id,
                    "path": (preview.relative_path if preview and preview_id == preview.id else None) or f"{directory}/{work.pixiv_illust_id}.gif", "revision": revision, "work_revision_id": work_revision.id if work_revision else None, "force": bool(required)},
                    "converting", "previews", tuple(required)))
                required = [key]
            elif not usable:
                return specs
        with self.database.read() as session:
            thumbnail = session.scalar(self._derivatives_statement(session, work_id).where(
                Derivative.kind == "thumbnail", Derivative.page_index == 0, Derivative.spec_key == "v1"))
        first_asset = next((asset for asset in assets if asset.role == "image" and asset.page_index == 0), None)
        relevant_required = required if work.type == "ugoira" else [key for key in required if first_asset and key == f"asset-{first_asset.id}"]
        if (relevant_required or not self._derivative_usable(thumbnail, "thumbnail")) and self.kind != "verify":
            if work.type != "ugoira":
                # Later missing pages must not prevent a usable first-page card.
                first_asset = next((asset for asset in assets if asset.role == "image" and asset.page_index == 0), None)
                required = [key for key in required if first_asset and key == f"asset-{first_asset.id}"]
                if remote_failure and (first_asset is None or not self._inspect_asset(first_asset)):
                    return specs
            elif remote_failure and not (usable or pair_ready):
                return specs
            thumbnail_id = self._ensure_derivative(work_id, "thumbnail", for_rebuild=bool(required))
            specs.append(ItemSpec(f"thumbnail-{thumbnail_id}", "thumbnail", {
                "work_id": work_id, "record_id": thumbnail_id, "revision": revision, "work_revision_id": work_revision.id if work_revision else None,
                "path": f"{directory}/.thumbnails/{work.pixiv_illust_id}_p0_v1.webp", "force": bool(required)}, "thumbnails", "previews", tuple(required)))
        if work.type == "ugoira" and self.kind != "verify":
            with self.database.read() as session:
                motion = session.scalar(self._derivatives_statement(session, work_id).where(
                    Derivative.kind == "thumbnail", Derivative.page_index == 0, Derivative.spec_key == CARD_MOTION_SPEC))
            if required or not self._derivative_usable(motion, "thumbnail"):
                motion_id = self._ensure_derivative(work_id, "thumbnail", CARD_MOTION_SPEC, for_rebuild=bool(required))
                specs.append(ItemSpec(f"thumbnail-{motion_id}", "thumbnail", {
                    "work_id": work_id, "record_id": motion_id, "revision": revision, "work_revision_id": work_revision.id if work_revision else None,
                    "path": f"{directory}/.thumbnails/{work.pixiv_illust_id}_card_motion_v1.webp", "force": bool(required)},
                    "thumbnails", "previews", tuple(required)))
        if self.kind in ("repair", "verify", "backfill") and not maintenance:
            self._clear_remote_attention_if_complete(work_id)
        return specs

    def _verify_pair(self, work_id, revision_id=None):
        with self.database.read() as session:
            assets = {asset.role: asset for asset in session.scalars(self._assets_statement(session, work_id, revision_id))}
        if any(role not in assets or assets[role].state != "verified" for role in ("ugoira_zip", "ugoira_timing")):
            raise InvalidMedia("缺少已核验的动图原包或播放时序")
        try:
            verify_ugoira_pair(contained_path(self.publisher.root, assets["ugoira_zip"].relative_path),
                              contained_path(self.publisher.root, assets["ugoira_timing"].relative_path))
        except InvalidMedia as exc:
            with self.database.write() as session:
                for role in ("ugoira_zip", "ugoira_timing"):
                    stored = session.get(Asset, assets[role].id)
                    stored.state, stored.error = "mismatch", exc.user_message
                    refresh_for_record(session, stored)
            raise
        with self.database.write() as session:
            work = session.get(Work, work_id)
            work.source_metadata = {**work.source_metadata, "original_backfill_requires_selection": False}

    def _source_version(self, record):
        with self.database.read() as session:
            work = session.get(Work, record.work_id)
            if work is None:
                return None
            if record.kind == "gif":
                inputs = list(session.scalars(self._assets_statement(session, work.id, record.revision_id).where(
                    Asset.role.in_(("ugoira_zip", "ugoira_timing"))).order_by(Asset.role)))
                expected = 2
            elif (session.get(WorkRevision, record.revision_id).type if record.revision_id else work.type) == "ugoira":
                inputs = list(session.scalars(self._derivatives_statement(session, work.id, record.revision_id).where(
                    Derivative.kind == "gif", Derivative.spec_key == "v1")))
                expected = 1
            else:
                inputs = list(session.scalars(self._assets_statement(session, work.id, record.revision_id).where(
                    Asset.role == "image", Asset.page_index == 0)))
                expected = 1
            if len(inputs) != expected or any(not source.sha256 or source.state != "verified" for source in inputs):
                return None
            return ":".join(source.sha256 for source in inputs)

    def _ensure_derivative(self, work_id, kind, spec_key="v1", revision_id=None, for_rebuild=False):
        with self.database.write() as session:
            record = session.scalar(self._derivatives_statement(session, work_id, revision_id).where(
                Derivative.kind == kind, Derivative.page_index == 0, Derivative.spec_key == spec_key))
            work_revision = session.get(WorkRevision, revision_id) if revision_id else latest_revision(session, work_id)
            if record is None or for_rebuild and work_revision and record.revision_id != work_revision.id:
                work_revision = session.get(WorkRevision, revision_id) if revision_id else latest_revision(session, work_id)
                record = Derivative(work_id=work_id, revision_id=work_revision.id if work_revision else None, kind=kind, spec_key=spec_key)
                session.add(record)
                session.flush()
                if work_revision: bind_derivative(session, work_revision, record)
            return record.id

    def _derivative_usable(self, record, role, force=False):
        if not record or not record.relative_path:
            return False
        source_version = self._source_version(record)
        if record.spec_key == CARD_MOTION_SPEC and (not source_version or not record.source_version):
            return False
        if record.source_version and source_version and source_version != record.source_version:
            with self.database.write() as session:
                stored = session.get(Derivative, record.id)
                if stored is None:
                    return False
                stored.state, stored.error = "outdated", "输入文件已改变，等待重建预览"
                bump_catalogue_revision(session)
            return False
        try:
            path = contained_path(self.publisher.root, record.relative_path)
            stat = path.stat()
            if (not force and record.state == "verified" and stat.st_size > 0 and
                    (stat.st_size, stat.st_mtime_ns, stat.st_ino) == (record.byte_size, record.file_mtime_ns, record.file_identity)):
                return True
            verified = verify_media(path, role)
            with self.database.write() as session:
                stored = session.get(Derivative, record.id)
                if stored is None:
                    return False
                stored.state, stored.error = "verified", None
                stored.width, stored.height = verified.width, verified.height
                stored.byte_size, stored.sha256, stored.verified_at = verified.byte_size, verified.sha256, utcnow()
                stored.file_mtime_ns, stored.file_identity = stat.st_mtime_ns, stat.st_ino
                refresh_for_record(session, stored)
                bump_catalogue_revision(session)
            return True
        except (InvalidMedia, OSError, ValueError):
            with self.database.write() as session:
                stored = session.get(Derivative, record.id)
                if stored is None:
                    return False
                stored.state, stored.error = "invalid", "预览文件缺失或损坏，可以重建"
                refresh_for_record(session, stored)
                bump_catalogue_revision(session)
            return False

    def execute(self, item, context):
        if item.kind in ("discover_page", "discover_work", "bookmark_page", "bookmark_finalize", "bookmark_artist"):
            from .task_planning import execute
            return execute(self, item, context)
        if item.kind == "checked":
            return ItemResult()
        if item.kind == "verify_asset":
            with self.database.read() as session:
                record = session.get(Asset, item.target["asset_id"])
                work = session.get(Work, item.target["work_id"])
                intent = session.get(ArchiveIntent, work.pixiv_illust_id) if work else None
                if record is None or intent is None or intent.mode != "keep" or intent.revision != item.target["revision"]:
                    return ItemResult("skipped", {"message": "保存决定已改变"})
            valid = self._inspect_asset(record, force=True)
            if valid:
                self._clear_remote_attention_if_complete(item.target["work_id"])
            return ItemResult("succeeded" if valid else "failed", {"message": "原件核验完成"})
        if item.kind == "verify_derivative":
            with self.database.read() as session:
                work = session.get(Work, item.target["work_id"])
                intent = session.get(ArchiveIntent, work.pixiv_illust_id) if work else None
                if intent is None or intent.mode != "keep" or intent.revision != item.target["revision"]:
                    return ItemResult("skipped", {"message": "保存决定已改变"})
                record = session.get(Derivative, item.target["record_id"]) if item.target.get("record_id") else None
            return ItemResult("succeeded" if self._derivative_usable(record, item.target["role"]) else "failed", {"message": "预览核验完成" if record else "缺少动图预览"})
        if item.kind in ("metadata_artist", "metadata_work", "bookmark_artist"):
            return self._execute_metadata(item)
        if item.kind == "delete":
            state = self.publisher.finish_deletion(item.target["journal_id"])
            return ItemResult("succeeded" if state == "committed" else "skipped")
        if item.kind == "error":
            return ItemResult("failed", item.target)
        if item.kind == "skip":
            return ItemResult("skipped", item.target)
        if item.kind == "ugoira_pair":
            with self.database.read() as session:
                work = session.get(Work, item.target["work_id"])
                intent = session.get(ArchiveIntent, work.pixiv_illust_id) if work else None
                if intent is None or intent.mode != "keep" or intent.revision != item.target["revision"]:
                    return ItemResult("skipped", {"message": "保存决定已改变"})
            self._verify_pair(item.target["work_id"], item.target.get("work_revision_id"))
            return ItemResult()
        target = item.target
        derivative = item.kind in ("gif", "thumbnail")
        record_id = target["record_id"] if derivative else target["asset_id"]
        with self.database.read() as session:
            record = session.get(Derivative if derivative else Asset, record_id)
            if record is None:
                return ItemResult("skipped", {"message": "作品已删除，此项不再执行"})
            work = session.get(Work, record.work_id)
            work_revision_id = target.get("work_revision_id")
            if work_revision_id:
                snapshot = session.get(WorkRevision, work_revision_id)
                binding_type = RevisionDerivative if derivative else RevisionAsset
                binding_column = binding_type.derivative_id if derivative else binding_type.asset_id
                bound = session.scalar(select(binding_column).where(binding_type.revision_id == work_revision_id,
                    binding_column == record.id).limit(1))
                if snapshot is None or snapshot.work_id != record.work_id or bound is None:
                    return ItemResult("skipped", {"message": "版本与媒体引用不匹配，此项不再发布"})
            intent = session.get(ArchiveIntent, work.pixiv_illust_id) if work else None
            if intent is None or intent.mode != "keep" or intent.revision != target["revision"]:
                return ItemResult("skipped", {"message": "保存决定已改变，此项不再执行"})
        requested_url = target.get("requested_url", target.get("url"))
        update = (work.source_metadata or {}).get("source_updates", {}).get(record_id)
        if requested_url and record.source_url and requested_url != record.source_url:
            return ItemResult("skipped", {"message": "来源已再次更新，此旧任务不再发布"})
        if not derivative and not update and self._inspect_asset(record):
            return ItemResult(detail={"message": "原件已核验，无需再次下载"})
        if derivative and not target.get("force") and self._derivative_usable(record, item.kind):
            return ItemResult(detail={"message": "预览已核验，无需再次生成"})
        relative, staged = self.publisher.staging()
        source_version = None
        try:
            if item.kind == "download":
                self.downloader.download(target["url"], staged, context.report_bytes)
                verified = verify_media(staged, record.role)
            elif item.kind == "timing":
                with staged.open("x", encoding="utf-8") as output:
                    json.dump({"frames": target["frames"]}, output, ensure_ascii=False)
                verified = verify_media(staged, "ugoira_timing")
            else:
                with self.database.read() as session:
                    if item.kind == "gif":
                        assets = {asset.role: asset for asset in session.scalars(self._assets_statement(session, work.id, target.get("work_revision_id") or record.revision_id))}
                        if any(role not in assets or assets[role].state != "verified" for role in ("ugoira_zip", "ugoira_timing")):
                            raise InvalidMedia("缺少已核验的动图原包或播放时序")
                        source = contained_path(self.publisher.root, assets["ugoira_zip"].relative_path)
                        timing = contained_path(self.publisher.root, assets["ugoira_timing"].relative_path)
                    elif (session.get(WorkRevision, target.get("work_revision_id") or record.revision_id).type if target.get("work_revision_id") or record.revision_id else work.type) == "ugoira":
                        preview = session.scalar(self._derivatives_statement(session, work.id, target.get("work_revision_id") or record.revision_id).where( Derivative.kind == "gif", Derivative.spec_key == "v1"))
                        if preview is None or not preview.relative_path:
                            raise InvalidMedia("缺少动图预览，无法生成缩略图")
                        source = contained_path(self.publisher.root, preview.relative_path)
                    else:
                        original = session.scalar(self._assets_statement(session, work.id, target.get("work_revision_id") or record.revision_id).where( Asset.role == "image", Asset.page_index == 0))
                        if original is None or not original.relative_path:
                            raise InvalidMedia("缺少第一张原件，无法生成缩略图")
                        source = contained_path(self.publisher.root, original.relative_path)
                if record.spec_key == CARD_MOTION_SPEC:
                    if not self._derivative_usable(preview, "gif"):
                        raise InvalidMedia("缺少已核验的本地 GIF，无法生成卡片预览")
                    source_version = self._source_version(record)
                    source_stat = source.stat()
                    fingerprint = (source_stat.st_size, source_stat.st_mtime_ns, source_stat.st_ino)
                    verified = build_card_motion(source, staged, checkpoint=context.checkpoint)
                    current = source.stat()
                    if fingerprint != (current.st_size, current.st_mtime_ns, current.st_ino) or source_version != self._source_version(record):
                        raise InvalidMedia("生成期间本地 GIF 已改变，等待重建卡片预览")
                    context.checkpoint()
                else:
                    verified = build_gif(source, timing, staged, checkpoint=context.checkpoint) if item.kind == "gif" else build_thumbnail(source, staged, checkpoint=context.checkpoint)
            journal_id = self.publisher.prepare(record_id, relative, target["path"], verified,
                derivative=derivative, item_id=context.item_id, expected_revision=target["revision"],
                source_version=(source_version or self._source_version(record)) if derivative else None, requested_url=requested_url, expected_source_fingerprint=target.get("expected_fingerprint"))
            state = self.publisher.publish(journal_id)
            if state == "committed" and not derivative:
                self._clear_remote_attention_if_complete(work.id)
            return ItemResult("succeeded" if state == "committed" else "skipped")
        except SuppressedWork:
            return ItemResult("skipped", {"message": "作品已删除，旧任务不再发布文件"})
        except Exception as exc:
            if isinstance(exc, LocalReadError):
                raise
            with self.database.write() as session:
                stored = session.get(Derivative if derivative else Asset, record_id)
                if stored is not None:
                    if not derivative and stored.state in ("verified", "unknown", "source_changed") and stored.relative_path:
                        owner = session.get(Work, stored.work_id)
                        updates = dict((owner.source_metadata or {}).get("source_updates", {}))
                        if requested_url and (not updates.get(record_id) or updates[record_id].get("requested_url") == requested_url):
                            updates[record_id] = {**updates.get(record_id, {}), "requested_url": requested_url, "reason": safe_failure(exc)["message"]}
                            owner.source_metadata = {**owner.source_metadata, "source_updates": updates}
                    else:
                        stored.state, stored.error = "failed", safe_failure(exc)["message"]
                    refresh_for_record(session, stored)
                    bump_catalogue_revision(session)
            raise

    def _execute_metadata(self, item):
        if item.kind == "metadata_work":
            with self.database.read() as session:
                work = session.get(Work, item.target["work_id"])
                intent = session.get(ArchiveIntent, work.pixiv_illust_id) if work else None
                if work is None or intent is None or intent.mode == "suppressed":
                    return ItemResult("skipped", {"message": "作品已删除，不再刷新信息"})
                artist_id, remote_id = work.artist_id, work.pixiv_illust_id
            result = self._refresh_work(artist_id, remote_id, item.target["work_id"])
            return ItemResult("succeeded" if result else "skipped")
        artist_id = item.target["artist_id"]
        with self.database.read() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                return ItemResult("skipped", {"message": "画师记录不存在"})
            remote_id = artist.pixiv_user_id
        if item.kind == "metadata_artist":
            if self.profiles is not None:
                self.profiles.refresh(artist_id)
            else:
                data = self.client.get_artist_detail(remote_id)
                with self.database.write() as session:
                    artist = session.get(Artist, artist_id)
                    artist.name, artist.account, artist.avatar_url = data["name"], data.get("account"), data.get("avatar_url")
        else:
            versions, account = self._bookmark_scope(artist_id)
            if account is None:
                return ItemResult("failed", {"message": "无法核实当前账号，已保留最近收藏缓存"})
            remaining, confirmed, unknown, superseded = set(versions), 0, 0, 0
            for data in self.client.get_all_artist_illusts(remote_id):
                remote_work_id = str(data["illust_id"])
                if remote_work_id not in remaining:
                    continue
                remaining.remove(remote_work_id)
                if versions[remote_work_id] is None:
                    superseded += 1
                    continue
                if type(data.get("is_bookmarked")) is not bool:
                    unknown += 1
                    continue
                with self.database.read() as session:
                    work_id = session.scalar(select(Work.id).where(Work.pixiv_illust_id == remote_work_id))
                if self._apply_bookmark(work_id, data, versions[remote_work_id], account):
                    confirmed += 1
                else:
                    superseded += 1
            unknown += len(remaining)
            detail = {"confirmed_works": confirmed, "unconfirmed_works": unknown,
                      "superseded_works": superseded}
            if unknown:
                return ItemResult("failed", {**detail, "message": f"已核实 {confirmed} 件；{unknown} 件未取得明确的主站状态，已保留缓存"})
            return ItemResult("succeeded" if confirmed else "skipped", detail)
        return ItemResult()
