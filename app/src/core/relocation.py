"""Prepare a verified independent copy; activation's commit point is the .env replace."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from ..data_migration import MigrationError, execute_migration, prepare_migration
from .runtime import Runtime


class RelocationBusy(MigrationError):
    """No copy was attempted because the source still owns unfinished work."""


def prepare_relocation(current: Runtime, source_root: Path, target_root: Path):
    if current.busy():
        raise RelocationBusy("请等待当前队列完成，再切换数据目录")
    if current.has_pending_publications():
        raise RelocationBusy("还有待恢复的文件，请处理完成后再切换目录")
    plan = prepare_migration(source_root, target_root)
    report = execute_migration(plan)
    candidate = Runtime(plan.target_root / "data/pixiv_tracker.db", plan.target_root / "images", client=current.client)
    try:
        candidate.queue.accept_submissions(False)
        candidate.bookmarks.accept_submissions(False)
        candidate.library.query(limit=1)
        receipt = {"stage": "ready", "source": str(plan.source_root), "target": str(plan.target_root),
                   "image_files": report.image_files, "image_bytes": report.image_bytes}
        with (plan.target_root / f"data/relocation-{uuid4().hex}.json").open("x", encoding="utf-8") as output:
            json.dump(receipt, output, ensure_ascii=False, indent=2)
        return candidate, report
    except BaseException:
        candidate.close()
        raise
