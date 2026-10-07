"""Persisted operations and fixed queue rounds, with domain handlers at one sync boundary."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Protocol
from datetime import timedelta
from contextlib import ExitStack, contextmanager

from sqlalchemy import and_, case, event, exists, func, or_, select, true, tuple_, union_all
from sqlalchemy.orm import aliased

from .database import ArchiveDatabase
from .catalogue_revision import catalogue_revision
from .models import ApplicationState, Asset, Derivative, Operation, OperationItem, TaskRound, Work, utcnow
from ..version import __build__
from ..request_policy import AccountChanged, RequestDeferred, TaskRemoteRetry
from .read_control import LocalReadError

log = logging.getLogger(__name__)
SETTLED = ("succeeded", "failed", "skipped", "not_executed")
ACTIVE = ("queued", "planning", "ready", "running", "interrupted")


@dataclass(frozen=True)
class ItemSpec:
    key: str
    kind: str
    target: dict
    phase: str
    unit: str = "items"
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class ItemResult:
    state: str = "succeeded"
    detail: dict = field(default_factory=dict)


class Handler(Protocol):
    def plan(self, parameters: dict, context: TaskContext) -> list[ItemSpec]: ...
    def execute(self, item: ItemSpec, context: TaskContext) -> ItemResult: ...


def outcome(counts: dict) -> str:
    total = sum(counts.get(state, 0) for state in SETTLED)
    if not total:
        return "empty"
    if counts.get("failed") or counts.get("not_executed"):
        return "partial_failure" if counts.get("succeeded") or counts.get("skipped") else "failed"
    return "succeeded"


def safe_failure(exc: Exception) -> dict:
    # Requests exception strings can contain URLs or authorization-related data.
    # Domain messages are supplied as explicit ItemResult details, not arbitrary exception reprs.
    failure = {"code": type(exc).__name__, "message": getattr(exc, "user_message", "此项未完成，请重试")}
    status = getattr(exc, "status", None)
    if type(status) is int and 100 <= status <= 599:
        failure["status"] = status
    retry_after = getattr(exc, "retry_after", None)
    if type(retry_after) in (int, float) and 0 <= retry_after <= 86400:
        failure["retry_after"] = retry_after
    retryable = getattr(exc, "retryable", None)
    if type(retryable) is bool:
        failure["retryable"] = retryable
    code = getattr(exc, "failure_code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        failure["failure_code"] = code
    return failure


@dataclass(frozen=True)
class PlanBatch:
    specs: list[ItemSpec]
    cursor: dict = field(default_factory=dict)
    complete: bool = True


class ManualPauseRequested(BaseException):
    """Yield at a safe checkpoint without classifying control as failure."""


class ConnectionRequired(BaseException):
    """Keep remote work pending while its runtime has no connected client."""


class TaskContext:
    def __init__(self, queue: TaskQueue, operation_id: str, item_id: str | None = None, *, kind=None):
        self.queue, self.operation_id, self.item_id = queue, operation_id, item_id
        self.kind = kind
        self.requests_sent = 0

    def mark_remote_attempt(self):
        self.requests_sent += 1

    def report(self, phase: str, done: int, total: int | None, unit: str, detail: str):
        self.checkpoint()
        if done < 0 or total is not None and (total < 0 or done > total):
            raise ValueError("进度计数超出清单范围")
        with self.queue.database.write() as session:
            operation = session.get(Operation, self.operation_id)
            operation.phase, operation.phase_done, operation.phase_total = phase, done, total
            operation.phase_unit, operation.detail, operation.updated_at = unit, detail, utcnow()
            snapshots = dict(operation.result.get("phase_snapshots", {}))
            snapshots[phase + ":" + unit] = {"phase": phase, "unit": unit, "done": done, "total": total}
            operation.result = {**operation.result, "phase_snapshots": snapshots}

    def checkpoint_parameters(self, parameters: dict):
        """Freeze a started automatic batch's artist selection before network I/O."""
        self.checkpoint()
        with self.queue.database.write() as session:
            session.get(Operation, self.operation_id).parameters = parameters

    def report_step(self, phase, unit):
        self.checkpoint()
        with self.queue.database.write() as session:
            operation = session.get(Operation, self.operation_id)
            self.queue._report_step_in_transaction(session, operation, phase, unit)
        with self.queue._scheduler_lock:
            self.queue._validated_phases.add((self.operation_id, phase + ":" + unit))

    def report_bytes(self, done: int, total: int | None):
        self.checkpoint()
        with self.queue.database.write() as session:
            operation = session.get(Operation, self.operation_id)
            current_items = dict(operation.result.get("current_items", {}))
            current_items[self.item_id] = {"id": self.item_id, "unit": "bytes", "done": done, "total": total}
            operation.result = {**operation.result, "current_items": current_items, "current_item": current_items[self.item_id]}

    def detail(self, text):
        self.checkpoint()
        with self.queue.database.write() as session:
            operation = session.get(Operation, self.operation_id)
            operation.detail, operation.updated_at = text, utcnow()

    def checkpoint(self):
        if self.queue._stop.is_set():
            from ..request_policy import ShutdownRequested
            raise ShutdownRequested()
        if self.queue.operation_paused(self.operation_id):
            raise ManualPauseRequested()
        if self.kind is not None and not self.queue.kind_allowed(self.kind):
            raise ConnectionRequired()
        guard = getattr(self, "remote_guard", None)
        if guard is not None:
            guard()


