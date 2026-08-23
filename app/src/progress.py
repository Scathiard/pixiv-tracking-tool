"""串行重任务队列与兼容旧调用点的进度 API。"""
from __future__ import annotations

import queue
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable


TERMINAL_STATES = {"succeeded", "failed", "interrupted"}


@dataclass
class _Task:
    task_id: str
    kind: str = "task"
    label: str = ""
    dedupe_key: str | None = None
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    updated_at: float = field(default_factory=time.time)
    phase: str = "checking"
    current_artist: str = ""
    current_detail: str = ""
    artists_checked: int = 0
    total_artists: int = 0
    new_works_found: int = 0
    files_total: int = 0
    files_done: int = 0
    dl_artist_index: int = 0
    dl_artist_total: int = 0
    errors: list[str] = field(default_factory=list)
    result: str = ""


class JobManager:
    def __init__(self, history_limit: int = 20, terminal_ttl: float = 24 * 3600):
        self.history_limit = history_limit
        self.terminal_ttl = terminal_ttl
        self._tasks: dict[str, _Task] = {}
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._thread_context = threading.local()
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="pixiv-job-worker", daemon=True)
        self._worker.start()

    def submit(
        self,
        kind: str,
        target: Callable[[], object],
        *,
        label: str = "",
        dedupe_key: str | None = None,
    ) -> tuple[str, bool]:
        with self._condition:
            if self._closed:
                raise RuntimeError("任务管理器已经关闭")
            if dedupe_key:
                for task in self._tasks.values():
                    if task.dedupe_key == dedupe_key and task.status in {"queued", "running"}:
                        return task.task_id, False
            task_id = uuid.uuid4().hex[:8]
            self._tasks[task_id] = _Task(
                task_id=task_id,
                kind=kind,
                label=label,
                dedupe_key=dedupe_key,
            )
            self._queue.put((task_id, target))
            self._condition.notify_all()
            return task_id, True

    def submit_exclusive(
        self,
        kind: str,
        target: Callable[[], object],
        *,
        label: str = "",
    ) -> tuple[str | None, bool]:
        """只在队列完全空闲时创建任务；用于迁移等不能延迟执行的操作。"""
        with self._condition:
            if self._closed:
                raise RuntimeError("任务管理器已经关闭")
            if any(task.status in {"queued", "running"} for task in self._tasks.values()):
                return None, False
            task_id = uuid.uuid4().hex[:8]
            self._tasks[task_id] = _Task(
                task_id=task_id,
                kind=kind,
                label=label,
                dedupe_key=kind,
            )
            self._queue.put((task_id, target))
            self._condition.notify_all()
            return task_id, True

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            task_id, target = item
            with self._condition:
                task = self._tasks.get(task_id)
                if task is None:
                    self._queue.task_done()
                    continue
                task.status = "running"
                task.started_at = task.updated_at = time.time()
                self._thread_context.task_id = task_id
                self._condition.notify_all()
            try:
                result = target()
                with self._condition:
                    task = self._tasks[task_id]
                    if result is not None:
                        task.result = str(result)
                    task.status = "failed" if task.errors else "succeeded"
            except Exception as exc:
                with self._condition:
                    task = self._tasks[task_id]
                    task.errors.append(f"{type(exc).__name__}: {exc}")
                    task.status = "failed"
                    task.result = traceback.format_exc(limit=5)
            finally:
                with self._condition:
                    task = self._tasks.get(task_id)
                    if task:
                        task.finished_at = task.updated_at = time.time()
                    self._thread_context.task_id = None
                    self._prune_locked()
                    self._condition.notify_all()
                self._queue.task_done()

    def current_job_id(self) -> str | None:
        return getattr(self._thread_context, "task_id", None)

    def begin_manual(self, label: str = "") -> str:
        with self._condition:
            task_id = uuid.uuid4().hex[:8]
            now = time.time()
            self._tasks[task_id] = _Task(
                task_id=task_id,
                kind="manual",
                label=label,
                status="running",
                started_at=now,
                updated_at=now,
            )
            self._condition.notify_all()
            return task_id

    def finish_manual(self, task_id: str):
        with self._condition:
            task = self._tasks.get(task_id)
            if not task or task.status in TERMINAL_STATES:
                return
            task.status = "failed" if task.errors else "succeeded"
            task.finished_at = task.updated_at = time.time()
            self._condition.notify_all()

    def mutate(self, task_id: str, callback):
        with self._condition:
            task = self._tasks.get(task_id)
            if not task:
                return
            callback(task)
            task.updated_at = time.time()
            self._condition.notify_all()

    def get_job(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return asdict(task) if task else None

    def is_busy(self) -> bool:
        with self._lock:
            return any(task.status in {"queued", "running"} for task in self._tasks.values())

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self.is_busy():
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def wait_for_job(self, task_id: str, timeout: float | None = None) -> dict | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                task = self._tasks.get(task_id)
                if task is None or task.status in TERMINAL_STATES:
                    return asdict(task) if task else None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return asdict(task)
                self._condition.wait(remaining)

    def _prune_locked(self, now: float | None = None):
        now = time.time() if now is None else now
        terminal = [task for task in self._tasks.values() if task.status in TERMINAL_STATES]
        for task in terminal:
            if task.finished_at is not None and now - task.finished_at > self.terminal_ttl:
                self._tasks.pop(task.task_id, None)
        terminal = sorted(
            (task for task in self._tasks.values() if task.status in TERMINAL_STATES),
            key=lambda task: task.finished_at or task.updated_at,
            reverse=True,
        )
        for task in terminal[self.history_limit:]:
            self._tasks.pop(task.task_id, None)

    def prune(self):
        with self._condition:
            self._prune_locked()

    def state(self) -> dict:
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda task: task.created_at)
            active = [task for task in tasks if task.status in {"queued", "running"}]
            terminal = [task for task in tasks if task.status in TERMINAL_STATES]
            if not active:
                state = _empty_state()
                if terminal:
                    state["last_job"] = asdict(terminal[-1])
                state["jobs"] = [asdict(task) for task in tasks]
                return state
            primary = next((task for task in active if task.status == "running"), active[0])
            errors = [error for task in active for error in task.errors]
            return {
                "active": True,
                "task_id": primary.task_id,
                "status": primary.status,
                "phase": primary.phase,
                "current_artist": primary.current_artist or primary.label,
                "current_detail": primary.current_detail,
                "artists_checked": primary.artists_checked,
                "total_artists": primary.total_artists,
                "new_works_found": sum(task.new_works_found for task in active),
                "files_total": sum(task.files_total for task in active),
                "files_done": sum(task.files_done for task in active),
                "dl_artist_index": primary.dl_artist_index,
                "dl_artist_total": primary.dl_artist_total,
                "errors": errors[-20:],
                "task_count": len(active),
                "jobs": [asdict(task) for task in tasks],
                "last_job": asdict(terminal[-1]) if terminal else None,
            }

    def shutdown(self, wait: bool = True):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)
            self._condition.notify_all()
        if wait:
            self._worker.join(timeout=5)


