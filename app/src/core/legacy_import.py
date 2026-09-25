"""Explicit, offline legacy import into a new candidate; never activates it."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3

from .database import ArchiveDatabase
from .models import (
    ArchiveIntent, Artist, Asset, Derivative, LocalPreference, RemoteBookmark,
    TrackingPolicy, Work, WorkTag,
)


@dataclass
class ImportReport:
    source_snapshot_sha256: str
    artists: int = 0
    works: int = 0
    suppressed: int = 0
    assets: int = 0
    warnings: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    unmapped_files: list[str] = field(default_factory=list)
    completed: bool = False

    @property
    def ready_for_review(self) -> bool:
        return self.completed and not self.errors


def _date(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _media_relative(value: str) -> str:
    # Legacy indexes contain literal filenames, not percent-encoded URLs.
    if not value.startswith("/images/"):
        raise ValueError("不是已知的图库相对路径")
    relative = value[len("/images/"):]
    if any(part in {"", ".", ".."} or ":" in part or "\\" in part for part in relative.split("/")):
        raise ValueError("路径超出图库或格式无效")
    return PurePosixPath(relative).as_posix()


def _paths(raw, work_id, report):
    if not raw:
        return []
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError("invalid paths")
    except (ValueError, TypeError):
        if isinstance(raw, str) and raw.startswith("/images/") and "," not in raw:
            values = [raw]
        else:
            report.errors.append({"work": work_id, "code": "ambiguous_legacy_paths"})
            return []
    result = []
    for value in values:
        try:
            result.append(_media_relative(value))
        except ValueError:
            report.errors.append({"work": work_id, "code": "unsafe_media_path"})
    return result


def import_legacy(
    source: Path,
    destination: Path,
    *,
    deleted_manifest: Path | None = None,
    media_root: Path | None = None,
) -> ImportReport:
    """Read a consistent old snapshot and build a separately named candidate.

    The caller must explicitly provide paths. Media are optional, read-only, and
    used only to preserve caption text / locate old files. No network, deletion,
    activation, settings changes, or inference of missing animation timings.
    """
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists() or destination.with_suffix(destination.suffix + ".import.json").exists():
        raise FileExistsError("候选数据库必须是新的独立文件")
    if not source.is_file():
        raise FileNotFoundError(source)
    deleted = []
    if deleted_manifest is not None:
        deleted = json.loads(Path(deleted_manifest).read_text(encoding="utf-8"))
        if not isinstance(deleted, list) or not all(isinstance(v, str) and v.isdigit() for v in deleted):
            raise ValueError("删除阻止清单无效，未开始转换")
    snapshot = sqlite3.connect(":memory:")
    snapshot.row_factory = sqlite3.Row
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as reader:
        reader.backup(snapshot)
    tables = {r[0] for r in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"tracked_artist", "illustration"}.issubset(tables):
        snapshot.close()
        raise ValueError("不是支持的旧数据库")
    report = ImportReport(hashlib.sha256(snapshot.serialize()).hexdigest())
    db = None
    mapped_files = set()
    try:
        db = ArchiveDatabase.create(destination)
        if deleted_manifest is None:
            report.warnings.append({"code": "deleted_manifest_not_provided"})
        with db.write() as session:
            artists = {}
            for row in snapshot.execute("SELECT * FROM tracked_artist ORDER BY id"):
                row = dict(row)
                artist = Artist(
                    id=row["id"], pixiv_user_id=str(row["pixiv_user_id"]), name=row["name"],
                    account=row.get("pixiv_account"), avatar_url=row.get("avatar_url"),
                    last_checked_at=_date(row.get("last_checked_at")),
                )
                if row.get("added_at"):
                    artist.added_at = _date(row["added_at"])
                artist.policy = TrackingPolicy(mode="active" if row.get("is_active", True) else "paused")
                session.add(artist)
                artists[artist.id] = artist
                report.artists += 1
            session.flush()
            tag_rows = {}
            if "illustration_tag" in tables:
                known_work_ids = {row[0] for row in snapshot.execute("SELECT id FROM illustration")}
                for row in snapshot.execute("SELECT illustration_id, tag FROM illustration_tag"):
                    if row[0] not in known_work_ids:
                        report.errors.append({"work": row[0], "code": "orphaned_tag_relation"})
                    tag_rows.setdefault(row[0], set()).add(row[1])
            for row in snapshot.execute("SELECT * FROM illustration ORDER BY id"):
                row = dict(row)
                remote_id = str(row["pixiv_illust_id"])
                if row["artist_id"] not in artists:
                    raise ValueError(f"作品 {row['id']} 缺少画师关系，候选数据已回滚")
                raw_caption = row.get("caption") or ""
                caption, caption_state = raw_caption, "legacy"
                try:
                    caption_data = json.loads(raw_caption)
                    if isinstance(caption_data, dict) and isinstance(caption_data.get("status"), str):
                        caption, caption_state = "", caption_data["status"]
                except (ValueError, TypeError):
                    pass
                raw_date = row.get("posted_at")
                date = _date(raw_date)
                date_has_zone = bool(raw_date and datetime.fromisoformat(str(raw_date)).tzinfo)
                paths = _paths(row.get("file_paths"), row["id"], report)
                if type(row.get("page_count")) is not int or row["page_count"] < 1:
                    report.errors.append({"work": row["id"], "code": "invalid_page_count"})
                legacy = {
                    "file_paths": row.get("file_paths"), "tags": row.get("tags"),
                    "caption_state": raw_caption, "page_count": row.get("page_count"),
                }
                work = Work(
                    id=row["id"], pixiv_illust_id=remote_id, artist_id=row["artist_id"],
                    title=row["title"], type=row.get("type") or "illust",
                    page_count=max(1, row.get("page_count") or 1), caption=caption,
                    caption_state=caption_state, bookmark_count=row.get("bookmark_count"),
                    view_count=row.get("view_count"), x_restrict=row.get("x_restrict"),
                    restriction_verified=False, posted_at=date,
                    legacy_posted_at=str(raw_date) if raw_date else None,
                    time_basis="utc" if date_has_zone else "legacy_unknown",
                    source_metadata={"legacy": legacy},
                )
                if row.get("downloaded_at"):
                    work.downloaded_at = _date(row["downloaded_at"])
                work.preference = LocalPreference(
                    is_hidden=bool(row.get("is_hidden")), rating=row.get("rating") or 0,
                )
                work.bookmark = RemoteBookmark(value=bool(row.get("is_bookmarked")))
                tags = set(tag_rows.get(work.id, ()))
                if row.get("tags"):
                    try:
                        decoded = json.loads(row["tags"])
                        if not isinstance(decoded, list) or not all(isinstance(v, str) for v in decoded):
                            raise ValueError("invalid tags")
                        tags.update(decoded)
                    except (ValueError, TypeError):
                        report.warnings.append({"work": work.id, "code": "legacy_tags_preserved_raw"})
                invalid_tags = [tag for tag in tags if not isinstance(tag, str)]
                if invalid_tags:
                    report.errors.append({"work": work.id, "code": "invalid_relational_tag"})
                work.tags = [WorkTag(tag=tag) for tag in sorted(tag for tag in tags if isinstance(tag, str))]
                artist = artists[work.artist_id]
                if paths and not artist.directory:
                    artist.directory = str(PurePosixPath(paths[0]).parent)
                caption_directory = str(PurePosixPath(paths[0]).parent) if paths else artist.directory
                if media_root is not None and caption_directory:
                    root = Path(media_root).resolve()
                    caption_path = (root / caption_directory / f"{remote_id}.caption.html").resolve()
                    if caption_path.is_relative_to(root) and caption_path.is_file():
                        work.caption = caption_path.read_text(encoding="utf-8")
                        work.caption_state = "available" if work.caption else "empty"
                        mapped_files.add(caption_path.relative_to(root).as_posix())
                if work.type == "ugoira":
                    work.assets = [Asset(role="ugoira_zip"), Asset(role="ugoira_timing")]
                    for path in paths:
                        suffix = PurePosixPath(path).suffix.lower()
                        if suffix == ".gif":
                            if PurePosixPath(path).name not in (f"{remote_id}.gif", f"{remote_id}_p0.gif"):
                                report.errors.append({"work": work.id, "code": "ambiguous_animation_identity"})
                                continue
                            work.derivatives.append(Derivative(kind="gif", relative_path=path, state="unknown"))
                        elif suffix == ".zip":
                            if PurePosixPath(path).name not in (f"{remote_id}.zip", f"{remote_id}_p0.zip", f"{remote_id}_ugoira.zip") or work.assets[0].relative_path:
                                report.errors.append({"work": work.id, "code": "ambiguous_animation_identity"})
                                continue
                            work.assets[0].relative_path = path
                    if media_root is not None and artist.directory:
                        root = Path(media_root).resolve()
                        for name in (f"{remote_id}_p0.zip", f"{remote_id}_ugoira.zip"):
                            relative = PurePosixPath(artist.directory, name).as_posix()
                            candidate = (root / relative).resolve()
                            if candidate.is_relative_to(root) and candidate.is_file():
                                work.assets[0].relative_path = relative
                                break
                    if work.derivatives and not work.assets[0].relative_path:
                        # A normal automatic check must not backfill these originals.
                        work.source_metadata = {"legacy": legacy, "original_backfill_requires_selection": True}
                        work.assets[0].state = "missing_deferred"
                        report.warnings.append({"work": work.id, "code": "animation_original_missing"})
                else:
                    pages = {i: Asset(role="image", page_index=i, state="missing") for i in range(work.page_count)}
                    for path in paths:
                        name = PurePosixPath(path).name
                        match = re.fullmatch(re.escape(remote_id) + r"(?:_p(\d+))?\.[^.]+", name)
                        index = int(match.group(1) or 0) if match else -1
                        if index not in pages or pages[index].relative_path is not None:
                            report.errors.append({"work": work.id, "code": "ambiguous_asset_identity"})
                            continue
                        pages[index].relative_path, pages[index].state = path, "unknown"
                    work.assets = list(pages.values())
                session.add(work)
                mapped_files.update(record.relative_path for record in [*work.assets, *work.derivatives] if record.relative_path)
                session.add(ArchiveIntent(pixiv_illust_id=remote_id, mode="suppressed" if remote_id in deleted else "keep"))
                report.works += 1
                report.assets += len(work.assets)
            session.flush()
            for remote_id in set(deleted):
                if session.get(ArchiveIntent, remote_id) is None:
                    session.add(ArchiveIntent(pixiv_illust_id=remote_id, mode="suppressed"))
            report.suppressed = len(set(deleted))
        if media_root is not None:
            root = Path(media_root).resolve()
            for path in root.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    if not path.resolve().is_relative_to(root):
                        report.errors.append({"code": "media_symlink_escape", "path": relative})
                    elif relative not in mapped_files:
                        report.unmapped_files.append(relative)
        report.completed = True
    finally:
        if db is not None:
            db.close()
        snapshot.close()
        if db is not None:
            with destination.with_suffix(destination.suffix + ".import.json").open("x", encoding="utf-8") as output:
                json.dump({**asdict(report), "ready_for_review": report.ready_for_review}, output, ensure_ascii=False, indent=2)
    return report