class TaskQueue:
    def __init__(self, database: ArchiveDatabase, handlers: dict[str, Handler], *, allowed_kinds=None, check_workers=1):
        if check_workers not in (1, 2):
            raise ValueError("检查者数量只能为 1 或 2")
        self.database, self.handlers = database, handlers
        self.check_workers = check_workers
        self.allowed_kinds = allowed_kinds
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._threads = []
        self._planning = set()
        self._checking = set()
        self._check_turns = {}
        self._scheduler_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        self._resources = set()
        self._accepting = True
        self.worker_error = None
        self.maintenance = None
        self.maintenance_error = None
        self._claim_sequence = 0
        self._round_admissions = 0
        self._candidate_after = {}
        self._candidate_epochs = {}
        self._operation_after = {}
        self._operation_epochs = {}
        self._pause_generation = 0
        self._pause_cache = None
        self._validated_phases = set()
        self._scan_context = threading.local()
        self._last_scan_more = False
        with self.database.read() as session:
            records = session.execute(select(Operation.id, Operation.pause_requested, Operation.result).where(Operation.state.in_(ACTIVE))).all()
            # Finished intermediates can still connect an active grandchild to
            # its paused ancestor. Restore only ancestors referenced by this graph.
            known = {ident for ident, _, _ in records}
            pending = {parent for _, _, result in records for parent in result.get("parent_operation_ids", [])} - known
            while pending:
                requested = list(pending)[:500]
                known.update(requested)
                pending.difference_update(requested)
                ancestors = session.execute(select(Operation.id, Operation.pause_requested, Operation.result)
                    .where(Operation.id.in_(requested))).all()
                records.extend(ancestors)
                pending.update(parent for _, _, result in ancestors for parent in result.get("parent_operation_ids", [])
                    if parent not in known)
        self._paused_operations = {ident for ident, paused, _ in records if paused}
        self._control_parents = {ident: result.get("parent_operation_ids", []) for ident, _, result in records}

    @property
    def _last_scan_more(self):
        return getattr(self._scan_context, "more", False)

    @_last_scan_more.setter
    def _last_scan_more(self, value):
        self._scan_context.more = value

    def _advance_candidate_cursor(self, key, expected_epoch, after):
        with self._scheduler_lock:
            if self._candidate_epochs.get(key, 0) != expected_epoch:
                return None
            epoch = expected_epoch + 1
            self._candidate_epochs[key] = epoch
            if after is None:
                self._candidate_after.pop(key, None)
            else:
                self._candidate_after[key] = after
            return epoch

    def kind_allowed(self, kind):
        allowed = self.allowed_kinds() if self.allowed_kinds else None
        return allowed is None or kind in allowed

    def _reset_operation_windows_locked(self, key=None):
        keys = (set(self._operation_epochs) | set(self._operation_after)) if key is None else (key,)
        for current in keys:
            self._operation_after.pop(current, None)
            self._operation_epochs[current] = self._operation_epochs.get(current, 0) + 1

    def _advance_operation_cursor(self, key, expected_epoch, after):
        with self._scheduler_lock:
            if self._operation_epochs.get(key, 0) != expected_epoch:
                return False
            self._operation_epochs[key] = expected_epoch + 1
            if after is None:
                self._operation_after.pop(key, None)
            else:
                self._operation_after[key] = after
            return True

    def _publish_controls_after_commit(self, session, *, parents=None, paused=None):
        # Caller-owned transactions can append to an existing child's parents.
        # Publish only committed relationships; callbacks use no SQL/connection.
        key = ("task_controls", id(self))
        changes = session.info.get(key)
        if changes is None:
            changes = session.info[key] = {"parents": {}, "paused": {}}

            def committed(owner):
                pending = owner.info.pop(key, None)
                if pending is None:
                    return
                with self._scheduler_lock:
                    self._control_parents.update(pending["parents"])
                    for ident, value in pending["paused"].items():
                        if value:
                            self._paused_operations.add(ident)
                        else:
                            self._paused_operations.discard(ident)
                    if pending["parents"] or pending["paused"]:
                        self._pause_generation += 1
                        self._pause_cache = None
                    self._reset_operation_windows_locked()
                self._wake.set()

            def rolled_back(owner):
                owner.info.pop(key, None)

            event.listen(session, "after_commit", committed, once=True)
            event.listen(session, "after_rollback", rolled_back, once=True)
        if parents is not None:
            changes["parents"][parents[0]] = tuple(parents[1])
        if paused is not None:
            changes["paused"][paused[0]] = paused[1]

    @staticmethod
    def _paused_closure(paused, parents):
        children = {}
        for child, ancestors in parents.items():
            for ancestor in ancestors:
                children.setdefault(ancestor, []).append(child)
        effective = set(paused)
        pending = deque(paused)
        while pending:
            for child in children.get(pending.popleft(), ()):
                if child not in effective:
                    effective.add(child)
                    pending.append(child)
        return frozenset(effective)

    def _effective_paused(self):
        while True:
            with self._scheduler_lock:
                generation = self._pause_generation
                if self._pause_cache is not None and self._pause_cache[0] == generation:
                    return self._pause_cache[1]
                if not self._paused_operations:
                    effective = frozenset()
                    self._pause_cache = (generation, effective)
                    return effective
                paused = set(self._paused_operations)
                parents = {ident: tuple(values) for ident, values in self._control_parents.items()}
            effective = self._paused_closure(paused, parents)
            with self._scheduler_lock:
                if self._pause_generation == generation:
                    self._pause_cache = (generation, effective)
                    return effective

    def _allowed_query(self, query):
        allowed = self.allowed_kinds() if self.allowed_kinds else None
        return query if allowed is None else query.where(Operation.kind.in_(allowed))

    @staticmethod
    @contextmanager
    def _handler_scope(handler, context):
        with ExitStack() as stack:
            for owner in (getattr(handler, "client", None), getattr(handler, "downloader", None)):
                factory = getattr(type(owner), "task_scope", None)
                if callable(factory):
                    stack.enter_context(factory(owner, context))
            yield

    @staticmethod
    def _needs_planning(operation):
        return (operation.state in ("queued", "planning") or operation.result.get("planning_complete") is False or
                operation.state == "interrupted" and operation.result.get("planning_complete") is not True and
                operation.result.get("inventory_complete") is not True)

    def accept_submissions(self, accepting):
        with self.database.write():
            self._accepting = bool(accepting)

    def automatic_paused(self) -> bool:
        with self.database.read() as session:
            setting = session.get(ApplicationState, "automatic_paused")
            return setting is not None and setting.value is True

    def set_automatic_paused(self, paused: bool) -> bool:
        if type(paused) is not bool:
            raise ValueError("暂停设置必须是布尔值")
        with self.database.write() as session:
            setting = session.get(ApplicationState, "automatic_paused")
            if setting is None:
                session.add(ApplicationState(key="automatic_paused", value=paused))
            else:
                setting.value = paused
            self._publish_controls_after_commit(session)
        self._wake.set()
        return paused

    def operation_paused(self, operation_id):
        return operation_id in self._effective_paused()

    @staticmethod
    def _paused_in_transaction(session, operation):
        if operation.pause_requested:
            return True
        for parent_id in operation.result.get("parent_operation_ids", []):
            parent = session.get(Operation, parent_id)
            if parent is not None and parent.pause_requested:
                return True
        return False

    def set_manual_paused(self, operation_id, paused):
        if type(paused) is not bool:
            raise ValueError("暂停设置必须是布尔值")
        with self.database.write() as session:
            operation = session.get(Operation, operation_id)
            if operation is None:
                raise KeyError(operation_id)
            if operation.kind != "reconcile" or operation.source != "manual" or operation.state == "finished":
                raise ValueError("只能暂停或继续尚未结束的手动检查并补齐")
            operation.pause_requested = paused
            if not paused and operation.state == "interrupted":
                operation.state = "planning" if operation.result.get("planning_complete") is False else "ready"
            operation.updated_at = utcnow()
            operation.detail = "已请求暂停，当前步骤安全结束后停下" if paused else "已继续，等待处理未完成部分"
            result = {"operation_id": operation_id, "pause_requested": paused, "state": operation.state}
            self._publish_controls_after_commit(session, paused=(operation_id, paused))
        self._wake.set()
        return result

    def enqueue_in_transaction(self, session, kind, parameters, label, source="manual", *, parent_id=None, specs=None):
        """Register metadata and its durable follow-up under the caller's transaction."""
        if kind not in self.handlers or source not in ("manual", "automatic", "maintenance"):
            raise ValueError("不支持的任务类型或来源")
        if kind == "reconcile" and source != "manual":
            raise ValueError("全库维护只允许手动启动")
        if not self._accepting and parent_id is None:
            raise RuntimeError("图库正在切换，暂时不能提交任务")
        # An automatic check has a separate deferred scheduling intent and
        # selection policy; a manual check must not consume that paused batch.
        canonical = json.dumps([kind, parameters], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        legacy_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        automatic_check = kind == "check" and source == "automatic"
        key = hashlib.sha256((canonical + ":automatic-check").encode("utf-8")).hexdigest() if automatic_check else legacy_key
        statement = select(Operation).where(Operation.dedupe_key.in_((key, legacy_key)), Operation.state.in_(ACTIVE))
        if kind == "check":
            # Also distinguish existing unfinished records made with the former hash.
            statement = statement.where(Operation.source == "automatic" if automatic_check else Operation.source != "automatic")
        operation = session.scalar(statement)
        created = operation is None
        promoted = not created and source == "manual" and operation.source != "manual"
        if created:
            operation = Operation(kind=kind, parameters=parameters, source=source, label=label, dedupe_key=key)
            session.add(operation)
            session.flush()
            if specs is not None:
                operation.state = "ready"
                operation.result = {"inventory_complete": True}
                self.materialize_in_transaction(session, operation.id, specs)
        elif source == "manual":
            operation.source = "manual"
        inherited_round = False
        if parent_id is not None:
            parent = session.get(Operation, parent_id)
            children = list(parent.result.get("child_operation_ids", []))
            if operation.id not in children:
                children.append(operation.id)
                parent.result = {**parent.result, "child_operation_ids": children}
            parents = list(operation.result.get("parent_operation_ids", []))
            if parent_id not in parents:
                parents.append(parent_id)
                operation.result = {**operation.result, "parent_operation_ids": parents}
                self._publish_controls_after_commit(session, parents=(operation.id, parents))
            inherited_round = operation.round_id is None and parent.round_id is not None
            operation.round_id = operation.round_id or parent.round_id
        if created or promoted or inherited_round:
            self._publish_controls_after_commit(session)
        self._wake.set()
        return {"operation_id": operation.id, "created": created, "state": operation.state, "round_id": operation.round_id}

    def materialize_in_transaction(self, session, operation_id, specs):
        specs = list(specs)
        if not specs:
            session.flush()
            self._wake.set()
            return
        needed = {spec.key for spec in specs} | {key for spec in specs for key in spec.depends_on}
        existing = set()
        keys = list(needed)
        for offset in range(0, len(keys), 500):
            existing.update(session.scalars(select(OperationItem.item_key).where(
                OperationItem.operation_id == operation_id, OperationItem.item_key.in_(keys[offset:offset + 500]))))
        if all(spec.key in existing for spec in specs):
            session.flush()
            self._wake.set()
            return
        position = self._max_item_position(session, operation_id)
        position = 0 if position is None else position + 1
        added = Counter()
        for spec in specs:
            if spec.key in existing:
                continue
            if any(key not in existing for key in spec.depends_on):
                raise ValueError("清单依赖顺序不合法")
            session.add(OperationItem(operation_id=operation_id, position=position, item_key=spec.key,
                kind=spec.kind, target=spec.target, phase=spec.phase, unit=spec.unit, depends_on=list(spec.depends_on),
                lane=self._lane(spec)))
            existing.add(spec.key)
            added[spec.phase + ":" + spec.unit] += 1
            position += 1
        session.flush()
        operation = session.get(Operation, operation_id)
        progress = dict(operation.result.get("step_progress", {}))
        for key, count in added.items():
            if key in progress:
                progress[key] = {**progress[key], "total": progress[key]["total"] + count}
        if progress:
            operation.result = {**operation.result, "step_progress": progress}
        self._wake.set()

    @classmethod
    def _max_item_position(cls, session, operation_id):
        # Position is the fourth index column, after state and lane. Seek the
        # end of each actual prefix instead of visiting all settled rows.
        states = cls._item_states(operation_id)
        first_lane = select(func.min(OperationItem.lane)).where(
            OperationItem.operation_id == operation_id,
            OperationItem.state == states.c.state).correlate(states).scalar_subquery()
        lanes = select(states.c.state, first_lane.label("lane")).where(
            states.c.state.is_not(None)).cte("item_lanes", recursive=True)
        next_lane = select(func.min(OperationItem.lane)).where(
            OperationItem.operation_id == operation_id, OperationItem.state == lanes.c.state,
            OperationItem.lane > lanes.c.lane).correlate(lanes).scalar_subquery()
        lanes = lanes.union_all(select(lanes.c.state, next_lane).where(lanes.c.lane.is_not(None)))
        null_position = select(func.max(OperationItem.position)).where(
            OperationItem.operation_id == operation_id, OperationItem.state == states.c.state,
            OperationItem.lane.is_(None)).correlate(states).scalar_subquery()
        lane_position = select(func.max(OperationItem.position)).where(
            OperationItem.operation_id == operation_id, OperationItem.state == lanes.c.state,
            OperationItem.lane == lanes.c.lane).correlate(lanes).scalar_subquery()
        positions = union_all(
            select(null_position.label("position")).select_from(states).where(states.c.state.is_not(None)),
            select(lane_position.label("position")).select_from(lanes).where(lanes.c.lane.is_not(None)),
        ).subquery()
        return session.scalar(select(func.max(positions.c.position)))

    def submit(self, kind: str, parameters: dict, label: str, source="manual") -> dict:
        with self.database.write() as session:
            return self.enqueue_in_transaction(session, kind, parameters, label, source)

    def recover(self):
        """Call after filesystem journal recovery, before starting the sole worker."""
        with self.database.write() as session:
            for item in session.scalars(select(OperationItem).where(OperationItem.state == "running")):
                item.state = "pending"
            for operation in session.scalars(self._allowed_query(select(Operation).where(Operation.state.in_(("planning", "running", "interrupted"))))):
                round_record = session.get(TaskRound, operation.round_id) if operation.round_id else None
                operation.state = "planning" if operation.result.get("planning_complete") is False else "ready" if round_record and round_record.frozen_at else "planning"
                operation.detail = "上次运行中断，正在核对并继续"
        self.worker_error = None

    def start(self):
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = [threading.Thread(target=self._worker, args=(lane,),
            name=f"archive-{lane}-{index}", daemon=True)
            for index, lane in enumerate(("check",) * self.check_workers + ("download", "download", "local"))]
        self._thread = self._threads[0]  # Compatibility for older callers.
        for thread in self._threads:
            thread.start()
        self._wake.set()

    def request_stop(self):
        # Called directly from the tray: do not wait for an in-flight DB writer.
        self._accepting = False
        self._stop.set()
        self._wake.set()

    def stop(self, timeout=5):
        self.request_stop()
        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in self._threads:
            thread.join(timeout=None if deadline is None else max(0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def _worker(self, lane):
        from ..request_policy import ShutdownRequested
        while not self._stop.is_set():
            try:
                if lane == "check" and self.maintenance is not None and self._maintenance_lock.acquire(blocking=False):
                    try:
                        self.maintenance.tick()
                        self.maintenance_error = None
                    except Exception as exc:
                        self.maintenance_error = type(exc).__name__
                    finally:
                        self._maintenance_lock.release()
                if self._step(lane):
                    continue
                self._wake.wait(0.1)
                self._wake.clear()
            except ShutdownRequested:
                return
            except LocalReadError:
                # Local capacity/budget is expected pressure, not corrupt state.
                self._wake.wait(0.1)
            except Exception as exc:
                self.worker_error = type(exc).__name__
                log.error("任务执行器中断，未完成记录已保留", exc_info=False)
                self._stop.set()

    @staticmethod
    def _lane(item):
        if item.phase == "checking" or item.kind in ("metadata_artist", "metadata_work", "bookmark_artist", "bookmark_page", "discover_page", "discover_work"):
            return "check"
        return "download" if item.kind == "download" or item.phase == "downloading" and item.kind not in ("timing", "error") else "local"

    @staticmethod
    def _resource(item):
        target = item.target
        if target.get("asset_id"):
            return ("asset", target["asset_id"])
        if target.get("record_id"):
            return ("derivative", target["record_id"])
        return ("item", item.id)

    def _item_resources(self, session, item):
        resources = {self._resource(item)}
        work_id = item.target.get("work_id")
        if work_id and item.kind.startswith("reconcile_"):
            resources.add(("work", work_id))
            if item.kind in ("reconcile_prepare", "reconcile_pair", "reconcile_snapshot"):
                resources.update(("asset", value) for value in session.scalars(select(Asset.id).where(Asset.work_id == work_id)))
                resources.update(("derivative", value) for value in session.scalars(select(Derivative.id).where(Derivative.work_id == work_id)))
        if work_id and item.kind in ("gif", "thumbnail", "ugoira_pair"):
            work = session.get(Work, work_id)
            if item.kind in ("gif", "ugoira_pair"):
                ids = session.scalars(select(Asset.id).where(Asset.work_id == work_id, Asset.role.in_(("ugoira_zip", "ugoira_timing"))))
                resources.update(("asset", value) for value in ids)
            elif work and work.type == "ugoira":
                ids = session.scalars(select(Derivative.id).where(Derivative.work_id == work_id, Derivative.kind == "gif", Derivative.spec_key == "v1"))
                resources.update(("derivative", value) for value in ids)
            else:
                ids = session.scalars(select(Asset.id).where(Asset.work_id == work_id, Asset.role == "image", Asset.page_index == 0))
                resources.update(("asset", value) for value in ids)
        return resources

    @staticmethod
    def _candidate_window(session, operation, lane, after):
        columns = (OperationItem.id, OperationItem.position, OperationItem.kind,
            OperationItem.phase, OperationItem.lane, OperationItem.depends_on, OperationItem.not_before)
        query = select(*columns).where(OperationItem.operation_id == operation.id,
            OperationItem.state == "pending", OperationItem.position > after)
        if lane == "check" and operation.kind in ("check", "bookmarks"):
            # Discovery handlers deliberately process already fetched works
            # before fetching another page. Reconciliation has no such items.
            check = or_(OperationItem.phase == "checking", OperationItem.kind.in_(
                ("metadata_artist", "metadata_work", "bookmark_artist", "bookmark_page", "discover_page", "discover_work")))
            keys = func.json_each(OperationItem.depends_on).table_valued("value").alias("candidate_dependencies")
            dependency = aliased(OperationItem)
            state = select(dependency.state).where(dependency.operation_id == operation.id,
                dependency.item_key == keys.c.value).correlate(keys).scalar_subquery()
            waiting = exists(select(1).select_from(keys).where(or_(state.is_(None), ~state.in_(SETTLED))).correlate(OperationItem))
            return list(session.execute(query.where(or_(OperationItem.lane == lane,
                and_(OperationItem.lane.is_(None), check)), ~waiting,
                or_(OperationItem.not_before.is_(None), OperationItem.not_before <= utcnow())).order_by(
                case((OperationItem.kind == "discover_work", 0), else_=1), OperationItem.position).limit(64)))
        # Equality on lane lets the existing composite index supply position
        # order. Separate old/new streams avoid an OR plus full temporary sort.
        current = list(session.execute(query.where(OperationItem.lane == lane).order_by(OperationItem.position).limit(64)))
        legacy = list(session.execute(query.where(OperationItem.lane.is_(None)).order_by(OperationItem.position).limit(64)))
        return sorted(current + legacy, key=lambda item: item.position)[:64]

    def _step(self, lane, round_id=None):
        """Select a bounded indexed window before entering the atomic claim."""
        started = time.monotonic()
        self._last_scan_more = False
        with self._scheduler_lock:
            planning, checking, resources = set(self._planning), set(self._checking), set(self._resources)
            cursors = dict(self._candidate_after)
            epochs = dict(self._candidate_epochs)
            claim_sequence = self._claim_sequence
            check_turns = dict(self._check_turns)
        if not self._accepting or self._stop.is_set():
            return False
        if lane == "check" and round_id is None:
            with self.database.read() as session:
                waiting = self._allowed_query(select(Operation.id).where(
                    Operation.state.in_(("queued", "ready", "planning", "interrupted")),
                    Operation.round_id.is_(None), Operation.pause_requested.is_(False)))
                paused = session.get(ApplicationState, "automatic_paused")
                if paused and paused.value is True:
                    waiting = waiting.where(or_(Operation.source != "automatic", Operation.started_at.is_not(None)))
                admission_pending = session.scalar(waiting.limit(1)) is not None
            if admission_pending:
                with self.database.write() as session:
                    self._claim_round_in_transaction(session)
        claim = None
        operation_key = (lane, round_id)
        with self._scheduler_lock:
            operation_after = self._operation_after.get(operation_key)
            operation_epoch = self._operation_epochs.get(operation_key, 0)
        paused_ids = self._effective_paused()
        with self.database.read() as session:
            query = self._allowed_query(select(Operation).where(Operation.state.in_(("queued", "planning", "ready", "running", "interrupted")),
                Operation.pause_requested.is_(False)))
            if round_id is not None:
                query = query.where(Operation.round_id == round_id)
            if paused_ids:
                query = query.where(~Operation.id.in_(paused_ids))
            priority = case((Operation.source == "manual", 0), else_=1)
            window = query
            if operation_after is not None:
                window = window.where(tuple_(Operation.last_claim_seq, priority, Operation.created_at, Operation.id) > operation_after)
            scanned = list(session.scalars(window.order_by(Operation.last_claim_seq,
                priority, Operation.created_at, Operation.id).limit(64)))
            operations = list(scanned)
            if claim_sequence % 8 == 7:
                maintenance = session.scalar(query.where(Operation.source == "maintenance",
                    Operation.round_id.is_not(None)).order_by(Operation.last_claim_seq,
                    Operation.created_at, Operation.id).limit(1))
                if maintenance:
                    operations = [maintenance] + [op for op in operations if op.id != maintenance.id][:63]
            paused = session.get(ApplicationState, "automatic_paused")
            candidates = []
            deferred = []
            for operation in operations:
                if self._paused_in_transaction(session, operation):
                    continue
                if lane == "check" and operation.id in planning | checking:
                    continue
                not_before = operation.result.get("planning_not_before")
                plan_ready = (lane == "check" and operation.round_id is not None and self._needs_planning(operation)
                    and not (not_before and not_before > utcnow().isoformat()))
                if plan_ready:
                    if operation.source == "automatic" and operation.started_at is None and paused and paused.value is True:
                        deferred.append(operation.id)
                        continue
                    if check_turns.get(operation.id, "plan") == "plan":
                        candidates.append(("plan", operation.id))
                        break
                if operation.state == "queued":
                    continue
                if lane == "check" and operation.source == "automatic" and not (paused and paused.value is True) and any(
                    other.source == "automatic" and other.round_id == operation.round_id and other.started_at is None
                    for other in operations):
                    # Freeze every admitted automatic selection before its
                    # batch starts executing. Later pause only affects future batches.
                    continue
                cursor_key = (operation.id, lane)
                after = cursors.get(cursor_key, -1)
                epoch = epochs.get(cursor_key, 0)
                # Bound candidates before dependency checks. Legacy NULL lanes
                # are classified inside the window rather than scanning the
                # whole remaining manifest for an empty channel.
                for _ in range(32):
                    items = self._candidate_window(session, operation, lane, after)
                    if not items:
                        self._advance_candidate_cursor(cursor_key, epoch, None)
                        break
                    after = items[-1].position
                    epoch = self._advance_candidate_cursor(cursor_key, epoch, after)
                    now = utcnow()
                    eligible = [item for item in items if (item.lane == lane or item.lane is None and self._lane(item) == lane)
                        and (item.not_before is None or item.not_before <= now)]
                    keys = list({key for item in eligible for key in item.depends_on})
                    states = {}
                    for offset in range(0, len(keys), 500):
                        states.update(session.execute(select(OperationItem.item_key, OperationItem.state).where(
                            OperationItem.operation_id == operation.id, OperationItem.item_key.in_(keys[offset:offset + 500]))).all())
                    for candidate in eligible:
                        if any(states.get(key) not in SETTLED for key in candidate.depends_on):
                            continue
                        item = session.get(OperationItem, candidate.id)
                        resource = self._item_resources(session, item)
                        if resource & resources:
                            continue
                        candidates.append(("item", operation.id, item.id, resource))
                        break
                    if candidates:
                        break
                else:
                    self._last_scan_more = True
                # Give planning a turn when the checking window has no ready
                # item. Neither planning nor checking waits for the other to
                # drain its complete manifest, including during cooldown.
                if not candidates and plan_ready:
                    candidates.append(("plan", operation.id))
                if candidates:
                    break
        if not candidates:
            after = ((scanned[-1].last_claim_seq, 0 if scanned[-1].source == "manual" else 1,
                      scanned[-1].created_at, scanned[-1].id) if len(scanned) == 64 else None)
            advanced = self._advance_operation_cursor(operation_key, operation_epoch, after)
            self._last_scan_more = self._last_scan_more or after is not None or not advanced
        # Finalization belongs to planning/item completion and deferred round
        # release below. An empty lane has no state change to commit; a later
        # submission or dependency settlement wakes the workers normally.
        if not candidates and not deferred:
            return False
        write_started = time.monotonic()
        try:
            with self.database.write() as session:
                if not self._accepting or self._stop.is_set():
                    return False
                for operation_id in deferred:
                    operation = session.get(Operation, operation_id)
                    if operation.source == "automatic" and operation.started_at is None:
                        previous_round, operation.round_id = operation.round_id, None
                        session.flush()
                        if previous_round:
                            self._finalize_round(session, previous_round)
                for candidate in candidates:
                    operation = session.get(Operation, candidate[1])
                    if (operation is None or operation.state == "finished" or self.operation_paused(operation.id)
                            or self._paused_in_transaction(session, operation) or not self.kind_allowed(operation.kind)):
                        continue
                    if candidate[0] == "plan":
                        not_before = operation.result.get("planning_not_before")
                        if (not self._needs_planning(operation) or
                                not_before and not_before > utcnow().isoformat()):
                            continue
                        with self._scheduler_lock:
                            if operation.id in self._planning | self._checking:
                                continue
                            if self._check_turns.get(operation.id, "plan") != check_turns.get(operation.id, "plan"):
                                continue
                            self._planning.add(operation.id)
                            self._check_turns[operation.id] = "item"
                        operation.state = "planning"
                        operation.started_at = operation.started_at or utcnow()
                        claim = candidate
                    else:
                        item = session.get(OperationItem, candidate[2])
                        if item.state != "pending" or item.not_before and item.not_before > utcnow():
                            continue
                        states = dict(session.execute(select(OperationItem.item_key, OperationItem.state).where(
                            OperationItem.operation_id == operation.id, OperationItem.item_key.in_(item.depends_on))).all()) if item.depends_on else {}
                        if any(states.get(key) not in SETTLED for key in item.depends_on):
                            continue
                        resource = self._item_resources(session, item)
                        with self._scheduler_lock:
                            if resource & self._resources or lane == "check" and operation.id in self._planning | self._checking:
                                continue
                            if lane == "check" and self._check_turns.get(operation.id, "plan") != check_turns.get(operation.id, "plan"):
                                continue
                            self._resources.update(resource)
                            if lane == "check":
                                self._checking.add(operation.id)
                                self._check_turns[operation.id] = "plan"
                            self._candidate_after.pop((operation.id, lane), None)
                            self._candidate_epochs[operation.id, lane] = self._candidate_epochs.get((operation.id, lane), 0) + 1
                        item.state, item.attempts = "running", item.attempts + 1
                        operation.state, operation.started_at = "running", operation.started_at or utcnow()
                        operation.result = {**operation.result, "lane": lane}
                        claim = ("item", operation.id, item, resource, any(states.get(key) != "succeeded" for key in item.depends_on))
                        self._report_step_in_transaction(session, operation, item.phase, item.unit)
                    with self._scheduler_lock:
                        self._claim_sequence = max(self._claim_sequence, operation.last_claim_seq) + 1
                        sequence = self._claim_sequence
                        self._reset_operation_windows_locked(operation_key)
                    if candidate[0] != "plan":
                        operation.last_claim_seq = sequence
                    break
        except BaseException:
            if claim is not None:
                with self._scheduler_lock:
                    if claim[0] == "plan":
                        self._planning.discard(claim[1])
                    else:
                        self._resources.difference_update(claim[3])
                        self._checking.discard(claim[1])
                    if lane == "check":
                        if claim[1] in check_turns:
                            self._check_turns[claim[1]] = check_turns[claim[1]]
                        else:
                            self._check_turns.pop(claim[1], None)
            raise
        if claim and claim[0] == "item":
            # Trust the baseline only after the transaction that prepared it
            # committed. Failed writes must leave recovery free to recount.
            with self._scheduler_lock:
                self._validated_phases.add((claim[1], claim[2].phase + ":" + claim[2].unit))
        log.debug("task_claim build=%s lane=%s operation=%s step=%s elapsed_ms=%.3f write_ms=%.3f", __build__, lane,
            claim[1] if claim else "none", claim[2].id if claim and claim[0] == "item" else "plan" if claim else "none",
            (time.monotonic() - started) * 1000, (time.monotonic() - write_started) * 1000)
        if claim is None:
            return False
        if claim[0] == "plan":
            try:
                self._plan(claim[1])
            except LocalReadError:
                self._defer_plan(claim[1], 0.2, "等待本地读取容量")
            finally:
                with self._scheduler_lock:
                    self._planning.discard(claim[1])
            return True
        _, operation_id, item, resource, dependency_failed = claim
        try:
            self._execute_item(operation_id, item, dependency_failed, progress_prepared=True)
        except LocalReadError:
            self._defer_item(item.id, operation_id, 0.2, "等待本地读取容量", refund_attempt=True)
        finally:
            with self._scheduler_lock:
                self._resources.difference_update(resource)
                self._checking.discard(operation_id)
            with self.database.write() as session:
                self._finalize(session, [operation_id])
            self._wake.set()
        return True

    def _claim_round_in_transaction(self, session):
        paused = session.get(ApplicationState, "automatic_paused")
        query = self._allowed_query(select(Operation).where(Operation.state.in_(("queued", "ready", "planning", "interrupted")),
            Operation.round_id.is_(None), Operation.pause_requested.is_(False)))
        if paused and paused.value is True:
            query = query.where(or_(Operation.source != "automatic", Operation.started_at.is_not(None)))
        waiting = list(session.scalars(query.order_by(case((Operation.source == "manual", 0),
            (Operation.source == "maintenance", 2), else_=1), Operation.created_at, Operation.id).limit(64)))
        with self._scheduler_lock:
            admissions = self._round_admissions
        if any(op.source != "maintenance" for op in waiting):
            if admissions % 2 == 0:
                waiting = [op for op in waiting if op.source != "maintenance"]
            else:
                # Reserve a place even when the bounded foreground window is
                # full. Continuous submissions must not hide maintenance.
                maintenance = session.scalar(query.where(Operation.source == "maintenance")
                    .order_by(Operation.created_at, Operation.id).limit(1))
                if maintenance and all(op.id != maintenance.id for op in waiting):
                    waiting = waiting[:63] + [maintenance]
        if not waiting:
            return None
        with self._scheduler_lock:
            self._round_admissions += 1
        current = TaskRound()
        session.add(current)
        session.flush()
        for op in waiting:
            op.round_id = current.id
        return current.id

    def _claim_round(self):
        with self.database.write() as session:
            eligible_rounds = self._allowed_query(select(Operation.round_id).where(Operation.state.in_(ACTIVE)))
            current = session.scalar(select(TaskRound).where(TaskRound.state.in_(("planning", "running")),
                TaskRound.id.in_(eligible_rounds)).order_by(TaskRound.created_at))
            return current.id if current else self._claim_round_in_transaction(session)

    def _plan(self, operation_id):
        from ..request_policy import ShutdownRequested
        with self.database.write() as session:
            operation = session.get(Operation, operation_id)
            if not self.kind_allowed(operation.kind):
                return False
            paused = session.get(ApplicationState, "automatic_paused")
            if (operation.source == "automatic" and operation.state == "queued"
                    and operation.started_at is None and paused is not None and paused.value is True):
                # A pause may arrive after round selection but before this batch starts.
                operation.round_id = None
                return False
            operation.state, operation.phase = "planning", "checking"
            operation.result = {**operation.result, "planning_complete": False}
            operation.started_at = operation.started_at or utcnow()
            kind, parameters = operation.kind, operation.parameters
        context = TaskContext(self, operation_id, kind=kind)
        failure = None
        try:
            context.checkpoint()
            handler = self.handlers[kind]
            with self._handler_scope(handler, context):
                if callable(getattr(handler, "plan_batch", None)):
                    with self.database.read() as session:
                        cursor = dict(session.get(Operation, operation_id).result.get("planning_cursor", {}))
                    batch = handler.plan_batch(parameters, context, cursor)
                    specs, complete = batch.specs, batch.complete
                else:
                    specs, complete = handler.plan(parameters, context), True
            seen = set()
            for spec in specs:
                if spec.key in seen:
                    raise ValueError("清单重复或依赖顺序不合法")
                seen.add(spec.key)
        except (AccountChanged, RequestDeferred, LocalReadError) as exc:
            self._defer_plan(operation_id, getattr(exc, "retry_after", 0.2),
                getattr(exc, "user_message", "等待本地容量或新的账号连接"))
            return False
        except (ShutdownRequested, ManualPauseRequested, ConnectionRequired):
            with self.database.write() as session:
                operation = session.get(Operation, operation_id)
                operation.state, operation.detail = ("interrupted", "退出前已保存进度，下次启动继续") if self._stop.is_set() else ("planning", "已暂停，清单进度已保存" if self.kind_allowed(kind) else "等待连接 Pixiv，任务进度已保留")
            return False
        except (Exception, TaskRemoteRetry) as exc:
            if not self.kind_allowed(kind):
                return False
            if isinstance(exc, TaskRemoteRetry):
                exc = exc.error
            failure = safe_failure(exc)
            if failure.get("retryable"):
                with self.database.read() as session:
                    attempts = session.get(Operation, operation_id).result.get("planning_retry_attempts", 0)
                unsent = failure.get("status") is None and failure.get("failure_code") in ("api_rate_limit", "image_rate_limit")
                if unsent or attempts < 2:
                    self._defer_plan(operation_id, failure.get("retry_after") or 2 ** (attempts + 1),
                        "远端暂时未响应，清单进度已保存", retry_attempts=attempts + int(not unsent))
                    return False
            complete = True
            specs = [ItemSpec("inventory-failed", "inventory-failed", {}, "checking")]
        with self.database.write() as session:
            operation = session.get(Operation, operation_id)
            operation.result = {**{key: value for key, value in operation.result.items() if key not in ("planning_not_before", "planning_retry_attempts")}, "planning_complete": complete, "planning_cursor": batch.cursor if callable(getattr(self.handlers[kind], "plan_batch", None)) and failure is None else {}, "inventory_complete": complete and failure is None and not operation.result.get("inventory_failed") and not any(
                spec.kind == "error" and spec.phase == "checking" for spec in specs), "inventory": {
                "phase": operation.phase, "unit": operation.phase_unit,
                "done": operation.phase_done, "total": operation.phase_total,
            }}
            self.materialize_in_transaction(session, operation_id, specs)
            if failure:
                for stored in session.scalars(select(OperationItem).where(OperationItem.operation_id == operation_id, OperationItem.item_key == "inventory-failed")):
                    stored.state, stored.result = "failed", failure
            running_item = session.scalar(select(OperationItem.id).where(OperationItem.operation_id == operation_id, OperationItem.state == "running").limit(1))
            operation.state = "planning" if not complete else "running" if running_item else "ready"
            if not complete:
                with self._scheduler_lock:
                    operation.last_claim_seq = self._claim_sequence
            self._finalize(session, [operation_id])
        return True

    def run_round(self) -> bool:
        """Synchronous driver used by isolated callers; production uses lane workers."""
        if not self._run_lock.acquire(blocking=False):
            return False
        try:
            round_id = self._claim_round()
            if round_id is None:
                return False
            idle_lanes = set()
            while not self._stop.is_set():
                progressed = False
                for lane in ("check", "download", "local"):
                    if lane in idle_lanes:
                        continue
                    handled = self._step(lane, round_id)
                    if handled:
                        progressed = True
                    elif not self._last_scan_more:
                        idle_lanes.add(lane)
                if progressed:
                    idle_lanes.clear()
                with self.database.read() as session:
                    current = session.get(TaskRound, round_id)
                    if current.state == "finished":
                        return True
                if not progressed and len(idle_lanes) == 3:
                    return False
            return False
        finally:
            self._run_lock.release()

    @staticmethod
    def _item_states(operation_id):
        # Walk distinct state values through the existing operation/state
        # index. DISTINCT itself scans every row on SQLite; successive MIN
        # probes seek once per state and retain even unfamiliar saved states.
        states = select(func.min(OperationItem.state).label("state")).where(
            OperationItem.operation_id == operation_id).cte("item_states", recursive=True)
        next_state = select(func.min(OperationItem.state)).where(
            OperationItem.operation_id == operation_id, OperationItem.state > states.c.state).correlate(states).scalar_subquery()
        return states.union_all(select(next_state).where(states.c.state.is_not(None)))

    @classmethod
    def _phase_counts(cls, session, operation_id, phase, unit):
        states = cls._item_states(operation_id)
        return dict(session.execute(select(OperationItem.state, func.count()).where(
            OperationItem.operation_id == operation_id,
            OperationItem.state.in_(select(states.c.state).where(states.c.state.is_not(None))),
            OperationItem.phase == phase, OperationItem.unit == unit).group_by(OperationItem.state)).all())

    def _step_progress(self, session, operation, phase, unit, *, initialize=False):
        key = phase + ":" + unit
        progress = dict(operation.result.get("step_progress", {}))
        value = progress.get(key)
        with self._scheduler_lock:
            trusted = (operation.id, key) in self._validated_phases
        if value is None or not trusted:
            started = time.monotonic()
            counts = self._phase_counts(session, operation.id, phase, unit)
            value = {"done": sum(counts.get(state, 0) for state in SETTLED), "total": sum(counts.values())}
            log.debug("task_phase_baseline operation=%s phase=%s rows=%s elapsed_ms=%.3f", operation.id,
                key, value["total"], (time.monotonic() - started) * 1000)
            if initialize:
                progress[key] = value
                operation.result = {**operation.result, "step_progress": progress}
        return value

    def _report_step_in_transaction(self, session, operation, phase, unit):
        progress = self._step_progress(session, operation, phase, unit, initialize=True)
        operation.phase, operation.phase_unit = phase, unit
        operation.phase_done, operation.phase_total = progress["done"], progress["total"]
        operation.detail, operation.updated_at = "正在处理", utcnow()
        snapshots = dict(operation.result.get("phase_snapshots", {}))
        snapshots[phase + ":" + unit] = {"phase": phase, "unit": unit, **progress}
        operation.result = {**operation.result, "phase_snapshots": snapshots}

    @staticmethod
    def record_settlement_in_transaction(session, item, previous_state):
        if (previous_state in SETTLED) == (item.state in SETTLED):
            return
        operation = session.get(Operation, item.operation_id)
        progress = dict(operation.result.get("step_progress", {}))
        key = item.phase + ":" + item.unit
        if key in progress:
            progress[key] = {**progress[key], "done": progress[key]["done"] + (1 if item.state in SETTLED else -1)}
            operation.result = {**operation.result, "step_progress": progress}

    def _execute_item(self, operation_id, item, dependency_failed=False, *, progress_prepared=False):
        from ..request_policy import ShutdownRequested
        phase_key = item.phase + ":" + item.unit
        with self.database.read() as session:
            kind = session.get(Operation, operation_id).kind
        context = TaskContext(self, operation_id, item.id, kind=kind)
        context.allow_token_refresh = not item.result.get("auth_refreshed", False)
        # Claim happened just before shutdown: leave the item pending rather than
        # classifying cancellation as a failed or successful attempt.
        try:
            if progress_prepared:
                context.checkpoint()
            else:
                context.report_step(item.phase, item.unit)
        except (ShutdownRequested, ManualPauseRequested, ConnectionRequired) as exc:
            self._interrupt_item(item.id, operation_id, refund_attempt=isinstance(exc, ConnectionRequired))
            return
        if dependency_failed:
            with self.database.read() as session:
                dependency_states = dict(session.execute(select(OperationItem.item_key, OperationItem.state).where(
                    OperationItem.operation_id == operation_id, OperationItem.item_key.in_(item.depends_on))).all())
            superseded = all(dependency_states.get(key) in ("succeeded", "skipped") for key in item.depends_on)
            result = ItemResult("skipped" if superseded else "not_executed", {"message": "前置保存决定已改变，此项不再执行" if superseded else "前置步骤未成功，此项尚未执行"})
        else:
            try:
                spec = ItemSpec(item.item_key, item.kind, item.target, item.phase, item.unit, tuple(item.depends_on))
                handler = self.handlers[kind]
                with self._handler_scope(handler, context):
                    result = handler.execute(spec, context)
                if not isinstance(result, ItemResult) or result.state not in SETTLED and not (item.kind == "bookmark_artist" and result.state == "yielded"):
                    raise TypeError("任务必须返回明确的处理结果")
            except (ShutdownRequested, ManualPauseRequested, ConnectionRequired) as exc:
                self._interrupt_item(item.id, operation_id, refund_attempt=isinstance(exc, ConnectionRequired))
                return
            except (AccountChanged, RequestDeferred, LocalReadError) as exc:
                self._defer_item(item.id, operation_id, getattr(exc, "retry_after", 0.2),
                    getattr(exc, "user_message", "等待本地容量或新的账号连接"),
                    refund_attempt=isinstance(exc, AccountChanged) or context.requests_sent == 0,
                    detail={"auth_refreshed": True} if getattr(context, "auth_refreshed", False) else None)
                return
            except (Exception, TaskRemoteRetry) as exc:
                if isinstance(exc, TaskRemoteRetry):
                    exc = exc.error
                if not self.kind_allowed(kind):
                    self._interrupt_item(item.id, operation_id, refund_attempt=True)
                    return
                detail = safe_failure(exc)
                unsent = detail.get("status") is None and detail.get("failure_code") in ("api_rate_limit", "image_rate_limit")
                if detail.get("retryable") and (unsent or item.attempts < 3):
                    self._defer_item(item.id, operation_id, detail.get("retry_after") or 2 ** item.attempts,
                        "远端暂时未响应，已保存进度，等待重试", refund_attempt=unsent, detail=detail)
                    return
                result = ItemResult("failed", detail)
        if result.state == "yielded":
            with self.database.write() as session:
                stored = session.get(OperationItem, item.id)
                stored.state, stored.result = "pending", result.detail
                stored.attempts = 0  # Retry budget belongs to the next persisted page.
                stored.not_before = None
            return
        with self.database.write() as session:
            stored = session.get(OperationItem, item.id)
            previous_state = stored.state
            stored.state, stored.result = result.state, result.detail
            self.record_settlement_in_transaction(session, stored, previous_state)
            session.flush()
            operation = session.get(Operation, operation_id)
            if result.state == "failed" and (item.kind in ("discover_page", "bookmark_page") or
                    item.kind == "bookmark_artist" and "unconfirmed_works" not in result.detail):
                operation.result = {**operation.result, "inventory_failed": True, "inventory_complete": False}
            progress = self._step_progress(session, operation, item.phase, item.unit)
            done, total = progress["done"], progress["total"]
            if (operation.phase, operation.phase_unit) == (item.phase, item.unit):
                operation.phase_done, operation.phase_total = done, total
            operation.updated_at = utcnow()
            snapshots = dict(operation.result.get("phase_snapshots", {}))
            snapshots[phase_key] = {"phase": item.phase, "unit": item.unit, "done": done, "total": total}
            current_items = dict(operation.result.get("current_items", {}))
            current_items.pop(item.id, None)
            operation.result = {**{k: v for k, v in operation.result.items() if k != "current_item"}, "current_items": current_items, "phase_snapshots": snapshots}
            if current_items:
                operation.result = {**operation.result, "current_item": next(iter(current_items.values()))}

    def _defer_plan(self, operation_id, seconds, message, *, retry_attempts=None):
        with self.database.write() as session:
            operation = session.get(Operation, operation_id)
            operation.state, operation.detail = "planning", message
            result = {**operation.result, "planning_not_before": (utcnow() + timedelta(seconds=seconds)).isoformat()}
            if retry_attempts is not None:
                result["planning_retry_attempts"] = retry_attempts
            operation.result = result

    def _defer_item(self, item_id, operation_id, seconds, message, *, refund_attempt=False, detail=None):
        self._interrupt_item(item_id, operation_id, refund_attempt=refund_attempt,
            not_before=utcnow() + timedelta(seconds=max(0.05, seconds)), message=message, detail=detail)

    def _interrupt_item(self, item_id, operation_id, *, refund_attempt=False, not_before=None, message=None, detail=None):
        with self.database.write() as session:
            item = session.get(OperationItem, item_id)
            item.state, item.not_before = "pending", not_before
            if detail is not None:
                item.result = {**item.result, **detail}
            if refund_attempt:
                item.attempts = max(0, item.attempts - 1)
            operation = session.get(Operation, operation_id)
            operation.state, operation.detail = ("interrupted", "退出前已保存进度，下次启动继续") if self._stop.is_set() else ("ready" if operation.result.get("planning_complete") is not False else "planning", "已保存进度，等待继续")
            if message:
                operation.detail = message
            current_items = dict(operation.result.get("current_items", {}))
            current_items.pop(item_id, None)
            operation.result = {**{key: value for key, value in operation.result.items() if key != "current_item"},
                                "current_items": current_items}

    def _finalize(self, session, operation_ids):
        session.flush()
        pending = list(operation_ids)
        seen = set()
        rounds = set()
        while pending:
            operation_id = pending.pop(0)
            if operation_id in seen:
                continue
            seen.add(operation_id)
            operation = session.get(Operation, operation_id)
            if operation is None:
                continue
            if operation.round_id:
                rounds.add(operation.round_id)
            if operation.state not in ("ready", "running") or operation.result.get("planning_complete") is False:
                continue
            states = self._item_states(operation.id)
            unfinished = session.scalar(select(states.c.state).where(~states.c.state.in_(SETTLED)).limit(1))
            if unfinished is not None:
                continue
            child_counts = None
            if operation.result.get("child_operation_ids"):
                parent_record = aliased(Operation)
                child_keys = func.json_each(parent_record.result, "$.child_operation_ids").table_valued("value").alias("child_keys")
                child_query = select(child_keys.c.value).select_from(parent_record).join(child_keys, true()).outerjoin(Operation, Operation.id == child_keys.c.value).where(parent_record.id == operation.id)
                unfinished_child = session.scalar(child_query.where(or_(Operation.id.is_(None), Operation.state != "finished")).limit(1))
                if unfinished_child:
                    operation.detail = "发现已完成，等待保存与本地处理结案"
                    operation.state = "ready"
                    operation.result = {**operation.result, "lane": None}
                    continue
                # Aggregate persisted child results once, after the SQL completion gate.
                child_counts = session.execute(select(*[func.coalesce(func.sum(Operation.result["counts"][state].as_integer()), 0) for state in SETTLED]).select_from(parent_record).join(child_keys, true()).join(Operation, Operation.id == child_keys.c.value).where(parent_record.id == operation.id)).one()
            counts = dict(session.execute(select(OperationItem.state, func.count()).where(OperationItem.operation_id == operation.id).group_by(OperationItem.state)).all())
            aggregate = Counter(counts)
            if child_counts:
                aggregate.update({state: count for state, count in zip(SETTLED, child_counts) if count})
            combined = dict(aggregate)
            result = outcome(combined)
            operation.result = {**operation.result, **combined, "counts": combined, "outcome": result, "lane": None}
            operation.needs_attention = result in ("partial_failure", "failed")
            operation.state, operation.finished_at = "finished", utcnow()
            phase_keys = tuple(operation.result.get("step_progress", {}))
            with self._scheduler_lock:
                self._check_turns.pop(operation_id, None)
                for key in phase_keys:
                    self._validated_phases.discard((operation_id, key))
            session.flush()
            pending.extend(operation.result.get("parent_operation_ids", []))
        for round_id in rounds:
            self._finalize_round(session, round_id)

    def _finalize_round(self, session, round_id):
        current = session.get(TaskRound, round_id)
        planning = session.scalar(select(Operation.id).where(Operation.round_id == round_id, or_(Operation.state.in_(("queued", "planning")), Operation.result["planning_complete"].as_boolean().is_(False))).limit(1))
        if not planning:
            current.state, current.frozen_at = "running", current.frozen_at or utcnow()
        unfinished = session.scalar(select(Operation.id).where(Operation.round_id == round_id, Operation.state != "finished").limit(1))
        if not unfinished:
            counts = self._round_counts(session, round_id)
            aggregate_failures = session.scalar(select(Operation.id).where(Operation.round_id == round_id,
                Operation.result["outcome"].as_string().in_(("failed", "partial_failure"))).limit(1))
            final_outcome = outcome(counts)
            if aggregate_failures and final_outcome not in ("failed", "partial_failure"):
                final_outcome = "partial_failure" if counts.get("succeeded") or counts.get("skipped") else "failed"
            current.result = {**counts, "outcome": final_outcome}
            current.state, current.finished_at = "finished", utcnow()


    @staticmethod
    def _round_counts(session, round_id):
        return dict(session.execute(select(OperationItem.state, func.count()).join(Operation).where(
            Operation.round_id == round_id).group_by(OperationItem.state)).all())

    def acknowledge(self, operation_id):
        with self.database.write() as session:
            operation = session.get(Operation, operation_id)
            if operation is None:
                raise KeyError(operation_id)
            operation.needs_attention = False

    def snapshot(self, *, history_limit=20):
        with self.database.read() as session:
            current = session.scalar(select(TaskRound).order_by(TaskRound.created_at.desc()).limit(1))
            waiting = session.scalar(select(func.count()).select_from(Operation).where(
                Operation.round_id.is_(None), Operation.state == "queued"))
            rounds = None
            if current is not None:
                counts = self._round_counts(session, current.id)
                total, settled = sum(counts.values()), sum(counts.get(state, 0) for state in SETTLED)
                rounds = {"id": current.id, "state": current.state, "counts": counts,
                    "total": total if current.frozen_at else None, "settled": settled,
                    "ratio": settled / total if current.frozen_at and total else None,
                    "outcome": current.result.get("outcome"), "created_at": current.created_at.isoformat()}
            active_operation_count = session.scalar(select(func.count()).select_from(Operation).where(Operation.state.in_(ACTIVE)))
            queued_operation_count = session.scalar(select(func.count()).select_from(Operation).where(Operation.state.in_(("queued", "ready"))))
            priority = case((Operation.source == "manual", 0), (Operation.source == "maintenance", 2), else_=1)
            active = list(session.scalars(select(Operation).where(Operation.state.in_(("planning", "running"))).order_by(priority, Operation.created_at).limit(4)))
            active += list(session.scalars(select(Operation).where(Operation.state.in_(("queued", "ready", "interrupted"))).order_by(priority, Operation.created_at).limit(32)))
            history = list(session.scalars(select(Operation).where(Operation.state == "finished").order_by(
                Operation.finished_at.desc()).limit(max(1, min(history_limit, 100)))))
            attention_count = session.scalar(select(func.count()).select_from(Operation).where(Operation.needs_attention.is_(True)))
            views = [self._operation_view(op, session) for op in active + history]
            for view in views:
                view["result"] = {key: value for key, value in view["result"].items() if key not in ("child_operation_ids", "parent_operation_ids")}
            return {"active_operation_count": active_operation_count, "queued_operation_count": queued_operation_count, "catalogue_revision": catalogue_revision(session), "round": rounds, "next_round_count": waiting, "needs_attention_count": attention_count,
                    "automatic_paused": bool((setting := session.get(ApplicationState, "automatic_paused")) and setting.value),
                    "worker_error": self.worker_error,
                    "operations": views}

    def _operation_view(self, op, session=None):
        result = dict(op.result)
        running = []
        manually_paused = self.operation_paused(op.id)
        waiting_connection = op.state in ACTIVE and not self.kind_allowed(op.kind)
        phase, lane = {"name": op.phase, "unit": op.phase_unit, "done": op.phase_done, "total": op.phase_total, "detail": op.detail}, result.get("lane") or ("check" if op.state == "planning" else None)
        if waiting_connection:
            lane, phase["name"], phase["detail"] = None, "waiting_connection", "等待连接 Pixiv，任务进度已保留"
        if session is not None and op.state in ACTIVE and not waiting_connection:
            running = list(session.scalars(select(OperationItem).where(OperationItem.operation_id == op.id, OperationItem.state == "running").order_by(OperationItem.position).limit(4)))
            remote_pages = session.scalar(select(OperationItem.id).where(OperationItem.operation_id == op.id,
                OperationItem.kind.in_(("discover_page", "bookmark_page", "bookmark_artist")),
                OperationItem.state.in_(("pending", "running")),
                OperationItem.phase.in_(("checking", "bookmarks")),
                OperationItem.unit.in_(("pages", "artists"))).limit(1))
            if remote_pages:
                phase["total"] = None
            lane = "check" if op.state == "planning" else None
            if running:
                running.sort(key=lambda item: TaskQueue._lane(item) != "download")
                item = running[0]
                progress = self._step_progress(session, op, item.phase, item.unit)
                phase = {"name": item.phase, "unit": item.unit, "done": progress["done"],
                    "total": None if result.get("planning_complete") is False or remote_pages else progress["total"], "detail": "正在处理"}
                lane = TaskQueue._lane(item)
                current = result.get("current_items", {}).get(item.id)
                result.pop("current_item", None)
                if current:
                    result["current_item"] = current
        return {"id": op.id, "round_id": op.round_id, "kind": op.kind, "label": op.label,
                "source": op.source, "lane": lane,
                "state": ("pausing" if running or op.id in self._planning else "paused") if manually_paused else "queued" if waiting_connection else op.state,
                "pause_requested": op.pause_requested,
                "control": {"can_pause": op.kind == "reconcile" and op.source == "manual" and op.state in ACTIVE and not op.pause_requested,
                            "can_resume": op.kind == "reconcile" and op.source == "manual" and op.state in ACTIVE and op.pause_requested}, "phase": phase,
                "result": result, "needs_attention": op.needs_attention,
                "created_at": op.created_at.isoformat(), "finished_at": op.finished_at.isoformat() if op.finished_at else None}

    def details(self, operation_id, offset=0, limit=100, problems_only=False):
        limit = max(1, min(limit, 100))
        with self.database.read() as session:
            operation = session.get(Operation, operation_id)
            if operation is None:
                raise KeyError(operation_id)
            counts = dict(session.execute(select(OperationItem.state, func.count()).where(
                OperationItem.operation_id == operation_id).group_by(OperationItem.state)).all())
            statement = select(OperationItem).where(OperationItem.operation_id == operation_id)
            if problems_only:
                statement = statement.where(OperationItem.state.in_(("failed", "not_executed")))
            found = list(session.scalars(statement.order_by(OperationItem.position).offset(max(0, offset)).limit(max(1, min(limit, 100)) + 1)))
            items = found[:limit]
            phases = session.execute(select(OperationItem.phase, OperationItem.unit, OperationItem.state, func.count()).where(
                OperationItem.operation_id == operation_id).group_by(OperationItem.phase, OperationItem.unit, OperationItem.state))
            return {**self._operation_view(operation, session), "counts": counts, "has_more_items": len(found) > limit,
                "phases": [{"phase": p, "unit": u, "state": s, "count": c} for p, u, s, c in phases],
                "items": [{"id": i.id, "key": i.item_key, "state": i.state, "phase": i.phase,
                           "attempts": i.attempts, "result": i.result} for i in items]}

    def history(self, before=None, limit=20, attention_only=False):
        limit = max(1, min(limit, 100))
        with self.database.read() as session:
            statement = select(Operation).where(Operation.state == "finished")
            if attention_only:
                statement = statement.where(Operation.needs_attention.is_(True))
            if before:
                reference = session.get(Operation, before)
                if reference is None or reference.finished_at is None:
                    raise KeyError(before)
                statement = statement.where(or_(Operation.finished_at < reference.finished_at,
                    and_(Operation.finished_at == reference.finished_at, Operation.id < reference.id)))
            found = list(session.scalars(statement.order_by(Operation.finished_at.desc(), Operation.id.desc()).limit(limit + 1)))
            return {"items": [self._operation_view(operation) for operation in found[:limit]],
                    "has_more": len(found) > limit}

    def retry(self, operation_id):
        with self.database.read() as session:
            operation = session.get(Operation, operation_id)
            if operation is None or operation.state != "finished":
                raise ValueError("只能重新提交已经结束的任务")
            parameters = dict(operation.parameters)
            started = parameters.pop("started_artist_ids", None)
            if started is not None:
                parameters["artist_ids"] = started
            kind, label = operation.kind, operation.label
        return self.submit(kind, parameters, label + "（重新执行）")
