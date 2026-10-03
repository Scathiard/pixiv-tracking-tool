"""Manual reconciliation of existing works in one durable operation.

Planning reads catalogue records only. File decoding and repair planning run on
the local lane; the check lane obtains one reusable metadata/bookmark snapshot.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlsplit
import zipfile

from sqlalchemy import select

from .archive import ArchiveHandler, WorkMetadataConflict
from .catalogue_revision import bump_catalogue_revision
from .media import CARD_MOTION_SPEC, InvalidMedia, contained_path
from .models import ArchiveIntent, Artist, Asset, Derivative, OperationItem, Work
from .source_status import classify_failure
from .tasks import ItemResult, ItemSpec, safe_failure
from .revisions import (ensure_legacy_revision, latest_revision, bind_asset, project_revision, revision_directory, revision_assets, revision_derivatives)


class ReconcileHandler(ArchiveHandler):
    def __init__(self, *args, compare_content=False, **kwargs):
        self.compare_content = compare_content
        super().__init__("reconcile", *args, **kwargs)

    def plan(self, parameters, context):
        ids = parameters.get("started_work_ids")
        if ids is None:
            ids = self._work_ids(parameters)
            context.checkpoint_parameters({**parameters, "started_work_ids": ids})
        specs = []
        artist_ids = set()
        context.report("inventory", 0, len(ids), "works", "正在整理已有作品清单")
        for index, work_id in enumerate(ids):
            if index and index % 25 == 0:
                context.report("inventory", index, len(ids), "works", "已整理作品清单")
            context.checkpoint()
            with self.database.write() as session:
                work = session.get(Work, work_id)
                intent = session.get(ArchiveIntent, work.pixiv_illust_id) if work else None
                if not work or not intent or intent.mode != "keep":
                    continue
                artist_ids.add(work.artist_id)
                work_revision = ensure_legacy_revision(session, work)
                work = project_revision(work, work_revision)
                expected = [("ugoira_zip", 0), ("ugoira_timing", 0)] if work.type == "ugoira" else [("image", page) for page in range(work.page_count)]
                known = {(asset.role, asset.page_index) for asset in revision_assets(session, work_revision)}
                for role, page in expected:
                    if (role, page) not in known:
                        urls = (work.source_metadata or {}).get("urls", [])
                        asset = Asset(work_id=work.id, revision_id=work_revision.id, role=role, page_index=page, state="missing",
                            source_url=urls[page].get("original") if role == "image" and page < len(urls) else None)
                        session.add(asset); bind_asset(session, work_revision, asset)
                session.flush()
                target = {"work_id": work.id, "revision": intent.revision, "work_revision_id": work_revision.id}
                checks = []
                for asset in session.scalars(self._assets_statement(session, work_id).order_by(Asset.page_index, Asset.role)):
                    key = f"inspect-asset-{asset.id}"
                    specs.append(ItemSpec(key, "reconcile_asset", {**target, "asset_id": asset.id}, "verifying", "assets"))
                    checks.append(key)
                derivatives = list(session.scalars(self._derivatives_statement(session, work_id)))
                for record in derivatives:
                    key = f"inspect-derivative-{record.id}"
                    specs.append(ItemSpec(key, "reconcile_derivative", {**target, "record_id": record.id}, "verifying", "previews"))
                    checks.append(key)
                if work.type == "ugoira":
                    key = f"inspect-pair-{work_id}"
                    specs.append(ItemSpec(key, "reconcile_pair", target, "verifying", "works", tuple(checks)))
                    checks.append(key)
                snapshot_key = f"snapshot-{work_id}"
                specs.append(ItemSpec(snapshot_key, "reconcile_snapshot", target, "checking", "works"))
                specs.append(ItemSpec(f"prepare-{work_id}", "reconcile_prepare", {**target, "snapshot_key": snapshot_key},
                    "reconciling", "works", tuple([*checks, snapshot_key])))
        # Refresh each in-scope artist exactly once, including stopped artists.
        if "work_ids" not in parameters:
            with self.database.read() as session:
                statement = select(Artist.id)
                if "artist_ids" in parameters:
                    statement = statement.where(Artist.id.in_(parameters["artist_ids"]))
                artist_ids.update(session.scalars(statement))
        if "started_artist_ids" in parameters:
            artist_ids = set(parameters["started_artist_ids"])
        else:
            context.checkpoint_parameters({**parameters, "started_work_ids": ids, "started_artist_ids": sorted(artist_ids)})
        if self.source_status or self.profiles:
            for artist_id in sorted(artist_ids):
                # Keep persisted keys/kinds compatible with earlier manifests.
                # The same response checks the homepage and refreshes its profile.
                specs.append(ItemSpec(f"profile-{artist_id}", "metadata_artist", {"artist_id": artist_id}, "checking", "artists"))
        context.report("inventory", len(ids), len(ids), "works", "已有作品清单已固定")
        return specs

    def _current(self, target):
        with self.database.read() as session:
            work = session.get(Work, target["work_id"])
            intent = session.get(ArchiveIntent, work.pixiv_illust_id) if work else None
            return project_revision(work, latest_revision(session, work.id)) if intent and intent.mode == "keep" and intent.revision == target["revision"] else None

    def execute(self, item, context):
        if item.kind == "metadata_artist" and self.source_status:
            return self._artist_source(item.target["artist_id"], context)
        if not item.kind.startswith("reconcile_"):
            return super().execute(item, context)
        work = self._current(item.target)
        if not work:
            return ItemResult("skipped", {"message": "保存决定已改变，此项不再执行"})
        if item.kind == "reconcile_snapshot":
            return self._snapshot(work, item.target, context)
        if item.kind == "reconcile_prepare":
            return self._prepare(work, item.target, context)
        if item.kind == "reconcile_asset":
            with self.database.read() as session:
                asset = session.get(Asset, item.target["asset_id"])
            valid = bool(asset and self._inspect_asset(asset, force=True))
            # A detected gap is an observation, not a failed repair dependency.
            return ItemResult(detail={"valid": valid, "gap": not valid, "message": "原件核验完成"})
        if item.kind == "reconcile_derivative":
            with self.database.read() as session:
                record = session.get(Derivative, item.target["record_id"])
            valid = self._derivative_usable(record, "gif" if record and record.kind == "gif" else "thumbnail", force=True)
            return ItemResult(detail={"valid": valid, "gap": not valid, "message": "预览核验完成"})
        if item.kind == "reconcile_pair":
            try:
                self._verify_pair(work.id, item.target.get("work_revision_id"))
                valid = True
            except InvalidMedia:
                valid = False
            return ItemResult(detail={"valid": valid, "gap": not valid, "message": "动图原包与时序核验完成"})
        raise ValueError("未知维护步骤")

    def _artist_source(self, artist_id, context):
        from ..request_policy import ShutdownRequested
        context.checkpoint()
        with self.database.read() as session:
            if session.get(Artist, artist_id) is None:
                return ItemResult("skipped", {"message": "画师记录不存在，此项不再执行"})
        try:
            data = self.source_status.fetch_artist(artist_id)
        except ShutdownRequested:
            raise
        except Exception as exc:
            state, reason = classify_failure(exc)
            availability = self.source_status.snapshot("artist", artist_id, self.source_status.account_context())
            concluded = (state in ("unavailable", "private") and availability
                and availability["state"] == state and availability["effective"])
            return ItemResult("skipped" if concluded else "failed", {
                **safe_failure(exc), "message": reason, "availability": availability})
        availability = self.source_status.snapshot("artist", artist_id, self.source_status.account_context())
        if data is None:
            return ItemResult("skipped", {"availability": availability,
                "message": "来源已有较新核对或账号已改变，旧响应不再应用"})
        if self.profiles:
            try:
                self.profiles.save(artist_id, data)
            except ShutdownRequested:
                raise
            except Exception as exc:
                return ItemResult("failed", {**safe_failure(exc), "availability": availability,
                    "message": "画师主页可访问，本地资料或头像刷新未完成，已有内容保留"})
        return ItemResult(detail={"availability": availability, "message": "画师主页来源核对完成"})

    def _snapshot(self, work, target, context):
        from ..request_policy import ShutdownRequested
        account = self._bookmark_account()
        bookmark_version = self.bookmarks.begin_observation(work.id) if account is not None else None
        version = self.source_status.begin("work", work.id, account) if self.source_status else None
        metadata, data = None, None
        try:
            data = self.client.get_illust_detail(work.pixiv_illust_id)
            if str(data.get("illust_id", "")) != work.pixiv_illust_id:
                from .source_status import SourceIdentityMismatch
                raise SourceIdentityMismatch()
            if not self._current(target):
                return ItemResult("skipped", {"message": "作品已删除，不再保存旧响应"})
            failure = None
            if data["type"] == "ugoira":
                try:
                    metadata = self.client.get_ugoira_metadata(work.pixiv_illust_id)
                except ShutdownRequested:
                    raise
                except Exception as exc:
                    failure = safe_failure(exc)
            saved = self._save_remote_work(work.artist_id, data, metadata=metadata,
                expected_work_id=work.id, expected_revision=target["revision"], expected_source_version=version,
                compare_content=self.compare_content)
            if saved is None:
                return ItemResult(detail={"remote_ready": False, "message": "来源已有较新核对，旧响应不再应用"})
            if self.source_status and self._bookmark_account() == account:
                self.source_status.apply_available("work", work.id, data, version, account)
            self._apply_bookmark(work.id, data, bookmark_version, account)
            with self.database.read() as session:
                revision = latest_revision(session, work.id)
            return ItemResult(detail={"metadata": metadata, "failure": failure, "remote_ready": failure is None,
                "work_revision_id": revision.id if revision else None,
                "message": "作品资料与收藏已共用一次主站响应"})
        except ShutdownRequested:
            raise
        except Exception as exc:
            if not self._current(target):
                return ItemResult("skipped", {"message": "保存决定已改变，忽略旧查询结果"})
            if (isinstance(exc, WorkMetadataConflict) and isinstance(data, dict)
                    and str(data.get("illust_id", "")) == work.pixiv_illust_id):
                if self.source_status:
                    if self._bookmark_account() == account:
                        self.source_status.apply_available("work", work.id, data, version, account)
                else:
                    self._record_availability(work.id, "available", None)
            elif self.source_status:
                if self._bookmark_account() == account:
                    self.source_status.apply_failure("work", work.id, exc, version, account)
            elif getattr(exc, "failure_code", None) in ("work_unavailable", "work_private"):
                self._record_availability(work.id, "private" if exc.failure_code == "work_private" else "unavailable",
                    "主站暂时无法获取此作品，已有存档保留")
            source_state, _ = classify_failure(exc)
            return ItemResult(detail={"metadata": None, "failure": {**safe_failure(exc), "source_state": source_state}, "remote_ready": False,
                "message": "主站资料本次未取得，继续核验和重建本地内容"})

    def _prepare(self, work, target, context):
        with self.database.read() as session:
            snapshot = session.scalar(select(OperationItem).where(OperationItem.operation_id == context.operation_id,
                OperationItem.item_key == target["snapshot_key"]))
            facts = snapshot.result or {}
            work_revision = latest_revision(session, work.id)
            assets = list(session.scalars(self._assets_statement(session, work.id)))
            derivatives = list(session.scalars(self._derivatives_statement(session, work.id)))
            artist = session.get(Artist, work.artist_id)
            directory = revision_directory(work, work_revision, artist.directory or f"artist_{artist.pixiv_user_id}")
            # Fetch current metadata: snapshot and inspection may have committed since claim.
            work = project_revision(session.get(Work, work.id), work_revision)
        target = {**target, "work_revision_id": work_revision.id if work_revision else None}
        updates = (work.source_metadata or {}).get("source_updates", {})
        metadata = facts.get("metadata")
        # A pair mismatch does not invalidate an independently decoded ZIP if
        # the current timing snapshot demonstrably matches its frame names.
        # Reuse that original; only the mismatched timing needs replacement.
        if work.type == "ugoira" and metadata:
            self._reuse_verified_zip(assets, updates, metadata, context)
            with self.database.read() as session:
                assets = list(session.scalars(self._assets_statement(session, work.id)))
        missing = [asset for asset in assets if asset.state != "verified" or asset.id in updates]
        specs, repairs = [], {}
        failure = facts.get("failure")
        if failure:
            availability = (work.source_metadata or {}).get("remote_availability", {})
            reviewed_deleted = (availability.get("state") == "deleted" and availability.get("confirmed") is True
                and availability.get("deletion_evidence") is True)
            terminal = (failure.get("source_state") in ("unavailable", "private") or
                failure.get("failure_code") in ("work_unavailable", "work_private", "ugoira_metadata_unavailable") or
                failure.get("failure_code") == "work_deleted" and reviewed_deleted)
            specs.append(ItemSpec(f"source-result-{work.id}", "skip" if terminal else "error", {**failure, "work_id": work.id}, "checking", "works"))
        for asset in missing:
            if not facts.get("remote_ready"):
                specs.append(ItemSpec(f"gap-{asset.id}", "skip", {"work_id": work.id, "asset_id": asset.id,
                    "role": asset.role, "page_index": asset.page_index, "failure_code": (failure or {}).get("failure_code"),
                    "message": (failure or {}).get("message") or "原件缺项本次无法取得，已有内容保留"}, "repairing", "assets"))
                continue
            item_target = {"work_id": work.id, "asset_id": asset.id, "revision": target["revision"], "work_revision_id": target.get("work_revision_id")}
            if asset.role == "ugoira_timing":
                if not metadata:
                    continue
                item_target.update(frames=metadata["frames"], requested_url=asset.source_url,
                    expected_fingerprint=self._timing_fingerprint(metadata), path=asset.relative_path or f"{directory}/{work.pixiv_illust_id}.frames.json")
                kind = "timing"
            else:
                url = asset.source_url
                if not url:
                    specs.append(ItemSpec(f"gap-{asset.id}", "skip", {"work_id": work.id, "message": "主站未提供此原件地址，已有内容保留"}, "repairing", "assets"))
                    continue
                extension = ".zip" if asset.role == "ugoira_zip" else PurePosixPath(urlsplit(url).path).suffix.lower()
                if extension not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip"):
                    raise InvalidMedia("原件的文件类型无法识别")
                item_target.update(url=url, requested_url=url, path=asset.relative_path or f"{directory}/{work.pixiv_illust_id}_p{asset.page_index}{extension}")
                kind = "download"
            key = f"repair-{asset.id}"
            repairs[asset.id] = key
            specs.append(ItemSpec(key, kind, item_target, "downloading", "assets"))
        by_derivative = {(record.kind, record.spec_key): record for record in derivatives}
        required = []
        if work.type == "ugoira":
            original_ready = len(assets) >= 2 and all(asset.state == "verified" or asset.id in repairs for asset in assets)
            gif = by_derivative.get(("gif", "v1"))
            usable_gif = bool(gif and gif.state == "verified")
            if original_ready and (repairs or not usable_gif):
                pair_key = f"repair-pair-{work.id}"
                specs.append(ItemSpec(pair_key, "ugoira_pair", {"work_id": work.id, "revision": target["revision"], "work_revision_id": target.get("work_revision_id")}, "verifying", "works", tuple(repairs.values())))
                gif_id = self._ensure_derivative(work.id, "gif", for_rebuild=bool(repairs))
                key = f"rebuild-{gif_id}"
                specs.append(ItemSpec(key, "gif", {"work_id": work.id, "record_id": gif_id, "revision": target["revision"], "work_revision_id": target.get("work_revision_id"),
                    "path": gif.relative_path if gif and gif.relative_path and gif_id == gif.id else f"{directory}/{work.pixiv_illust_id}.gif", "force": bool(repairs)},
                    "converting", "previews", (pair_key,)))
                required = [key]
            elif not usable_gif:
                self._append(context, specs)
                return ItemResult(detail={"gaps": len(missing), "message": "原包或时序缺失，保留现有文件"})
        else:
            first = next((asset for asset in assets if asset.role == "image" and asset.page_index == 0), None)
            if first is None or first.state != "verified" and first.id not in repairs:
                self._append(context, specs)
                return ItemResult(detail={"gaps": len(missing), "message": "首图尚不能获取，继续保留其他页"})
            required = [repairs[first.id]] if first.id in repairs else []
        for spec_key in (("v1", CARD_MOTION_SPEC) if work.type == "ugoira" else ("v1",)):
            record = by_derivative.get(("thumbnail", spec_key))
            if required or record is None or record.state != "verified":
                record_id = self._ensure_derivative(work.id, "thumbnail", spec_key, for_rebuild=bool(required))
                specs.append(ItemSpec(f"rebuild-{record_id}", "thumbnail", {"work_id": work.id, "record_id": record_id,
                    "revision": target["revision"], "work_revision_id": target.get("work_revision_id"), "force": bool(required),
                    "path": f"{directory}/.thumbnails/{work.pixiv_illust_id}_card_motion_v1.webp" if spec_key == CARD_MOTION_SPEC else f"{directory}/.thumbnails/{work.pixiv_illust_id}_p0_v1.webp"},
                    "thumbnails", "previews", tuple(required)))
        self._append(context, specs)
        self._clear_remote_attention_if_complete(work.id)
        return ItemResult(detail={"gaps": len(missing), "planned_repairs": len(repairs), "message": "实际缺项已进入同一维护清单"})

    def _append(self, context, specs):
        if specs:
            with self.database.write() as session:
                context.queue.materialize_in_transaction(session, context.operation_id, specs)

    def _reuse_verified_zip(self, assets, updates, metadata, context):
        archive = next((asset for asset in assets if asset.role == "ugoira_zip"), None)
        if archive is None or archive.id in updates:
            return
        original = archive
        with self.database.read() as session:
            if archive.state == "missing" and not archive.relative_path:
                # Old conversions can lack source provenance. Reuse a separately
                # decoded legacy ZIP whose frame bytes still match our inventory.
                original = session.scalar(select(Asset).where(Asset.work_id == archive.work_id,
                    Asset.id != archive.id, Asset.role == "ugoira_zip", Asset.source_url.is_(None),
                    Asset.sha256.is_not(None), Asset.relative_path.is_not(None)).limit(1))
            elif archive.state != "mismatch":
                return
            if original is None:
                return
            inspection = session.scalar(select(OperationItem.result).where(
                OperationItem.operation_id == context.operation_id,
                OperationItem.item_key == f"inspect-asset-{original.id}"))
        if not isinstance(inspection, dict) or inspection.get("valid") is not True:
            return
        try:
            path = contained_path(self.publisher.root, original.relative_path)
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns, stat.st_ino) != (original.byte_size, original.file_mtime_ns, original.file_identity):
                return
            with zipfile.ZipFile(path) as source:
                names = source.namelist()
            frames = metadata["frames"]
            if len(names) != len(frames) or set(names) != {frame["file"] for frame in frames}:
                return
        except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile):
            return
        with self.database.write() as session:
            stored = session.get(Asset, archive.id)
            if stored is not None and stored.state in ("mismatch", "missing"):
                for key in ("relative_path", "byte_size", "sha256", "width", "height", "verified_at", "file_mtime_ns", "file_identity"):
                    setattr(stored, key, getattr(original, key))
                stored.state, stored.error = "verified", None
                bump_catalogue_revision(session)
