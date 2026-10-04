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
from datetime import timedelta
from contextlib import nullcontext

from sqlalchemy import and_, case, exists, func, or_, select, true, union_all
from sqlalchemy.orm import aliased

from .database import ArchiveDatabase
from .catalogue_revision import catalogue_revision
from .models import ApplicationState, Asset, Derivative, Operation, OperationItem, TaskRound, Work, utcnow
from ..version import __build__

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
        if self.queue.operation_paused(self.operation_id):
            raise ManualPauseRequested()
        if self.kind is not None and not self.queue.kind_allowed(self.kind):
            raise ConnectionRequired()


class TaskQueue:
    def __init__(self, database: ArchiveDatabase, handlers: dict[str, Handler], *, allowed_kinds=None):
        self.database, self.handlers = database, handlers
        self.allowed_kinds = allowed_kinds
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
        self._claim_sequence = 0
        self._round_admissions = 0
        self._candidate_after = {}
        self._last_scan_more = False
        with self.database.read() as session:
            records = session.execute(select(Operation.id, Operation.pause_requested, Operation.result).where(Operation.state.in_(ACTIVE))).all()
        self._paused_operations = {ident for ident, paused, _ in records if paused}
        self._control_parents = {ident: result.get("parent_operation_ids", []) for ident, _, result in records}

    def kind_allowed(self, kind):
        allowed = self.allowed_kinds() if self.allowed_kinds else None
        return allowed is None or kind in allowed

    def _allowed_query(self, query):
        allowed = self.allowed_kinds() if self.allowed_kinds else None
        return query if allowed is None else query.where(Operation.kind.in_(allowed))

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
        self._wake.set()
        return paused

    def operation_paused(self, operation_id):
        pending, seen = [operation_id], set()
        while pending:
            ident = pending.pop()
            if ident in seen:
                continue
            if ident in self._paused_operations:
                return True
            seen.add(ident)
            pending.extend(self._control_parents.get(ident, []))
        return False

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
        if paused:
            self._paused_operations.add(operation_id)
        else:
            self._paused_operations.discard(operation_id)
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
            self._control_parents[operation.id] = parents
            operation.round_id = operation.round_id or parent.round_id
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
        for spec in specs:
            if spec.key in existing:
                continue
            if any(key not in existing for key in spec.depends_on):
                raise ValueError("清单依赖顺序不合法")
            session.add(OperationItem(operation_id=operation_id, position=position, item_key=spec.key,
                kind=spec.kind, target=spec.target, phase=spec.phase, unit=spec.unit, depends_on=list(spec.depends_on),
                lane=self._lane(spec)))
            existing.add(spec.key)
            position += 1
        session.flush()
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
                round_record = session.get(TaskRound, operation.round_id)
                operation.state = "planning" if operation.result.get("planning_complete") is False else "ready" if round_record and round_record.frozen_at else "planning"
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
        if not self._accepting or self._stop.is_set():
            return False
        if lane == "check" and round_id is None:
            with self.database.write() as session:
                self._claim_round_in_transaction(session)
        claim = None
        with self.database.read() as session:
            query = self._allowed_query(select(Operation).where(Operation.state.in_(("queued", "planning", "ready", "running", "interrupted")),
                Operation.pause_requested.is_(False)))
            if round_id is not None:
                query = query.where(Operation.round_id == round_id)
            paused_ids = [ident for ident in self._control_parents if self.operation_paused(ident)]
            if paused_ids:
                query = query.where(~Operation.id.in_(paused_ids))
            operations = list(session.scalars(query.order_by(Operation.last_claim_seq,
                case((Operation.source == "manual", 0), else_=1), Operation.created_at, Operation.id).limit(64)))
            if self._claim_sequence % 8 == 7:
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
                if lane == "check" and operation.round_id is not None and self._needs_planning(operation) and operation.id not in self._planning:
                    if operation.source == "automatic" and operation.started_at is None and paused and paused.value is True:
                        deferred.append(operation.id)
                        continue
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
                after = self._candidate_after.get(cursor_key, -1)
                # Bound candidates before dependency checks. Legacy NULL lanes
                # are classified inside the window rather than scanning the
                # whole remaining manifest for an empty channel.
                for _ in range(32):
                    items = self._candidate_window(session, operation, lane, after)
                    if not items:
                        self._candidate_after.pop(cursor_key, None)
                        break
                    after = items[-1].position
                    self._candidate_after[cursor_key] = after
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
                        if resource & self._resources:
                            continue
                        candidates.append(("item", operation.id, item.id, resource))
                        break
                    if candidates:
                        break
                else:
                    self._last_scan_more = True
                if candidates:
                    break
        write_started = time.monotonic()
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
                if operation.state == "finished" or self._paused_in_transaction(session, operation) or not self.kind_allowed(operation.kind):
                    continue
                if candidate[0] == "plan":
                    if operation.id in self._planning or not self._needs_planning(operation):
                        continue
                    operation.state = "planning"
                    operation.started_at = operation.started_at or utcnow()
                    self._planning.add(operation.id)
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
                    if resource & self._resources:
                        continue
                    self._resources.update(resource)
                    item.state, item.attempts = "running", item.attempts + 1
                    operation.state, operation.started_at = "running", operation.started_at or utcnow()
                    operation.result = {**operation.result, "lane": lane}
                    self._candidate_after.pop((operation.id, lane), None)
                    claim = ("item", operation.id, item, resource, any(states.get(key) != "succeeded" for key in item.depends_on))
                self._claim_sequence = max(self._claim_sequence, operation.last_claim_seq) + 1
                if candidate[0] != "plan":
                    operation.last_claim_seq = self._claim_sequence
                break
        log.debug("task_claim build=%s lane=%s operation=%s step=%s elapsed_ms=%.3f write_ms=%.3f", __build__, lane,
            claim[1] if claim else "none", claim[2].id if claim and claim[0] == "item" else "plan" if claim else "none",
            (time.monotonic() - started) * 1000, (time.monotonic() - write_started) * 1000)
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
        query = self._allowed_query(select(Operation).where(Operation.state.in_(("queued", "ready", "planning", "interrupted")),
            Operation.round_id.is_(None), Operation.pause_requested.is_(False)))
        if paused and paused.value is True:
            query = query.where(or_(Operation.source != "automatic", Operation.started_at.is_not(None)))
        waiting = list(session.scalars(query.order_by(case((Operation.source == "manual", 0),
            (Operation.source == "maintenance", 2), else_=1), Operation.created_at, Operation.id).limit(64)))
        if any(op.source != "maintenance" for op in waiting):
            if self._round_admissions % 2 == 0:
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
            client = getattr(handler, "client", None)
            scope_factory = getattr(type(client), "task_scope", None)
            with scope_factory(client, context) if callable(scope_factory) else nullcontext():
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
        except (ShutdownRequested, ManualPauseRequested, ConnectionRequired):
            with self.database.write() as session:
                operation = session.get(Operation, operation_id)
                operation.state, operation.detail = ("interrupted", "退出前已保存进度，下次启动继续") if self._stop.is_set() else ("planning", "已暂停，清单进度已保存" if self.kind_allowed(kind) else "等待连接 Pixiv，任务进度已保留")
            return False
        except Exception as exc:
            if not self.kind_allowed(kind):
                return False
            failure = safe_failure(exc)
            complete = True
            specs = [ItemSpec("inventory-failed", "inventory-failed", {}, "checking")]
        with self.database.write() as session:
            operation = session.get(Operation, operation_id)
            operation.result = {**operation.result, "planning_complete": complete, "planning_cursor": batch.cursor if callable(getattr(self.handlers[kind], "plan_batch", None)) and failure is None else {}, "inventory_complete": complete and failure is None and not operation.result.get("inventory_failed") and not any(
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

    def _execute_item(self, operation_id, item, dependency_failed=False):
        from ..request_policy import ShutdownRequested
        phase_key = item.phase + ":" + item.unit
        with self.database.read() as session:
            kind = session.get(Operation, operation_id).kind
            counts = self._phase_counts(session, operation_id, item.phase, item.unit)
        context = TaskContext(self, operation_id, item.id, kind=kind)
        # Claim happened just before shutdown: leave the item pending rather than
        # classifying cancellation as a failed or successful attempt.
        try:
            context.report(item.phase, sum(counts.get(state, 0) for state in SETTLED), sum(counts.values()), item.unit, "正在处理")
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
                client = getattr(handler, "client", None)
                scope_factory = getattr(type(client), "task_scope", None)
                scope = scope_factory(client, context) if callable(scope_factory) else nullcontext()
                with scope:
                    result = handler.execute(spec, context)
                if not isinstance(result, ItemResult) or result.state not in SETTLED and not (item.kind == "bookmark_artist" and result.state == "yielded"):
                    raise TypeError("任务必须返回明确的处理结果")
            except (ShutdownRequested, ManualPauseRequested, ConnectionRequired) as exc:
                self._interrupt_item(item.id, operation_id, refund_attempt=isinstance(exc, ConnectionRequired))
                return
            except Exception as exc:
                if not self.kind_allowed(kind):
                    self._interrupt_item(item.id, operation_id, refund_attempt=True)
                    return
                detail = safe_failure(exc)
                if detail.get("retryable") and item.attempts < 3:
                    with self.database.write() as session:
                        stored = session.get(OperationItem, item.id)
                        stored.state, stored.result = "pending", detail
                        stored.not_before = utcnow() + timedelta(seconds=max(1, detail.get("retry_after", 2 ** item.attempts)))
                        operation = session.get(Operation, operation_id)
                        operation.detail = "远端暂时未响应，已保存进度，等待重试"
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
            stored.state, stored.result = result.state, result.detail
            session.flush()
            operation = session.get(Operation, operation_id)
            if result.state == "failed" and (item.kind in ("discover_page", "bookmark_page") or
                    item.kind == "bookmark_artist" and "unconfirmed_works" not in result.detail):
                operation.result = {**operation.result, "inventory_failed": True, "inventory_complete": False}
            counts = self._phase_counts(session, operation_id, item.phase, item.unit)
            done, total = sum(counts.get(state, 0) for state in SETTLED), sum(counts.values())
            operation.phase_done, operation.updated_at = done, utcnow()
            snapshots = dict(operation.result.get("phase_snapshots", {}))
            snapshots[phase_key] = {"phase": item.phase, "unit": item.unit, "done": done, "total": total}
            current_items = dict(operation.result.get("current_items", {}))
            current_items.pop(item.id, None)
            operation.result = {**{k: v for k, v in operation.result.items() if k != "current_item"}, "current_items": current_items, "phase_snapshots": snapshots}
            if current_items:
                operation.result = {**operation.result, "current_item": next(iter(current_items.values()))}

    def _interrupt_item(self, item_id, operation_id, *, refund_attempt=False):
        with self.database.write() as session:
            item = session.get(OperationItem, item_id)
            item.state = "pending"
            if refund_attempt:
                item.attempts = max(0, item.attempts - 1)
            operation = session.get(Operation, operation_id)
            operation.state, operation.detail = ("interrupted", "退出前已保存进度，下次启动继续") if self._stop.is_set() else ("ready" if operation.result.get("planning_complete") is not False else "planning", "已保存进度，等待继续")
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
                counts = self._phase_counts(session, op.id, item.phase, item.unit)
                phase = {"name": item.phase, "unit": item.unit, "done": sum(counts.get(state, 0) for state in SETTLED),
                    "total": None if result.get("planning_complete") is False or remote_pages else sum(counts.values()), "detail": "正在处理"}
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