def _empty_state():
    return {
        "active": False,
        "task_id": None,
        "status": None,
        "phase": "checking",
        "current_artist": "",
        "current_detail": "",
        "artists_checked": 0,
        "total_artists": 0,
        "new_works_found": 0,
        "files_total": 0,
        "files_done": 0,
        "dl_artist_index": 0,
        "dl_artist_total": 0,
        "errors": [],
        "task_count": 0,
        "jobs": [],
        "last_job": None,
    }


_manager = JobManager()
_gate_condition = threading.Condition(threading.RLock())
_active_mutations = 0
_maintenance_active = False


def begin_mutation() -> bool:
    """为短时 HTTP 写操作取得租约；迁移准备/执行期间拒绝新写入。"""
    global _active_mutations
    with _gate_condition:
        if _maintenance_active:
            return False
        _active_mutations += 1
        return True


def end_mutation():
    global _active_mutations
    with _gate_condition:
        _active_mutations = max(0, _active_mutations - 1)
        _gate_condition.notify_all()


def begin_maintenance() -> bool:
    """仅在没有短时写操作时进入维护态，并阻止新的 HTTP 写入。"""
    global _maintenance_active
    with _gate_condition:
        if _maintenance_active or _active_mutations:
            return False
        _maintenance_active = True
        _gate_condition.notify_all()
        return True


def end_maintenance():
    global _maintenance_active
    with _gate_condition:
        _maintenance_active = False
        _gate_condition.notify_all()


def maintenance_active() -> bool:
    with _gate_condition:
        return _maintenance_active


def submit_task(kind, target, *, label="", dedupe_key=None):
    return _manager.submit(kind, target, label=label, dedupe_key=dedupe_key)


def submit_exclusive(kind, target, *, label=""):
    return _manager.submit_exclusive(kind, target, label=label)


def is_busy():
    return _manager.is_busy()


def wait_for_idle(timeout=None):
    return _manager.wait_for_idle(timeout)


def wait_for_job(task_id, timeout=None):
    return _manager.wait_for_job(task_id, timeout)


def get_job(task_id):
    return _manager.get_job(task_id)


def shutdown(wait=True):
    _manager.shutdown(wait=wait)


def begin_task(artist_name=""):
    task_id = _manager.current_job_id()
    if task_id:
        if artist_name:
            _manager.mutate(task_id, lambda task: setattr(task, "label", artist_name))
        return task_id
    return _manager.begin_manual(artist_name)


def begin_phase(task_id, phase):
    _manager.mutate(task_id, lambda task: setattr(task, "phase", phase))


def set_artist(task_id, name):
    _manager.mutate(task_id, lambda task: setattr(task, "current_artist", name))


def set_detail(task_id, msg):
    _manager.mutate(task_id, lambda task: setattr(task, "current_detail", msg))


def set_artist_progress(task_id, checked, total):
    def update(task):
        task.artists_checked = checked
        task.total_artists = total
    _manager.mutate(task_id, update)


def add_found(task_id, n):
    _manager.mutate(task_id, lambda task: setattr(task, "new_works_found", task.new_works_found + n))


def set_files_total(task_id, n):
    _manager.mutate(task_id, lambda task: setattr(task, "files_total", n))


def add_files_done(task_id, n):
    _manager.mutate(task_id, lambda task: setattr(task, "files_done", task.files_done + n))


def set_dl_artist_progress(task_id, index, total):
    def update(task):
        task.dl_artist_index = index
        task.dl_artist_total = total
    _manager.mutate(task_id, update)


def add_error(task_id, msg):
    def update(task):
        task.errors.append(str(msg))
        task.errors = task.errors[-20:]
    _manager.mutate(task_id, update)


def finish_task(task_id):
    if _manager.current_job_id() == task_id:
        return
    _manager.finish_manual(task_id)


def get_state():
    return _manager.state()
