"""File validation and recoverable publication. Originals are never conversion scratch space."""
from __future__ import annotations

import hashlib
import json
import math
import os
import warnings
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from PIL import Image, ImageOps
from sqlalchemy import select

from .database import ArchiveDatabase
from .models import ArchiveIntent, Asset, Derivative, PublicationJournal, Work, WorkRevision, RevisionDerivative, utcnow
from .revisions import refresh_for_record, latest_revision, refresh_revision, asset_entries


class InvalidMedia(ValueError):
    @property
    def user_message(self):
        return str(self)


class SuppressedWork(RuntimeError):
    pass


def contained_path(root: Path, relative: str) -> Path:
    """Reject absolute, traversal, Windows alternate-stream and symlink escapes."""
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise ValueError("无效的图库相对路径")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts) or PurePosixPath(relative).is_absolute():
        raise ValueError("无效的图库相对路径")
    root = Path(root).resolve()
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("路径超出图库")
    return path


@dataclass(frozen=True)
class Verification:
    byte_size: int
    sha256: str
    width: int | None = None
    height: int | None = None


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_ugoira_pair(zip_path: Path, timing_path: Path):
    """Check correspondence separately from the expensive, cached frame decoding."""
    frames = read_timing(timing_path)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = archive.namelist()
        if len(names) != len(frames) or set(names) != {frame["file"] for frame in frames}:
            raise InvalidMedia("动图原包与播放时序不匹配，现有预览仍保留")
    except (OSError, zipfile.BadZipFile) as exc:
        raise InvalidMedia("动图原包无法读取，现有预览仍保留") from exc


def read_timing(path: Path) -> list[dict]:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise InvalidMedia("动图时序文件过大")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        frames = data["frames"]
        names = set()
        if not isinstance(frames, list) or not frames:
            raise ValueError()
        for frame in frames:
            name, delay = frame["file"], frame["delay"]
            if (not isinstance(name, str) or not name or "/" in name or "\\" in name
                    or ":" in name or name in (".", "..") or name in names
                    or type(delay) is not int or delay <= 0):
                raise ValueError()
            names.add(name)
        return frames
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise InvalidMedia("动图缺少有效的帧顺序或每帧时长") from exc


def _verify_image(path_or_stream) -> tuple[int, int]:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(path_or_stream) as image:
            image.verify()
        if hasattr(path_or_stream, "seek"):
            path_or_stream.seek(0)
        with Image.open(path_or_stream) as image:
            size = image.size
            for index in range(getattr(image, "n_frames", 1)):
                image.seek(index)
                image.load()
    return size


