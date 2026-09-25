"""Archive task handlers: discover explicit assets, then execute the frozen manifest."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .download import AssetDownloader
from .media import InvalidMedia, Publisher, SuppressedWork, build_gif, build_thumbnail, contained_path, verify_media, verify_ugoira_pair
from .models import (
    ArchiveIntent, Artist, Asset, Derivative, LocalPreference, RemoteBookmark,
    TrackingPolicy, Work, WorkTag, utcnow,
)
from .tasks import ItemResult, ItemSpec, safe_failure


class ArchiveHandler:
    def __init__(self, kind, database, publisher: Publisher, client, downloader=None):
        self.kind, self.database, self.publisher, self.client = kind, database, publisher, client
        self.downloader = downloader or AssetDownloader()

    def _artist_ids(self, parameters, context):
        if "started_artist_ids" in parameters:
            return parameters["started_artist_ids"]
        if "artist_ids" in parameters:
            result = sorted(set(parameters["artist_ids"]))
        else:
            with self.database.read() as session:
                from .models import Operation
                operation = session.get(Operation, context.operation_id)
                modes = ("active",) if operation.source == "automatic" else ("active", "paused")
                result = list(session.scalars(select(Artist.id).join(TrackingPolicy).where(
                    TrackingPolicy.mode.in_(modes)).order_by(Artist.id)))
        context.checkpoint_parameters({**parameters, "started_artist_ids": result})
        return result

    def _save_remote_work(self, artist_id, data):
        remote_id = str(data["illust_id"])
        if not remote_id.isdigit() or data.get("type") not in ("illust", "manga", "ugoira"):
            raise ValueError("无效作品信息")
        count = int(data["page_count"])
        if count <= 0:
            raise ValueError("无效页数")
        with self.database.write() as session:
            intent = session.get(ArchiveIntent, remote_id)
            if intent and intent.mode == "suppressed":
                return None
            if intent is None:
                session.add(ArchiveIntent(pixiv_illust_id=remote_id))
            work = session.scalar(select(Work).where(Work.pixiv_illust_id == remote_id))
            if work is None:
                posted = datetime.fromisoformat(data["posted_at"]) if data.get("posted_at") else None
                work = Work(pixiv_illust_id=remote_id, artist_id=artist_id,
                    title=data["title"], type=data["type"], page_count=count,
                    posted_at=posted.astimezone(timezone.utc).replace(tzinfo=None) if posted and posted.tzinfo else posted,
                    time_basis="utc" if posted and posted.tzinfo else "unknown")
                work.preference = LocalPreference()
                work.bookmark = RemoteBookmark()
                session.add(work)
            elif work.artist_id != artist_id or work.type != data["type"] or work.page_count != count:
                # Never silently discard archived pages when remote metadata changes shape.
                raise InvalidMedia("远端作品的归属、类型或页数发生变化，已有存档保留待核对")
            work.title = data["title"]
            if data.get("caption") is not None:
                work.caption, work.caption_state = data["caption"], "available" if data["caption"] else "empty"
            work.bookmark_count, work.view_count = data.get("bookmark_count"), data.get("view_count")
            if data.get("x_restrict") is not None:
                work.x_restrict, work.restriction_verified = data["x_restrict"], True
            existing_tags = {tag.tag: tag for tag in work.tags}
            remote_tags = {tag for tag in data.get("tags", []) if isinstance(tag, str)}
            work.tags = [existing_tags[tag] if tag in existing_tags else WorkTag(tag=tag) for tag in sorted(remote_tags)]
            if work.bookmark is None:
                work.bookmark = RemoteBookmark()
            if data.get("is_bookmarked") is not None:
                work.bookmark.value, work.bookmark.confirmed_at = bool(data["is_bookmarked"]), utcnow()
            work.source_metadata = {**(work.source_metadata or {}), "urls": data.get("urls", [])}
            session.flush()
            existing_assets = {(asset.role, asset.page_index): asset for asset in work.assets}
            roles = [("ugoira_zip", 0), ("ugoira_timing", 0)] if work.type == "ugoira" else [("image", page) for page in range(count)]
            urls = data.get("urls", [])
            for role, page in roles:
                asset = existing_assets.get((role, page))
                if asset is None:
                    asset = Asset(work_id=work.id, role=role, page_index=page, state="missing")
                    session.add(asset)
                if role == "image" and page < len(urls) and urls[page].get("original"):
                    if asset.source_url and urlsplit(asset.source_url).path != urlsplit(urls[page]["original"]).path:
                        asset.state, asset.error = "source_changed", "远端原件地址已改变，旧文件保留至补下载成功"
                    asset.source_url = urls[page]["original"]
            return work.id

    def plan(self, parameters, context):
        specs = []
        if self.kind == "delete":
            # The API already commits suppression immediately; repeating it is idempotent.
            for work_id in sorted(set(parameters["work_ids"])):
                for journal_id in self.publisher.suppress(work_id):
                    specs.append(ItemSpec(f"delete-{journal_id}", "delete", {"journal_id": journal_id}, "deleting", "files"))
            return specs
        if self.kind in ("metadata", "bookmarks"):
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
                    for data in self.client.get_all_artist_illusts(remote_id):
                        self._save_remote_work(artist_id, data)
                    with self.database.write() as session:
                        session.get(Artist, artist_id).last_checked_at = utcnow()
                except Exception as exc:
                    specs.append(ItemSpec(f"artist-error-{artist_id}", "error", safe_failure(exc), "checking", "artists"))
                context.report("checking", index + 1, len(ids), "artists", "已检查画师")
            work_ids = self._work_ids({"artist_ids": ids})
        else:
            work_ids = self._work_ids(parameters)
        context.report("inventory", 0, len(work_ids), "works", "正在清点本地文件")
        for index, work_id in enumerate(work_ids):
            try:
                specs.extend(self._plan_work(work_id, parameters, context))
            except SuppressedWork:
                specs.append(ItemSpec(f"suppressed-{work_id}", "skip", {"message": "作品已删除，旧任务不再处理"}, "checking", "works"))
            except Exception as exc:
                specs.append(ItemSpec(f"work-error-{work_id}", "error", safe_failure(exc), "checking", "works"))
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

    def _inspect_asset(self, asset, force=False):
        if asset.state == "source_changed" or asset.state == "mismatch" and not force:
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
                stored.state, stored.error, stored.verified_at = "verified", None, utcnow()
                stored.byte_size, stored.sha256 = verified.byte_size, verified.sha256
                stored.width, stored.height = verified.width, verified.height
                stored.file_mtime_ns, stored.file_identity = stat.st_mtime_ns, stat.st_ino
            return True
        except (InvalidMedia, OSError, ValueError) as exc:
            with self.database.write() as session:
                stored = session.get(Asset, asset.id)
                stored.state = "invalid" if asset.relative_path else "missing"
                stored.error = getattr(exc, "user_message", "原件缺失或无法读取")
            return False

    def _plan_work(self, work_id, parameters, context):
        with self.database.read() as session:
            work = session.scalar(select(Work).options(selectinload(Work.assets), selectinload(Work.artist)).where(Work.id == work_id))
            intent = session.get(ArchiveIntent, work.pixiv_illust_id)
            if intent is None or intent.mode != "keep":
                raise SuppressedWork()
            revision = intent.revision
            directory = work.artist.directory or f"artist_{work.artist.pixiv_user_id}"
            assets = list(work.assets)
        specs, required = [], []
        defer_originals = work.source_metadata.get("original_backfill_requires_selection") and self.kind != "backfill"
        local_only = self.kind in ("verify", "thumbnails")
        if self.kind != "thumbnails" and not defer_originals:
            context.detail(f"正在核验 {work.title} · {len(assets)} 项原件")
            missing = [asset for asset in assets if not self._inspect_asset(asset, force=self.kind == "verify")]
            metadata = None
            if missing and not local_only:
                if work.type == "ugoira":
                    metadata = self.client.get_ugoira_metadata(work.pixiv_illust_id)
                elif any(not asset.source_url for asset in missing):
                    self._save_remote_work(work.artist_id, self.client.get_illust_detail(work.pixiv_illust_id))
                    with self.database.read() as session:
                        missing = [session.get(Asset, asset.id) for asset in missing]
            for asset in missing:
                key = f"asset-{asset.id}"
                required.append(key)
                if local_only:
                    specs.append(ItemSpec(key, "error", {"message": "原件缺失或损坏，可选择补下载"}, "verifying", "pages"))
                    continue
                target = {"asset_id": asset.id, "revision": revision}
                if asset.role == "ugoira_timing":
                    target.update({"frames": metadata["frames"], "path": asset.relative_path or f"{directory}/{work.pixiv_illust_id}.frames.json"})
                    kind = "timing"
                else:
                    url = (metadata.get("zip_urls", {}).get("original") or metadata["zip_urls"]["medium"]) if asset.role == "ugoira_zip" else asset.source_url
                    if not url:
                        raise InvalidMedia("缺少此页原件的下载地址")
                    extension = ".zip" if asset.role == "ugoira_zip" else PurePosixPath(urlsplit(url).path).suffix.lower()
                    if extension not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip"):
                        raise InvalidMedia("原件的文件类型无法识别")
                    target.update({"url": url, "path": asset.relative_path or f"{directory}/{work.pixiv_illust_id}_p{asset.page_index}{extension}"})
                    kind = "download"
                specs.append(ItemSpec(key, kind, target, "downloading", "assets"))
        if self.kind == "thumbnails" and work.type != "ugoira":
            for asset in assets:
                if asset.page_index == 0 and asset.role == "image":
                    self._inspect_asset(asset)
        if work.type == "ugoira":
            if not defer_originals and self.kind != "thumbnails":
                if required and not local_only:
                    key = f"pair-{work_id}"
                    specs.append(ItemSpec(key, "ugoira_pair", {"work_id": work_id, "revision": revision},
                        "verifying", "works", tuple(required)))
                    required = [key]
                elif not required:
                    self._verify_pair(work_id)
            with self.database.read() as session:
                preview = session.scalar(select(Derivative).where(Derivative.work_id == work_id, Derivative.kind == "gif", Derivative.spec_key == "v1"))
            usable = self._derivative_usable(preview, "gif")
            if not usable and not defer_originals and not local_only:
                preview_id = self._ensure_derivative(work_id, "gif")
                key = f"preview-{preview_id}"
                specs.append(ItemSpec(key, "gif", {"work_id": work_id, "record_id": preview_id,
                    "path": f"{directory}/{work.pixiv_illust_id}.gif", "revision": revision},
                    "converting", "previews", tuple(required)))
                required = [key]
            elif not usable:
                return specs
        with self.database.read() as session:
            thumbnail = session.scalar(select(Derivative).where(Derivative.work_id == work_id,
                Derivative.kind == "thumbnail", Derivative.page_index == 0, Derivative.spec_key == "v1"))
        if not self._derivative_usable(thumbnail, "thumbnail") and self.kind != "verify":
            if work.type != "ugoira":
                # Later missing pages must not prevent a usable first-page card.
                first_asset = next((asset for asset in assets if asset.role == "image" and asset.page_index == 0), None)
                required = [key for key in required if first_asset and key == f"asset-{first_asset.id}"]
            thumbnail_id = self._ensure_derivative(work_id, "thumbnail")
            specs.append(ItemSpec(f"thumbnail-{thumbnail_id}", "thumbnail", {
                "work_id": work_id, "record_id": thumbnail_id, "revision": revision,
                "path": f".thumbnails/{work.pixiv_illust_id}_p0_v1.webp"}, "thumbnails", "previews", tuple(required)))
        return specs

    def _verify_pair(self, work_id):
        with self.database.read() as session:
            assets = {asset.role: asset for asset in session.scalars(select(Asset).where(Asset.work_id == work_id))}
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
            raise
        with self.database.write() as session:
            work = session.get(Work, work_id)
            work.source_metadata = {**work.source_metadata, "original_backfill_requires_selection": False}

    def _source_version(self, record):
        with self.database.read() as session:
            work = session.get(Work, record.work_id)
            if record.kind == "gif":
                inputs = list(session.scalars(select(Asset).where(Asset.work_id == work.id,
                    Asset.role.in_(("ugoira_zip", "ugoira_timing"))).order_by(Asset.role)))
                expected = 2
            elif work.type == "ugoira":
                inputs = list(session.scalars(select(Derivative).where(Derivative.work_id == work.id,
                    Derivative.kind == "gif", Derivative.spec_key == "v1")))
                expected = 1
            else:
                inputs = list(session.scalars(select(Asset).where(Asset.work_id == work.id,
                    Asset.role == "image", Asset.page_index == 0)))
                expected = 1
            if len(inputs) != expected or any(not source.sha256 or source.state != "verified" for source in inputs):
                return None
            return ":".join(source.sha256 for source in inputs)

    def _ensure_derivative(self, work_id, kind):
        with self.database.write() as session:
            record = session.scalar(select(Derivative).where(Derivative.work_id == work_id,
                Derivative.kind == kind, Derivative.page_index == 0, Derivative.spec_key == "v1"))
            if record is None:
                record = Derivative(work_id=work_id, kind=kind)
                session.add(record)
                session.flush()
            return record.id

    def _derivative_usable(self, record, role):
        if not record or not record.relative_path:
            return False
        source_version = self._source_version(record)
        if record.source_version and source_version and source_version != record.source_version:
            with self.database.write() as session:
                stored = session.get(Derivative, record.id)
                stored.state, stored.error = "outdated", "输入文件已改变，等待重建预览"
            return False
        try:
            path = contained_path(self.publisher.root, record.relative_path)
            stat = path.stat()
            if (record.state == "verified" and stat.st_size > 0 and
                    (stat.st_size, stat.st_mtime_ns, stat.st_ino) == (record.byte_size, record.file_mtime_ns, record.file_identity)):
                return True
            verified = verify_media(path, role)
            with self.database.write() as session:
                stored = session.get(Derivative, record.id)
                stored.state, stored.error = "verified", None
                stored.width, stored.height = verified.width, verified.height
                stored.byte_size, stored.sha256, stored.verified_at = verified.byte_size, verified.sha256, utcnow()
                stored.file_mtime_ns, stored.file_identity = stat.st_mtime_ns, stat.st_ino
            return True
        except (InvalidMedia, OSError, ValueError):
            with self.database.write() as session:
                stored = session.get(Derivative, record.id)
                stored.state, stored.error = "invalid", "预览文件缺失或损坏，可以重建"
            return False

    def execute(self, item, context):
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
                intent = session.get(ArchiveIntent, work.pixiv_illust_id)
                if intent is None or intent.mode != "keep" or intent.revision != item.target["revision"]:
                    return ItemResult("skipped", {"message": "保存决定已改变"})
            self._verify_pair(item.target["work_id"])
            return ItemResult()
        target = item.target
        derivative = item.kind in ("gif", "thumbnail")
        record_id = target["record_id"] if derivative else target["asset_id"]
        with self.database.read() as session:
            record = session.get(Derivative if derivative else Asset, record_id)
            work = session.get(Work, record.work_id)
            intent = session.get(ArchiveIntent, work.pixiv_illust_id)
            if intent is None or intent.mode != "keep" or intent.revision != target["revision"]:
                return ItemResult("skipped", {"message": "保存决定已改变，此项不再执行"})
        if not derivative and self._inspect_asset(record):
            return ItemResult(detail={"message": "原件已核验，无需再次下载"})
        if derivative and self._derivative_usable(record, item.kind):
            return ItemResult(detail={"message": "预览已核验，无需再次生成"})
        relative, staged = self.publisher.staging()
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
                        assets = {asset.role: asset for asset in session.scalars(select(Asset).where(Asset.work_id == work.id))}
                        if any(role not in assets or assets[role].state != "verified" for role in ("ugoira_zip", "ugoira_timing")):
                            raise InvalidMedia("缺少已核验的动图原包或播放时序")
                        source = contained_path(self.publisher.root, assets["ugoira_zip"].relative_path)
                        timing = contained_path(self.publisher.root, assets["ugoira_timing"].relative_path)
                    elif work.type == "ugoira":
                        preview = session.scalar(select(Derivative).where(Derivative.work_id == work.id, Derivative.kind == "gif", Derivative.spec_key == "v1"))
                        if preview is None or not preview.relative_path:
                            raise InvalidMedia("缺少动图预览，无法生成缩略图")
                        source = contained_path(self.publisher.root, preview.relative_path)
                    else:
                        original = session.scalar(select(Asset).where(Asset.work_id == work.id, Asset.role == "image", Asset.page_index == 0))
                        if original is None or not original.relative_path:
                            raise InvalidMedia("缺少第一张原件，无法生成缩略图")
                        source = contained_path(self.publisher.root, original.relative_path)
                verified = build_gif(source, timing, staged) if item.kind == "gif" else build_thumbnail(source, staged)
            journal_id = self.publisher.prepare(record_id, relative, target["path"], verified,
                derivative=derivative, item_id=context.item_id, expected_revision=target["revision"],
                source_version=self._source_version(record) if derivative else None)
            state = self.publisher.publish(journal_id)
            return ItemResult("succeeded" if state == "committed" else "skipped")
        except SuppressedWork:
            return ItemResult("skipped", {"message": "作品已删除，旧任务不再发布文件"})
        except Exception as exc:
            with self.database.write() as session:
                stored = session.get(Derivative if derivative else Asset, record_id)
                if stored is not None:
                    stored.state, stored.error = "failed", safe_failure(exc)["message"]
            raise

    def _execute_metadata(self, item):
        if item.kind == "metadata_work":
            with self.database.read() as session:
                work = session.get(Work, item.target["work_id"])
                intent = session.get(ArchiveIntent, work.pixiv_illust_id) if work else None
                if work is None or intent is None or intent.mode == "suppressed":
                    return ItemResult("skipped", {"message": "作品已删除，不再刷新信息"})
                artist_id, remote_id = work.artist_id, work.pixiv_illust_id
            result = self._save_remote_work(artist_id, self.client.get_illust_detail(remote_id))
            return ItemResult("succeeded" if result else "skipped")
        artist_id = item.target["artist_id"]
        with self.database.read() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                return ItemResult("skipped", {"message": "画师记录不存在"})
            remote_id = artist.pixiv_user_id
        if item.kind == "metadata_artist":
            data = self.client.get_artist_detail(remote_id)
            with self.database.write() as session:
                artist = session.get(Artist, artist_id)
                artist.name, artist.account, artist.avatar_url = data["name"], data.get("account"), data.get("avatar_url")
        else:
            for data in self.client.get_all_artist_illusts(remote_id):
                if data.get("is_bookmarked") is None:
                    continue
                with self.database.write() as session:
                    work = session.scalar(select(Work).where(Work.pixiv_illust_id == str(data["illust_id"])))
                    if work is not None:
                        if work.bookmark is None:
                            work.bookmark = RemoteBookmark()
                        work.bookmark.value, work.bookmark.confirmed_at = bool(data["is_bookmarked"]), utcnow()
        return ItemResult()
