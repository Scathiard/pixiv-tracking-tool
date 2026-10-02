"""Persisted operations and fixed queue rounds, with domain handlers at one sync boundary."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import and_, case, exists, func, or_, select, true
from sqlalchemy.orm import aliased

from .database import ArchiveDatabase
from .catalogue_revision import catalogue_revision
from .models import ApplicationState, Asset, Derivative, Operation, OperationItem, TaskRound, Work, utcnow

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


class TaskContext:
    def __init__(self, queue: TaskQueue, operation_id: str, item_id: str | None = None):
        self.queue, self.operation_id, self.item_id = queue, operation_id, item_id

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


class TaskQueue:
    def __init__(self, database: ArchiveDatabase, handlers: dict[str, Handler]):
        self.database, self.handlers = database, handlers
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._threads = []
        self._planning = set()
        self._resources = set()
        self._accepting = True
        self.worker_error = None
        self.maintenance = None
        self.maintenance_error = None

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
        self._wake.set()
        return paused

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
            operation.round_id = operation.round_id or parent.round_id
        self._wake.set()
        return {"operation_id": operation.id, "created": created, "state": operation.state, "round_id": operation.round_id}

    def materialize_in_transaction(self, session, operation_id, specs):
        existing = set(session.scalars(select(OperationItem.item_key).where(OperationItem.operation_id == operation_id)))
        position = session.scalar(select(func.max(OperationItem.position)).where(OperationItem.operation_id == operation_id))
        position = 0 if position is None else position + 1
        for spec in specs:
            if spec.key in existing:
                continue
            if any(key not in existing for key in spec.depends_on):
                raise ValueError("清单依赖顺序不合法")
            session.add(OperationItem(operation_id=operation_id, position=position, item_key=spec.key,
                kind=spec.kind, target=spec.target, phase=spec.phase, unit=spec.unit, depends_on=list(spec.depends_on)))
            existing.add(spec.key)
            position += 1
        session.flush()
        self._wake.set()

    def submit(self, kind: str, parameters: dict, label: str, source="manual") -> dict:
        with self.database.write() as session:
            return self.enqueue_in_transaction(session, kind, parameters, label, source)

    def recover(self):
        """Call after filesystem journal recovery, before starting the sole worker."""
        with self.database.write() as session:
            for item in session.scalars(select(OperationItem).where(OperationItem.state == "running")):
                item.state = "pending"
            for operation in session.scalars(select(Operation).where(Operation.state.in_(("running", "interrupted")))):
                round_record = session.get(TaskRound, operation.round_id)
                operation.state = "ready" if round_record and round_record.frozen_at else "planning"
                operation.detail = "上次运行中断，正在核对并继续"
        self.worker_error = None

    def start(self):
        if any(thread.is_alive() for thread in self._threads):
            return
        self._stop.clear()
        self._threads = [threading.Thread(target=self._worker, args=(lane,),
            name=f"archive-{lane}-{index}", daemon=True)
            for index, lane in enumerate(("check", "download", "download", "local"))]
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
                if lane == "check" and self.maintenance is not None:
                    try:
                        self.maintenance.tick()
                        self.maintenance_error = None
                    except Exception as exc:
                        self.maintenance_error = type(exc).__name__
                if self._step(lane):
                    continue
                self._wake.wait(0.1)
                self._wake.clear()
            except ShutdownRequested:
                return
            except Exception as exc:
                self.worker_error = type(exc).__name__
                log.error("任务执行器中断，未完成记录已保留", exc_info=False)
                self._stop.set()

    @staticmethod
    def _lane(item):
        if item.phase == "checking" or item.kind in ("metadata_artist", "metadata_work", "bookmark_artist", "discover_work"):
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

    def _step(self, lane, round_id=None):
        claim = None
        with self.database.write() as session:
            if not self._accepting or self._stop.is_set():
                return False
            if lane == "check":
                if round_id is None:
                    self._claim_round_in_transaction(session)
                ops = list(session.scalars(select(Operation).where(Operation.state.in_(("queued", "planning")),
                    Operation.round_id.is_not(None)).order_by(Operation.created_at, Operation.id)))
                ops.sort(key=lambda op: (op.source != "manual", op.source == "maintenance"))
                for operation in ops:
                    if round_id is not None and operation.round_id != round_id or operation.id in self._planning:
                        continue
                    paused = session.get(ApplicationState, "automatic_paused")
                    if operation.source == "automatic" and operation.started_at is None and paused and paused.value is True:
                        previous_round = operation.round_id
                        operation.round_id = None
                        session.flush()
                        self._finalize_round(session, previous_round)
                        continue
                    operation.state = "planning"
                    operation.started_at = operation.started_at or utcnow()
                    self._planning.add(operation.id)
                    claim = ("plan", operation.id)
                    break
            if claim is None:
                download = or_(OperationItem.kind == "download", and_(OperationItem.phase == "downloading", ~OperationItem.kind.in_(("timing", "error"))))
                check = or_(OperationItem.phase == "checking", OperationItem.kind.in_(("metadata_artist", "metadata_work", "bookmark_artist", "discover_work")))
                lane_filter = check if lane == "check" else and_(~check, download) if lane == "download" else and_(~check, ~download)
                dependency = aliased(OperationItem)
                dependency_keys = func.json_each(OperationItem.depends_on).table_valued("value").alias("dependency_keys")
                waiting_dependency = exists(select(1).select_from(dependency_keys).join(dependency, and_(dependency.operation_id == OperationItem.operation_id, dependency.item_key == dependency_keys.c.value)).where(~dependency.state.in_(SETTLED)).correlate(OperationItem))
                query = select(OperationItem, Operation).join(Operation, Operation.id == OperationItem.operation_id).where(
                    Operation.state.in_(("planning", "ready", "running")), OperationItem.state == "pending", lane_filter, ~waiting_dependency)
                if round_id is not None:
                    query = query.where(Operation.round_id == round_id)
                query = query.order_by(case((Operation.source == "manual", 0), (Operation.source == "maintenance", 2), else_=1), Operation.created_at, Operation.id, OperationItem.position)
                # Bounded ORM windows; settled siblings are never materialized.
                offset = 0
                while claim is None:
                    candidates = list(session.execute(query.offset(offset).limit(64)))
                    for item, operation in candidates:
                        resource = self._item_resources(session, item)
                        if resource & self._resources:
                            continue
                        states = dict(session.execute(select(OperationItem.item_key, OperationItem.state).where(
                            OperationItem.operation_id == operation.id, OperationItem.item_key.in_(item.depends_on))).all()) if item.depends_on else {}
                        if any(key in states and states[key] not in SETTLED for key in item.depends_on):
                            continue
                        self._resources.update(resource)
                        item.state, item.attempts = "running", item.attempts + 1
                        operation.state, operation.started_at = "running", operation.started_at or utcnow()
                        operation.result = {**operation.result, "lane": lane}
                        claim = ("item", operation.id, item, resource, any(states.get(key) != "succeeded" for key in item.depends_on))
                        break
                    if len(candidates) < 64:
                        break
                    offset += 64
        if claim is None:
            return False
        if claim[0] == "plan":
            try:
                self._plan(claim[1])
            finally:
                with self.database.write():
                    self._planning.discard(claim[1])
            return True
        _, operation_id, item, resource, dependency_failed = claim
        try:
            self._execute_item(operation_id, item, dependency_failed)
        finally:
            with self.database.write() as session:
                self._resources.difference_update(resource)
                self._finalize(session, [operation_id])
            self._wake.set()
        return True

    def _claim_round_in_transaction(self, session):
        paused = session.get(ApplicationState, "automatic_paused")
        waiting = list(session.scalars(select(Operation).where(Operation.state.in_(("queued", "ready")), Operation.round_id.is_(None)).order_by(Operation.created_at, Operation.id)))
        if paused and paused.value is True:
            waiting = [op for op in waiting if op.source != "automatic" or op.started_at is not None]
        if any(op.source != "maintenance" for op in waiting):
            waiting = [op for op in waiting if op.source != "maintenance"]
        if not waiting:
            return None
        current = TaskRound()
        session.add(current)
        session.flush()
        for op in waiting:
            op.round_id = current.id
        return current.id

    def _claim_round(self):
        with self.database.write() as session:
            current = session.scalar(select(TaskRound).where(TaskRound.state.in_(("planning", "running"))).order_by(TaskRound.created_at))
            return current.id if current else self._claim_round_in_transaction(session)

    def _plan(self, operation_id):
        from ..request_policy import ShutdownRequested
        with self.database.write() as session:
            operation = session.get(Operation, operation_id)
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
        context = TaskContext(self, operation_id)
        failure = None
        try:
            context.checkpoint()
            specs = self.handlers[kind].plan(parameters, context)
            seen = set()
            for spec in specs:
                if spec.key in seen or any(key not in seen for key in spec.depends_on):
                    raise ValueError("清单重复或依赖顺序不合法")
                seen.add(spec.key)
        except ShutdownRequested:
            with self.database.write() as session:
                operation = session.get(Operation, operation_id)
                operation.state, operation.detail = "interrupted", "退出前已保存进度，下次启动继续"
            return False
        except Exception as exc:
            failure = safe_failure(exc)
            specs = [ItemSpec("inventory-failed", "inventory-failed", {}, "checking")]
        with self.database.write() as session:
            operation = session.get(Operation, operation_id)
            operation.result = {**operation.result, "planning_complete": True, "inventory_complete": failure is None and not any(
                spec.kind == "error" and spec.phase == "checking" for spec in specs), "inventory": {
                "phase": operation.phase, "unit": operation.phase_unit,
                "done": operation.phase_done, "total": operation.phase_total,
            }}
            self.materialize_in_transaction(session, operation_id, specs)
            if failure:
                for stored in session.scalars(select(OperationItem).where(OperationItem.operation_id == operation_id, OperationItem.item_key == "inventory-failed")):
                    stored.state, stored.result = "failed", failure
            running_item = session.scalar(select(OperationItem.id).where(OperationItem.operation_id == operation_id, OperationItem.state == "running").limit(1))
            operation.state = "running" if running_item else "ready"
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
            while not self._stop.is_set():
                progressed = False
                for lane in ("check", "download", "local"):
                    progressed = self._step(lane, round_id) or progressed
                with self.database.read() as session:
                    current = session.get(TaskRound, round_id)
                    if current.state == "finished":
                        return True
                if not progressed:
                    return False
            return False
        finally:
            self._run_lock.release()

    def _execute_item(self, operation_id, item, dependency_failed=False):
        from ..request_policy import ShutdownRequested
        phase_key = item.phase + ":" + item.unit
        with self.database.read() as session:
            kind = session.get(Operation, operation_id).kind
            counts = dict(session.execute(select(OperationItem.state, func.count()).where(OperationItem.operation_id == operation_id,
                OperationItem.phase == item.phase, OperationItem.unit == item.unit).group_by(OperationItem.state)).all())
        context = TaskContext(self, operation_id, item.id)
        # Claim happened just before shutdown: leave the item pending rather than
        # classifying cancellation as a failed or successful attempt.
        try:
            context.report(item.phase, sum(counts.get(state, 0) for state in SETTLED), sum(counts.values()), item.unit, "正在处理")
        except ShutdownRequested:
            self._interrupt_item(item.id, operation_id)
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
                result = self.handlers[kind].execute(spec, context)
                if not isinstance(result, ItemResult) or result.state not in SETTLED:
                    raise TypeError("任务必须返回明确的处理结果")
            except ShutdownRequested:
                self._interrupt_item(item.id, operation_id)
                return
            except Exception as exc:
                result = ItemResult("failed", safe_failure(exc))
        with self.database.write() as session:
            stored = session.get(OperationItem, item.id)
            stored.state, stored.result = result.state, result.detail
            session.flush()
            operation = session.get(Operation, operation_id)
            counts = dict(session.execute(select(OperationItem.state, func.count()).where(OperationItem.operation_id == operation_id,
                OperationItem.phase == item.phase, OperationItem.unit == item.unit).group_by(OperationItem.state)).all())
            done, total = sum(counts.get(state, 0) for state in SETTLED), sum(counts.values())
            operation.phase_done, operation.updated_at = done, utcnow()
            snapshots = dict(operation.result.get("phase_snapshots", {}))
            snapshots[phase_key] = {"phase": item.phase, "unit": item.unit, "done": done, "total": total}
            current_items = dict(operation.result.get("current_items", {}))
            current_items.pop(item.id, None)
            operation.result = {**{k: v for k, v in operation.result.items() if k != "current_item"}, "current_items": current_items, "phase_snapshots": snapshots}
            if current_items:
                operation.result = {**operation.result, "current_item": next(iter(current_items.values()))}

    def _interrupt_item(self, item_id, operation_id):
        with self.database.write() as session:
            session.get(OperationItem, item_id).state = "pending"
            operation = session.get(Operation, operation_id)
            operation.state, operation.detail = "interrupted", "退出前已保存进度，下次启动继续"
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
            if operation.state not in ("ready", "running") or operation.id in self._planning and operation.result.get("planning_complete") is not True:
                continue
            unfinished = session.scalar(select(OperationItem.id).where(OperationItem.operation_id == operation.id, ~OperationItem.state.in_(SETTLED)).limit(1))
            if unfinished:
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

    @staticmethod
    def _operation_view(op, session=None):
        result = dict(op.result)
        phase, lane = {"name": op.phase, "unit": op.phase_unit, "done": op.phase_done, "total": op.phase_total, "detail": op.detail}, result.get("lane") or ("check" if op.state == "planning" else None)
        if session is not None and op.state in ACTIVE:
            running = list(session.scalars(select(OperationItem).where(OperationItem.operation_id == op.id, OperationItem.state == "running").order_by(OperationItem.position).limit(4)))
            lane = "check" if op.state == "planning" else None
            if running:
                running.sort(key=lambda item: TaskQueue._lane(item) != "download")
                item = running[0]
                counts = dict(session.execute(select(OperationItem.state, func.count()).where(OperationItem.operation_id == op.id, OperationItem.phase == item.phase, OperationItem.unit == item.unit).group_by(OperationItem.state)).all())
                phase = {"name": item.phase, "unit": item.unit, "done": sum(counts.get(state, 0) for state in SETTLED), "total": None if result.get("planning_complete") is False else sum(counts.values()), "detail": "正在处理"}
                lane = TaskQueue._lane(item)
                current = result.get("current_items", {}).get(item.id)
                result.pop("current_item", None)
                if current:
                    result["current_item"] = current
        return {"id": op.id, "round_id": op.round_id, "kind": op.kind, "label": op.label,
                "source": op.source, "lane": lane,
                "state": op.state, "phase": phase,
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
