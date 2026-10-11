"""One SQLite checkpoint owner; bounded RAM state and soft background pressure."""
from __future__ import annotations

import math
import logging
from pathlib import Path
import sqlite3
import threading
import time

from . import diagnostics

log = logging.getLogger(__name__)


class WalMaintenance:
    MIN_SQLITE = (3, 51, 3)

    def __init__(self, path: Path, *, trigger_frames=1000, interval=.25,
                 high_bytes=32 * 1024 * 1024):
        self.path = Path(path)
        if trigger_frames < 1 or interval <= 0 or high_bytes < 1:
            raise ValueError("整理阈值和间隔必须为正数")
        self.trigger_frames, self.interval, self.high_bytes = trigger_frames, interval, high_bytes
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._wake, self._stop, self._ready = (threading.Event() for _ in range(3))
        self._thread = None
        self._startup_error = None
        self._last_commit = time.monotonic()
        self._commit_generation = 0
        self._observation_generation = None
        self._probe_generation = None
        self._probe_inflight = None
        self._last_probe = 0.
        self._reported_error = None
        self._state = {"status": "new", "pressure": False, "log_frames": None,
            "checkpointed_frames": None, "pending_frames": None, "page_size": None,
            "high_frames": None, "low_frames": None, "last_error": None,
            "last_mode": None, "last_started": None, "last_seconds": None,
            "max_seconds": 0., "attempts": 0, "busy_count": 0, "error_count": 0,
            "probe_count": 0}

    def snapshot(self):
        with self._lock:
            return {**self._state, "running": self.alive(), "stop_requested": self._stop.is_set(),
                "probe_ready": self._probe_generation == self._commit_generation,
                "probe_inflight": self._probe_inflight is not None}

    def pressured(self):
        with self._lock:
            return self._state["pressure"] or (self._ready.is_set() and not self.alive())

    def notify_commit(self):
        # No SQL, checkpoint waits or worker locks on the business path.
        with self._lock:
            self._last_commit = time.monotonic()
            self._commit_generation += 1
            self._probe_generation = None
            generation = self._commit_generation
        diagnostics.emit("wal.queued", generation=generation, already_queued=self._wake.is_set())
        self._wake.set()

    def can_probe(self):
        with self._lock:
            return (not self._stop.is_set() and self._state["status"] == "ready"
                and self._probe_generation == self._commit_generation and self._probe_inflight is None)

    def begin_step(self):
        """Called only in the business writer critical section, after eligibility."""
        with self._lock:
            if not self._state["pressure"] and not (self._ready.is_set() and not self.alive()):
                diagnostics.emit("wal.backpressure.allow", probe=False, generation=self._commit_generation)
                return True, None
            if (self._stop.is_set() or self._state["status"] != "ready" or
                    self._probe_generation != self._commit_generation or self._probe_inflight is not None):
                diagnostics.emit("wal.backpressure.deny", status=self._state["status"],
                    stop_requested=self._stop.is_set(), generation=self._commit_generation,
                    probe_ready=self._probe_generation == self._commit_generation,
                    probe_inflight=self._probe_inflight is not None)
                return False, None
            token = object()
            self._probe_generation = None
            self._probe_inflight = token
            self._state["probe_count"] += 1
            diagnostics.emit("wal.backpressure.allow", probe=True, generation=self._commit_generation)
            return True, token

    def finish_step(self, token):
        with self._lock:
            if token is self._probe_inflight:
                self._probe_inflight = None
                diagnostics.emit("wal.backpressure.finished", generation=self._commit_generation)
        self._wake.set()

    def start(self, timeout=5):
        with diagnostics.span("wal.start", timeout_seconds=timeout):
            with self._lifecycle_lock:
                if self._stop.is_set():
                    raise RuntimeError("已停止的整理执行者不能重新启动")
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, name="archive-wal-maintenance", daemon=False)
                    self._thread.start()
            if not self._ready.wait(timeout):
                raise TimeoutError("数据库整理连接尚未就绪，业务未启动")
            if self._startup_error is not None:
                raise RuntimeError("数据库整理连接启用失败") from self._startup_error
            if not self._thread.is_alive():
                raise RuntimeError("数据库整理执行者已结束")

    def stop(self, timeout=5):
        with diagnostics.span("wal.stop", timeout_seconds=timeout) as detail:
            self._stop.set()
            with self._lock:
                self._probe_generation = None
            self._wake.set()
            if self._thread is not None and self._thread.ident is not None:
                self._thread.join(timeout)
            stopped = self._thread is None or not self._thread.is_alive()
            detail['stopped'] = stopped
            return stopped

    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def _connect(self):
        # Created, used and closed exclusively by the maintenance thread.
        connection = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        try:
            version = tuple(int(part) for part in connection.execute("SELECT sqlite_version()").fetchone()[0].split("."))
            if version < self.MIN_SQLITE:
                raise RuntimeError("SQLite 3.51.3 或更新版本才支持安全的 NOOP 整理")
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                raise RuntimeError("后台整理需要已启用 WAL 的数据库")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=0")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            # Open the WAL read view even when the database has no recent writes.
            connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            with self._lock:
                high = max(1, math.ceil(self.high_bytes / page_size))
                self._state.update(page_size=page_size, high_frames=high, low_frames=max(0, high // 2))
            self._checkpoint(connection, "NOOP")
            return connection
        except BaseException:
            connection.close()
            raise

    def _checkpoint(self, connection, mode):
        with diagnostics.span("wal.checkpoint", mode=mode) as detail:
            if mode not in ("NOOP", "PASSIVE"):
                raise ValueError("不支持的整理模式")
            started = time.monotonic()
            with self._lock:
                self._state.update(last_mode=mode, last_started=started)
                self._state["attempts"] += 1
                generation = self._commit_generation
            try:
                row = connection.execute(f"PRAGMA main.wal_checkpoint({mode})").fetchone()
                if row is None or len(row) != 3:
                    raise RuntimeError("SQLite 未返回有效整理状态")
                busy, log, checkpointed = row
                if any(type(value) is not int for value in row):
                    raise RuntimeError("SQLite 整理计数格式错误")
                with self._lock:
                    detail.update(busy=busy, log_frames=log, checkpointed_frames=checkpointed,
                        generation=generation)
                    self._state["last_seconds"] = time.monotonic() - started
                    self._state["max_seconds"] = max(self._state["max_seconds"], self._state["last_seconds"])
                    if busy or log < 0 or checkpointed < 0 or checkpointed > log:
                        self._state["status"] = "busy" if busy else "unknown"
                        self._state["busy_count"] += int(bool(busy))
                        # Unknown observations never clear an existing pressure.
                        self._state["pressure"] = True
                        self._probe_generation = None
                        self._observation_generation = None
                        detail.update(pressure=True, status=self._state['status'])
                        return False
                    self._state.update(status="ready", log_frames=log,
                        checkpointed_frames=checkpointed, pending_frames=log - checkpointed)
                    self._observation_generation = generation if generation == self._commit_generation else None
                    if log >= self._state["high_frames"]:
                        self._state["pressure"] = True
                    elif log <= self._state["low_frames"]:
                        self._state["pressure"] = False
                        self._probe_generation = None
                    detail.update(pressure=self._state['pressure'], status=self._state['status'],
                        pending_frames=self._state['pending_frames'])
                return True
            except Exception as exc:
                with self._lock:
                    self._state.update(status="error", pressure=True,
                        last_error=type(exc).__name__, last_seconds=time.monotonic() - started)
                    self._state["error_count"] += 1
                    self._state["max_seconds"] = max(self._state["max_seconds"], self._state["last_seconds"])
                    self._probe_generation = None
                    self._observation_generation = None
                if self._reported_error != type(exc).__name__:
                    logging.getLogger(__name__).warning("数据库后台整理暂缓后台新步骤：%s", type(exc).__name__)
                    self._reported_error = type(exc).__name__
                raise

    def _cycle(self, connection):
        if not self._checkpoint(connection, "NOOP"):
            return False
        with self._lock:
            state = dict(self._state)
            idle = time.monotonic() - self._last_commit >= self.interval
        if state["pressure"] or state["pending_frames"] >= self.trigger_frames or (idle and state["pending_frames"]):
            if not self._checkpoint(connection, "PASSIVE"):
                return False
        with self._lock:
            if (self._state["pressure"] and self._state["pending_frames"] == 0
                    and self._observation_generation == self._commit_generation
                    and self._probe_inflight is None and self._probe_generation is None
                    and time.monotonic() - self._last_probe >= self.interval):
                # Remain under pressure. One actual background unit may try
                # the next SQLite write, which naturally attempts WAL reuse.
                self._probe_generation = self._commit_generation
                self._last_probe = time.monotonic()
                diagnostics.emit("wal.backpressure.probe_ready", generation=self._commit_generation)
        if self._reported_error is not None:
            log.info("数据库后台整理已恢复，后台按压力继续推进")
            self._reported_error = None
        return True

    def _run(self):
        connection = None
        try:
            try:
                with diagnostics.span("wal.connection"):
                    connection = self._connect()
            except BaseException as exc:
                self._startup_error = exc
                with self._lock:
                    self._state.update(status="failed", pressure=True, last_error=type(exc).__name__)
                log.warning("数据库后台整理启用失败，业务尚未启用：%s", type(exc).__name__)
                return
            finally:
                self._ready.set()
            delay = self.interval
            while not self._stop.is_set():
                # Time is measured after the preceding SQL ends. Notifications
                # wake monitoring but cannot turn the minimum gap into a spin.
                deadline = time.monotonic() + delay
                with diagnostics.span("wal.wait", delay_seconds=delay):
                    while not self._stop.is_set() and time.monotonic() < deadline:
                        self._wake.wait(max(0, deadline - time.monotonic()))
                        self._wake.clear()
                if self._stop.is_set():
                    break
                try:
                    with diagnostics.span("wal.cycle") as detail:
                        success = self._cycle(connection)
                        detail['success'] = success
                except Exception:
                    success = False
                delay = self.interval if success else min(5., max(self.interval, delay * 2))
            try:
                with diagnostics.span("wal.exit.checkpoint"):
                    self._checkpoint(connection, "PASSIVE")
            except Exception:
                pass  # Retained RAM error; WAL durability does not require drain.
        finally:
            try:
                if connection is not None:
                    with diagnostics.span("wal.exit.close"):
                        connection.close()
            finally:
                with self._lock:
                    self._state["status"] = "stopped" if connection is not None else "failed"
                diagnostics.emit("wal.exited", status=self._state['status'])
