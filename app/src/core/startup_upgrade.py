"""User-confirmed startup upgrade, with a lease and an explicit pre-write rollback boundary."""
from __future__ import annotations
import asyncio
from datetime import datetime
import json
from pathlib import Path
import threading
from uuid import uuid4
from .ownership import LibraryLease
from .upgrade import activate_upgrade, prepare_upgrade, rollback_upgrade


class StartupUpgrade:
    def __init__(self, source, images, factory, *, stopping=lambda:False):
        self.source, self.images = Path(source), Path(images)
        self.factory, self.stopping = factory, stopping
        self._lock = threading.RLock()
        self._status = {"state":"starting", "stage":"正在打开图库", "message":None,
                        "can_start":False, "activated":False, "backup":None, "candidate":None}
        self.receipt = None
        self.task = None

    def _set(self, **changes):
        with self._lock: self._status.update(changes)

    def snapshot(self):
        with self._lock: return dict(self._status)

    def require(self):
        self._set(state="required",stage="旧图库需要升级",message="先备份并核验转换结果，已有图片保持原路径。升级成功后继续使用本地图库。",can_start=True)

    def ready(self):
        self._set(state="ready",stage="图库已准备好",message=None,can_start=False)

    def _receipt_file(self, directory, receipt):
        # The receipt contains paths/counts/fingerprints, never artwork or login.
        path = directory / "receipt.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump(receipt, output, ensure_ascii=False, indent=2, default=str)

    def _prepare_and_start(self):
        current = None
        attempted_start = False
        try:
            if not self.snapshot()["activated"]:
                with LibraryLease(self.images):
                    directory = self.source.parent / "upgrade" / (datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:12])
                    directory.mkdir(parents=True, exist_ok=False)
                    candidate, backup = directory / "candidate.db", directory / "backup.db"
                    self._set(state="preparing",stage="备份旧库并验证升级候选",can_start=False,backup=str(backup),candidate=str(candidate),message=None)
                    prepared = prepare_upgrade(self.source, candidate, backup)
                    if self.stopping():
                        self._set(state="failed",stage="启用已取消",message="程序正在退出；旧库未启用转换，备份及候选保留。",can_start=False)
                        return None
                    self._set(state="activating",stage="启用已核验的候选图库")
                    self.receipt = activate_upgrade(prepared,writers_stopped=True)
                    self._set(activated=True)
                    self._receipt_file(directory,self.receipt)
            if self.stopping():
                self._set(state="failed",stage="等待下次启动",message="升级已经启用，候选与备份已保留；下次启动将继续打开图库。",can_start=False)
                return None
            self._set(state="starting",stage="恢复本地后台服务",message=None,can_start=False)
            # Runtime acquires its own lease. No worker runs before this factory
            # returns, so a construction failure can still restore the old DB.
            current = self.factory()
            if self.stopping():
                current.request_stop(cancel_remote=True); current.close(timeout=None)
                self._set(state="failed",stage="等待下次启动",message="升级已启用，程序正在退出。",can_start=False)
                return None
            attempted_start = True
            current.start()
            return current
        except Exception:
            if current is not None:
                current.request_stop(cancel_remote=True); current.close(timeout=None)
            if self.receipt is not None and not attempted_start:
                try:
                    with LibraryLease(self.images): rollback_upgrade(self.receipt,writers_stopped=True)
                    self.receipt=None; self._set(activated=False)
                except Exception:
                    self._set(state="failed",stage="启用后的恢复需要处理",message="图库启用未完成，自动回退未能完成。保留旧库备份与新版数据，请关闭程序后检查恢复记录。",can_start=False)
                    return None
            activated=self.snapshot()["activated"]
            self._set(state="failed",stage="启动未完成" if activated else "升级未完成",
                message="新版图库及旧库备份已保留；后台恢复可能已写入，未自动覆盖这些记录。可重试启动。" if activated else "旧图库保持可用，备份和候选保留。请检查可用磁盘空间及文件占用，然后重试。",
                can_start=not self.stopping())
            return None

    async def start(self, on_ready):
        with self._lock:
            if self.task is not None and not self.task.done(): return self.snapshot()
            if not self._status["can_start"] or self.stopping(): return self.snapshot()
            self._status.update(state="starting" if self._status["activated"] else "preparing",can_start=False)
            async def run():
                current = await asyncio.to_thread(self._prepare_and_start)
                if current is None: return
                try:
                    await on_ready(current)
                    self.ready()
                except Exception:
                    current.request_stop(cancel_remote=True); await asyncio.to_thread(current.close,timeout=None)
                    self._set(state="failed",stage="启动未完成",message="新版图库与备份保留，可重试启动。",can_start=not self.stopping())
            self.task = asyncio.create_task(run())
            return self.snapshot()
