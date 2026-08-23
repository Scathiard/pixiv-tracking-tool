"""可恢复的数据根目录复制与验证。配置切换由调用方在成功后原子执行。"""
from __future__ import annotations

import shutil
import sqlite3
import os
from dataclasses import dataclass
from pathlib import Path


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationPlan:
    source_root: Path
    target_root: Path
    estimated_bytes: int
    image_files: int


@dataclass(frozen=True)
class MigrationReport:
    source_root: Path
    target_root: Path
    database_ok: bool
    image_files: int
    image_bytes: int


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or bool(
        getattr(path, "is_junction", lambda: False)()
    )


def _iter_copyable_files(root: Path):
    if not root.exists():
        return
    for path in root.rglob("*"):
        if _is_link_or_junction(path):
            raise MigrationError(f"迁移源包含符号链接或联接点: {path}")
        if ".pixiv_tracker_trash" in path.parts:
            continue
        if path.is_file() and path.name != "tracker.log" and not path.name.endswith(
            (".part", ".tmp", "-wal", "-shm")
        ):
            yield path


def prepare_migration(source_root: Path, target_root: Path) -> MigrationPlan:
    source = Path(source_root).expanduser().resolve()
    raw_target = Path(target_root).expanduser()
    if raw_target.exists() and _is_link_or_junction(raw_target):
        raise MigrationError("目标目录不能是符号链接或联接点")
    target = raw_target.resolve()
    if not source.is_dir():
        raise MigrationError(f"源数据目录不存在: {source}")
    if source == target:
        raise MigrationError("目标目录与当前数据目录相同")
    if _is_relative_to(target, source):
        raise MigrationError("目标目录不能位于当前数据目录内部")
    if target.exists() and any(target.iterdir()):
        raise MigrationError("目标目录必须是空目录")

    image_files = 0
    estimated_bytes = 0
    for folder in (source / "data", source / "images"):
        for path in _iter_copyable_files(folder) or ():
            estimated_bytes += path.stat().st_size
            if _is_relative_to(path, source / "images"):
                image_files += 1
    existing_parent = target
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    safety_margin = max(16 * 1024 * 1024, estimated_bytes // 20)
    if shutil.disk_usage(existing_parent).free < estimated_bytes + safety_margin:
        raise MigrationError("目标磁盘剩余空间不足")
    return MigrationPlan(source, target, estimated_bytes, image_files)


def persist_data_root(env_path: Path, new_root: Path):
    """以临时文件 + os.replace 原子更新 DATA_ROOT，不触碰其他配置项。"""
    env_path = Path(env_path)
    new_value = str(Path(new_root).resolve())
    content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = content.splitlines()
    replaced = False
    output: list[str] = []
    for line in lines:
        if line.strip().startswith("DATA_ROOT="):
            output.append(f"DATA_ROOT={new_value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"DATA_ROOT={new_value}")
    rendered = "\n".join(output).rstrip("\n") + "\n"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = env_path.with_name(env_path.name + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, env_path)


def _copy_tree(source: Path, target: Path, *, skip_database: bool = False):
    if not source.exists():
        return
    for path in _iter_copyable_files(source) or ():
        relative = path.relative_to(source)
        if skip_database and relative.as_posix() == "pixiv_tracker.db":
            continue
        destination = target / relative
        if destination.exists():
            raise MigrationError(f"迁移目标出现意外同名文件: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def _manifest(root: Path, *, skip_database: bool = False) -> dict[str, int]:
    result = {}
    for path in _iter_copyable_files(root) or ():
        relative = path.relative_to(root).as_posix()
        if skip_database and relative == "pixiv_tracker.db":
            continue
        result[relative] = path.stat().st_size
    return result


def _backup_sqlite(source_db: Path, target_db: Path) -> bool:
    if not source_db.exists():
        return True
    target_db.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(source_db) as source, sqlite3.connect(target_db) as target:
            source.backup(target)
        with sqlite3.connect(target_db) as verify:
            return verify.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error as exc:
        raise MigrationError(f"SQLite 快照失败: {exc}") from exc


def execute_migration(plan: MigrationPlan) -> MigrationReport:
    if plan.target_root.exists() and any(plan.target_root.iterdir()):
        raise MigrationError("目标目录在迁移开始前已不再为空")
    plan.target_root.mkdir(parents=True, exist_ok=True)
    target_data = plan.target_root / "data"
    target_images = plan.target_root / "images"
    target_data.mkdir()
    target_images.mkdir()

    source_data_manifest = _manifest(plan.source_root / "data", skip_database=True)
    source_image_manifest = _manifest(plan.source_root / "images")

    database_ok = _backup_sqlite(
        plan.source_root / "data" / "pixiv_tracker.db",
        target_data / "pixiv_tracker.db",
    )
    if not database_ok:
        raise MigrationError("迁移后的数据库完整性检查失败")

    _copy_tree(plan.source_root / "data", target_data, skip_database=True)
    _copy_tree(plan.source_root / "images", target_images)

    target_data_manifest = _manifest(target_data, skip_database=True)
    target_image_manifest = _manifest(target_images)
    if target_data_manifest != source_data_manifest:
        raise MigrationError("data 文件清单或大小校验失败")
    if target_image_manifest != source_image_manifest:
        raise MigrationError("图片文件清单或大小校验失败")
    image_bytes = sum(target_image_manifest.values())
    if len(target_image_manifest) != plan.image_files:
        raise MigrationError("迁移期间源图片清单发生变化")
    return MigrationReport(
        source_root=plan.source_root,
        target_root=plan.target_root,
        database_ok=database_ok,
        image_files=len(target_image_manifest),
        image_bytes=image_bytes,
    )
