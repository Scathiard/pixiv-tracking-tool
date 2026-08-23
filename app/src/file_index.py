"""作品文件索引的稳定 JSON 表示与一次扫描构建。"""
from __future__ import annotations

import json
import re
from pathlib import Path


MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".mp4", ".webp", ".bmp"}
_ILLUST_PREFIX = re.compile(r"^(\d+)(?:_p\d+|[_.])", re.IGNORECASE)


def _natural_key(path: Path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def encode_file_paths(paths) -> str | None:
    normalized = [str(path) for path in paths if str(path)]
    if not normalized:
        return None
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def decode_file_paths(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
        if isinstance(decoded, list) and all(isinstance(item, str) for item in decoded):
            return [item for item in decoded if item]
    except (json.JSONDecodeError, TypeError):
        pass
    # 旧版本兼容。含逗号的旧记录无法无损解析，迁移时应从磁盘重建。
    return [item.strip() for item in str(value).split(",") if item.strip()]


def scan_artist_files(artist_dir: Path, web_prefix: str) -> dict[str, list[str]]:
    """只枚举一次画师目录，按 Pixiv 作品 ID 建立有序媒体文件索引。"""
    result: dict[str, list[str]] = {}
    artist_dir = Path(artist_dir)
    if not artist_dir.is_dir():
        return result
    prefix = web_prefix.rstrip("/")
    for path in sorted(artist_dir.iterdir(), key=_natural_key):
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        match = _ILLUST_PREFIX.match(path.name)
        if not match:
            continue
        result.setdefault(match.group(1), []).append(f"{prefix}/{path.name}")
    return result