def verify_media(path: Path, role: str, timing: Path | None = None) -> Verification:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise InvalidMedia("文件缺失或为空")
    width = height = None
    try:
        if role in ("image", "gif", "thumbnail"):
            width, height = _verify_image(path)
        elif role == "ugoira_timing":
            read_timing(path)
        elif role == "ugoira_zip":
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if not names or len(names) != len(set(names)):
                    raise InvalidMedia("动图原包为空或含重复文件名")
                if sum(info.file_size for info in infos) > 2 * 1024**3:
                    raise InvalidMedia("动图展开大小超过核验上限，原包已保留")
                if any(info.is_dir() or info.file_size <= 0 or info.file_size > 128 * 1024**2
                       or "/" in info.filename or "\\" in info.filename or ":" in info.filename
                       for info in infos):
                    raise InvalidMedia("动图原包包含无效帧")
                if timing is not None:
                    frames = read_timing(timing)
                    if set(names) != {frame["file"] for frame in frames}:
                        raise InvalidMedia("动图原包与播放时序不匹配")
                sizes = set()
                # Opening each member checks its CRC and decoding; never extract paths.
                for info in infos:
                    with archive.open(info) as stream:
                        sizes.add(_verify_image(stream))
                if len(sizes) != 1:
                    raise InvalidMedia("动图各帧尺寸不一致")
                width, height = sizes.pop()
        else:
            raise ValueError(f"未知的核验类型：{role}")
    except (OSError, SyntaxError, EOFError, zipfile.BadZipFile, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise InvalidMedia("文件损坏或无法完整解码") from exc
    return Verification(path.stat().st_size, file_digest(path), width, height)


def build_gif(zip_path: Path, timing_path: Path, destination: Path, max_pixels=160_000_000, *, checkpoint=None):
    if checkpoint: checkpoint()
    verification = verify_media(zip_path, "ugoira_zip", timing_path)
    frames = read_timing(timing_path)
    # Pillow's GIF writer holds frames; put a hard, explicit bound on this derivative.
    if verification.width * verification.height * len(frames) > max_pixels:
        raise InvalidMedia("此动图超过当前预览转换的内存上限，原包和时序已保留")
    images = []
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for frame in frames:
                if checkpoint: checkpoint()
                with archive.open(frame["file"]) as stream, Image.open(stream) as image:
                    images.append(image.convert("RGB"))
        if checkpoint: checkpoint()
        images[0].save(destination, format="GIF", save_all=True, append_images=images[1:],
                       duration=[frame["delay"] for frame in frames], loop=0, disposal=2,
                       optimize=False)
        return verify_media(destination, "gif")
    finally:
        for image in images:
            image.close()


def build_thumbnail(source: Path, destination: Path, size=(360, 360), *, checkpoint=None):
    if checkpoint: checkpoint()
    with Image.open(source) as image:
        image.seek(0)
        oriented = ImageOps.exif_transpose(image)
        try:
            thumbnail = ImageOps.fit(oriented.convert("RGB"), size, Image.Resampling.LANCZOS)
            try:
                if checkpoint: checkpoint()
                thumbnail.save(destination, format="WEBP", quality=82)
            finally:
                thumbnail.close()
        finally:
            if oriented is not image:
                oriented.close()
    return verify_media(destination, "thumbnail")


CARD_MOTION_SPEC = "card-motion-v1"
CARD_MOTION_MAX_FRAMES = 120
CARD_MOTION_MAX_PIXELS = CARD_MOTION_MAX_FRAMES * 360 * 360


def build_card_motion(source: Path, destination: Path, *, checkpoint=None,
                      max_input_frames=20_000, max_input_pixels=1_000_000_000,
                      max_duration_ms=3_600_000):
    """Bound the card cache, never alter GIF bytes or the original ZIP/timing.

    Read the GIF sequentially twice: first for bounded timing, then for selected
    frames. Only reduced RGBA frames survive a seek. Missing/zero GIF delays use
    100 ms; longer input retains its full duration with fewer preview frames.
    """
    checkpoint = checkpoint or (lambda: None)
    images, durations = [], []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if image.format != "GIF":
                    raise InvalidMedia("卡片动图需要有效的本地 GIF，原文件已保留")
                source_pixels = image.width * image.height
                timing, total = [], 0
                # Avoid n_frames: its unbounded internal scan has no stop boundary.
                for index in range(max_input_frames + 1):
                    checkpoint()
                    try:
                        image.seek(index)
                    except EOFError:
                        break
                    if index >= max_input_frames or source_pixels * (index + 1) > max_input_pixels:
                        raise InvalidMedia("此动图超过卡片预览的解码上限，原文件已保留")
                    delay = image.info.get("duration", 100)
                    delay = delay if type(delay) is int and delay > 0 else 100
                    total += delay
                    if total > max_duration_ms:
                        raise InvalidMedia("此动图超过卡片预览的播放时长上限，原文件已保留")
                    timing.append(delay)
                if not timing:
                    raise InvalidMedia("本地 GIF 不含有效帧，原文件已保留")
            period = max(100, math.ceil(total / CARD_MOTION_MAX_FRAMES))
            with Image.open(source) as image:
                pending = 0
                for index, delay in enumerate(timing):
                    checkpoint()
                    image.seek(index)
                    if index == len(timing) - 1 and len(images) == 1 and pending:
                        # Very short loops still need two visible states. Their
                        # original timing takes precedence over the ~10 fps goal.
                        durations.append(pending)
                        pending = 0
                    if pending == 0:
                        # Pillow owns one original-size composited frame; discard
                        # the temporary conversion as soon as it has been reduced.
                        with image.convert("RGBA") as frame:
                            frame.thumbnail((360, 360), Image.Resampling.LANCZOS)
                            images.append(frame.copy())
                    pending += delay
                    if pending >= period:
                        durations.append(pending)
                        pending = 0
                if pending:
                    if len(images) > 2 and pending < 100:
                        images.pop().close()
                        durations[-1] += pending
                    else:
                        durations.append(pending)
                if len(images) > CARD_MOTION_MAX_FRAMES or sum(frame.width * frame.height for frame in images) > CARD_MOTION_MAX_PIXELS:
                    raise InvalidMedia("卡片预览超过输出上限，原文件已保留")
                checkpoint()
                images[0].save(destination, format="WEBP", save_all=True, append_images=images[1:],
                               duration=durations, loop=0, quality=72, method=3)
        checkpoint()
        return verify_media(destination, "thumbnail")
    except (OSError, SyntaxError, EOFError, Image.DecompressionBombWarning,
            Image.DecompressionBombError) as exc:
        raise InvalidMedia("本地 GIF 损坏或无法生成卡片预览，原文件已保留") from exc
    finally:
        for frame in images:
            frame.close()


class Publisher:
    """A durable intent precedes file moves; replay compares bytes instead of guessing.

    All writers share ArchiveDatabase.write, so a suppression committed before
    publication wins. After publication suppression hides the work immediately.
    Interrupted/obsolete staging files are retained privately for inspection.
    """

    def __init__(self, database: ArchiveDatabase, root: Path):
        self.database, self.root = database, Path(root).resolve()

    def staging(self) -> tuple[str, Path]:
        relative = f".staging/{uuid4().hex}.part"
        path = contained_path(self.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        return relative, path

    def prepare(self, record_id: str, staging: str, target: str,
                verified: Verification, *, derivative=False, item_id=None,
                expected_revision: int, source_version=None, requested_url=None, expected_source_fingerprint=None):
        staged = contained_path(self.root, staging)
        contained_path(self.root, target)
        if not staging.startswith(".staging/") or target.startswith((".staging/", ".recovery/")):
            raise ValueError("发布路径不合法")
        if staged.stat().st_size != verified.byte_size or file_digest(staged) != verified.sha256:
            raise InvalidMedia("待发布文件在核验后发生了变化")
        # Flush bytes before persisting the publication intent.
        with staged.open("rb+") as stream:
            os.fsync(stream.fileno())
        shared_target = None
        if not derivative:
            with self.database.read() as session:
                candidate_record = session.get(Asset, record_id)
                if candidate_record and candidate_record.revision_id:
                    shared_target = session.scalar(select(Asset.relative_path).where(
                        Asset.work_id == candidate_record.work_id, Asset.id != record_id,
                        Asset.role == candidate_record.role, Asset.state == "verified",
                        Asset.sha256 == verified.sha256, Asset.relative_path.is_not(None)).limit(1))
            if shared_target:
                try:
                    shared_path = contained_path(self.root, shared_target)
                    if shared_path.stat().st_size != verified.byte_size or file_digest(shared_path) != verified.sha256:
                        shared_target = None
                except OSError:
                    shared_target = None
        with self.database.write() as session:
            record = session.get(Derivative if derivative else Asset, record_id)
            if record is None:
                raise KeyError(record_id)
            work = session.get(Work, record.work_id)
            self._require_intent(session, work.pixiv_illust_id, expected_revision)
            # Reuse independently verified bytes inside this work. Comparison uses
            # SHA-256, including same-size replacements and changed source paths.
            if shared_target:
                target = shared_target
            journal = PublicationJournal(
                operation_item_id=item_id, asset_id=None if derivative else record_id,
                pixiv_illust_id=work.pixiv_illust_id, intent_revision=expected_revision,
                action="publish", staging_path=staging, target_path=target,
                verification={**asdict(verified), "record_id": record_id,
                              "derivative": derivative, "source_version": source_version, "requested_url": requested_url, "expected_source_fingerprint": expected_source_fingerprint,
                              "shared_target": bool(shared_target),
                              "backup": f".recovery/{uuid4().hex}.previous"},
            )
            session.add(journal)
            session.flush()
            return journal.id

    @staticmethod
    def _require_intent(session, remote_id, revision):
        intent = session.get(ArchiveIntent, remote_id)
        if intent is None or intent.mode != "keep" or intent.revision != revision:
            raise SuppressedWork("本地保存决定已改变，此旧任务不再发布文件")

    def publish(self, journal_id: str, *, after_move=None):
        try:
            # Hashing may read gigabytes; perform it before acquiring the writer.
            with self.database.read() as session:
                preflight = session.get(PublicationJournal, journal_id)
                if preflight.step in ("committed", "obsolete"):
                    return preflight.step
                paths = [contained_path(self.root, preflight.target_path), contained_path(self.root, preflight.staging_path)]
            fingerprints, digests = {}, {}
            for path in paths:
                if path.is_file():
                    stat = path.stat()
                    fingerprints[path] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
                    digests[path] = file_digest(path)
                else:
                    fingerprints[path] = None
                    digests[path] = None
            with self.database.write() as session:
                journal = session.get(PublicationJournal, journal_id)
                if journal.step in ("committed", "obsolete"):
                    return journal.step
                self._require_intent(session, journal.pixiv_illust_id, journal.intent_revision)
                info = journal.verification
                record = session.get(Derivative if info["derivative"] else Asset, info["record_id"])
                requested_url = info.get("requested_url")
                owner = session.get(Work, record.work_id) if record else None
                if info["derivative"] and record and record.spec_key == CARD_MOTION_SPEC:
                    source = session.scalar(select(Derivative).join(RevisionDerivative,
                        RevisionDerivative.derivative_id == Derivative.id).where(RevisionDerivative.revision_id == record.revision_id,
                        Derivative.kind == "gif", Derivative.spec_key == "v1")) if record.revision_id else session.scalar(select(Derivative).where(
                        Derivative.work_id == record.work_id, Derivative.kind == "gif", Derivative.spec_key == "v1"))
                    if source is None or source.state != "verified" or not source.sha256 or source.sha256 != info.get("source_version"):
                        journal.step, journal.error = "obsolete", "本地 GIF 已改变，旧卡片预览保留在暂存区"
                        return "obsolete"
                pending_source = (owner.source_metadata or {}).get("source_updates", {}).get(record.id, {}) if owner and not info["derivative"] else {}
                changed_fingerprint = pending_source.get("expected_fingerprint") and pending_source["expected_fingerprint"] != info.get("expected_source_fingerprint")
                if not info["derivative"] and record and (requested_url and record.source_url and requested_url != record.source_url or changed_fingerprint):
                    journal.step, journal.error = "obsolete", "来源已更新，旧回应保留在暂存区"
                    return "obsolete"
                target = contained_path(self.root, journal.target_path)
                staged = contained_path(self.root, journal.staging_path)
                info = journal.verification
                for path in (target, staged):
                    stat = path.stat() if path.is_file() else None
                    current = (stat.st_size, stat.st_mtime_ns, stat.st_ino) if stat else None
                    if current != fingerprints[path]:
                        raise InvalidMedia("发布核验后文件发生变化，停止覆盖")
                if digests[target] != info["sha256"]:
                    if info.get("shared_target"):
                        raise InvalidMedia("复用原件在核验后发生变化，已有版本保留，等待重新核验")
                    if digests[staged] != info["sha256"]:
                        raise InvalidMedia("中断恢复时找不到已核验的文件")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        backup = contained_path(self.root, info["backup"])
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        if backup.exists():
                            raise InvalidMedia("恢复位置已有文件，停止覆盖并等待检查")
                        os.replace(target, backup)
                    os.replace(staged, target)
                    if after_move is not None:
                        after_move()
                record = session.get(Derivative if info["derivative"] else Asset, info["record_id"])
                if record is None:
                    raise InvalidMedia("待发布的资产记录已不存在")
                record.relative_path, record.state, record.error = journal.target_path, "verified", None
                record.width, record.height = info["width"], info["height"]
                record.byte_size, record.sha256 = info["byte_size"], info["sha256"]
                record.verified_at = utcnow()
                stat = target.stat()
                record.file_mtime_ns, record.file_identity = stat.st_mtime_ns, stat.st_ino
                if info["derivative"]:
                    record.source_version = info.get("source_version")
                if not info["derivative"] and requested_url:
                    work = session.get(Work, record.work_id)
                    updates = dict((work.source_metadata or {}).get("source_updates", {}))
                    if updates.get(record.id, {}).get("requested_url") == requested_url and (not updates[record.id].get("expected_fingerprint") or updates[record.id]["expected_fingerprint"] == info.get("expected_source_fingerprint")):
                        updates.pop(record.id)
                        work.source_metadata = {**work.source_metadata, "source_updates": updates}
                refresh_for_record(session, record)
                revision = session.get(WorkRevision, record.revision_id) if record.revision_id else None
                if revision and not info["derivative"] and revision.state == "complete" and revision.change_kind in ("compare", "source"):
                    current = asset_entries(session, revision)
                    previous = session.scalar(select(WorkRevision).where(WorkRevision.work_id == revision.work_id,
                        WorkRevision.ordinal < revision.ordinal, WorkRevision.state != "redundant").order_by(WorkRevision.ordinal.desc()).limit(1))
                    prior = asset_entries(session, previous) if previous else []
                    hashes = lambda entries: [(entry.role, entry.page_index, asset.sha256) for entry, asset in entries]
                    if previous and current and all(
                            asset.state == "verified" and asset.sha256 for entry, asset in current) and hashes(current) == hashes(prior):
                        if revision.source_signature == previous.source_signature:
                            revision.state = "redundant"
                            refresh_revision(session, previous)
                        else:
                            revision.change_kind = "metadata"
                from .catalogue_revision import bump_catalogue_revision
                bump_catalogue_revision(session)
                journal.step, journal.error, journal.operation_item_id = "committed", None, None
                return journal.step
        except SuppressedWork:
            with self.database.write() as session:
                journal = session.get(PublicationJournal, journal_id)
                journal.step, journal.error = "obsolete", "本地保存决定已改变"
            return "obsolete"
        except Exception as exc:
            with self.database.write() as session:
                session.get(PublicationJournal, journal_id).error = type(exc).__name__
            raise

    def recover(self) -> list[dict]:
        with self.database.read() as session:
            journals = list(session.scalars(select(PublicationJournal).where(PublicationJournal.step == "prepared")))
        results = []
        for journal in journals:
            try:
                state = self.publish(journal.id) if journal.action == "publish" else self.finish_deletion(journal.id)
                results.append({"id": journal.id, "state": state})
            except Exception as exc:
                results.append({"id": journal.id, "state": "failed", "error": type(exc).__name__})
        return results

    def suppress(self, work_id: int) -> list[str]:
        """Persist the user's deletion decision before queueing any physical removal."""
        with self.database.write() as session:
            work = session.get(Work, work_id)
            if work is None:
                raise KeyError(work_id)
            intent = session.get(ArchiveIntent, work.pixiv_illust_id)
            if intent is None:
                intent = ArchiveIntent(pixiv_illust_id=work.pixiv_illust_id, mode="suppressed", revision=1)
                session.add(intent)
            elif intent.mode != "suppressed":
                intent.mode, intent.revision, intent.updated_at = "suppressed", intent.revision + 1, utcnow()
            paths = {record.relative_path for record in [*work.assets, *work.derivatives] if record.relative_path}
            # A crash may have moved a file before its asset path was committed.
            for publication in session.scalars(select(PublicationJournal).where(
                    PublicationJournal.pixiv_illust_id == work.pixiv_illust_id,
                    PublicationJournal.action == "publish")):
                paths.add(publication.target_path)
                if publication.staging_path:
                    paths.add(publication.staging_path)
                if publication.verification.get("backup"):
                    paths.add(publication.verification["backup"])
            existing = {entry.staging_path: entry.id for entry in session.scalars(select(PublicationJournal).where(
                PublicationJournal.pixiv_illust_id == work.pixiv_illust_id, PublicationJournal.action == "delete",
                PublicationJournal.intent_revision == intent.revision))}
            ids = []
            for relative in sorted(paths):
                contained_path(self.root, relative)
                if relative in existing:
                    ids.append(existing[relative])
                    continue
                journal = PublicationJournal(pixiv_illust_id=work.pixiv_illust_id,
                    intent_revision=intent.revision, action="delete", staging_path=relative,
                    target_path=f".recovery/{uuid4().hex}.deleted")
                session.add(journal)
                session.flush()
                ids.append(journal.id)
            return ids

    def finish_deletion(self, journal_id: str, *, after_move=None):
        try:
            with self.database.write() as session:
                journal = session.get(PublicationJournal, journal_id)
                if journal.action != "delete":
                    raise ValueError("不是删除记录")
                if journal.step in ("committed", "obsolete"):
                    return journal.step
                intent = session.get(ArchiveIntent, journal.pixiv_illust_id)
                if intent is None or intent.mode != "suppressed" or intent.revision != journal.intent_revision:
                    journal.step = "obsolete"
                    return journal.step
                original = contained_path(self.root, journal.staging_path)
                quarantine = contained_path(self.root, journal.target_path)
                if original.exists():
                    if not original.is_file() or quarantine.exists():
                        raise InvalidMedia("删除目标或恢复位置发生变化，请检查")
                    quarantine.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(original, quarantine)
                    if after_move:
                        after_move()
                if quarantine.exists():
                    quarantine.unlink()
                journal.step, journal.error = "committed", None
                return journal.step
        except Exception as exc:
            with self.database.write() as session:
                session.get(PublicationJournal, journal_id).error = type(exc).__name__
            raise
