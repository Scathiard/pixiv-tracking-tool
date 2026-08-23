"""永久删除作品的持久化 tombstone，避免后续同步重新加入。"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from pathlib import Path

from . import config as _cfg


_lock = threading.RLock()
_ILLUST_FILE = re.compile(r"^(\d+)(?:[_.])")


def _store_path() -> Path:
    return _cfg.DATA_DIR / "deleted_illusts.json"


def load_deleted_illust_ids() -> set[str]:
    path = _store_path()
    with _lock:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"永久删除清单无法读取: {exc}") from exc
    if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
        raise RuntimeError("永久删除清单格式无效")
    return set(payload)


def _save(values: set[str]):
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    rendered = json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def add_deleted_illust(illust_id: str) -> bool:
    """加入 tombstone；返回本次是否新增。"""
    value = str(illust_id)
    with _lock:
        values = load_deleted_illust_ids()
        if value in values:
            return False
        values.add(value)
        _save(values)
        return True


def discard_deleted_illust(illust_id: str):
    value = str(illust_id)
    with _lock:
        values = load_deleted_illust_ids()
        if value in values:
            values.remove(value)
            _save(values)


def purge_deleted_files(artist_dir: Path) -> int:
    """移除下载流程可能重新落盘的 tombstone 作品文件。"""
    artist_dir = Path(artist_dir)
    if not artist_dir.is_dir():
        return 0
    deleted = load_deleted_illust_ids()
    if not deleted:
        return 0
    removed = 0
    for path in artist_dir.iterdir():
        match = _ILLUST_FILE.match(path.name)
        if match and match.group(1) in deleted and (path.is_file() or path.is_symlink()):
            path.unlink()
            removed += 1
    return removed
