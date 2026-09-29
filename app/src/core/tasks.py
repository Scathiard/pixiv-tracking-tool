"""Persisted operations and fixed queue rounds, with domain handlers at one sync boundary."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import and_, func, or_, select

from .database import ArchiveDatabase
from .models import ApplicationState, Operation, OperationItem, TaskRound, utcnow

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
        with self.queue.database.write() as session:
            session.get(Operation, self.operation_id).parameters = parameters

    def report_bytes(self, done: int, total: int | None):
        with self.queue.database.write() as session:
            operation = session.get(Operation, self.operation_id)
            operation.result = {**operation.result, "current_item": {
                "id": self.item_id, "unit": "bytes", "done": done, "total": total}}

    def detail(self, text):
        with self.queue.database.write() as session:
            operation = session.get(Operation, self.operation_id)
            operation.detail, operation.updated_at = text, utcnow()


class TaskQueue:
    def __init__(self, database: ArchiveDatabase, handlers: dict[str, Handler]):
        self.database, self.handlers = database, handlers
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
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

    def submit(self, kind: str, parameters: dict, label: str, source="manual") -> dict:
        if kind not in self.handlers or source not in ("manual", "automatic", "maintenance"):
            raise ValueError("不支持的任务类型或来源")
        canonical = json.dumps([kind, source, parameters], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with self.database.write() as session:
            if not self._accepting:
                raise RuntimeError("图库正在切换，暂时不能提交任务")
            operation = session.scalar(select(Operation).where(
                Operation.dedupe_key == key, Operation.state.in_(ACTIVE)))
            created = operation is None
            if created:
                operation = Operation(kind=kind, parameters=parameters, source=source,
                                      label=label, dedupe_key=key)
                session.add(operation)
                session.flush()
            result = {"operation_id": operation.id, "created": created,
                      "state": operation.state, "round_id": operation.round_id}
        self._wake.set()
        return result

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
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="archive-tasks", daemon=True)
        self._thread.start()
        self._wake.set()

    def stop(self, timeout=5):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return self._thread is None or not self._thread.is_alive()

    def _worker(self):
        while not self._stop.is_set():
            self._wake.wait(1)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                if self.maintenance is not None:
                    try:
                        self.maintenance.tick()
                        self.maintenance_error = None
                    except Exception as exc:
                        # Catalogue maintenance is optional work; a broken scan
                        # must not stop user-submitted archive operations.
                        self.maintenance_error = type(exc).__name__
                        log.error("后台补齐清点失败，本轮稍后重试", exc_info=False)
                while not self._stop.is_set() and self.run_round():
                    pass
            except Exception as exc:
                # Leave the persistent state for recovery; don't spin a broken round.
                self.worker_error = type(exc).__name__
                try:
                    with self.database.write() as session:
                        for operation in session.scalars(select(Operation).where(Operation.state.in_(("running", "planning", "ready")))):
                            operation.state, operation.needs_attention = "interrupted", True
                            operation.detail = "执行中断，重启后会核对并继续"
                except Exception:
                    pass  # The database itself may be unavailable; in-memory health remains visible.
                log.error("任务执行器中断，未完成记录已保留", exc_info=False)
                self._stop.set()

    def _claim_round(self):
        with self.database.write() as session:
            current = session.scalar(select(TaskRound).where(
                TaskRound.state.in_(("planning", "running"))).order_by(TaskRound.created_at))
            if current is not None:
                return current.id
            paused = session.get(ApplicationState, "automatic_paused")
            eligible = [Operation.state == "queued", Operation.round_id.is_(None)]
            if paused is not None and paused.value is True:
                eligible.append(Operation.source != "automatic")
            waiting = list(session.scalars(select(Operation).where(
                *eligible).order_by(
                    Operation.created_at, Operation.id)))
            if any(operation.source != "maintenance" for operation in waiting):
                waiting = [operation for operation in waiting if operation.source != "maintenance"]
            if not waiting:
                return None
            current = TaskRound()
            session.add(current)
            session.flush()
            for operation in waiting:
                operation.round_id = current.id
            return current.id

    def _plan(self, operation_id):
        with self.database.write() as session:
            operation = session.get(Operation, operation_id)
            paused = session.get(ApplicationState, "automatic_paused")
            if (operation.source == "automatic" and operation.state == "queued"
                    and operation.started_at is None and paused is not None and paused.value is True):
                # A pause may arrive after round selection but before this batch starts.
                operation.round_id = None
                return False
            operation.state, operation.phase = "planning", "checking"
            operation.started_at = operation.started_at or utcnow()
            kind, parameters = operation.kind, operation.parameters
        context = TaskContext(self, operation_id)
        failure = None
        try:
            specs = self.handlers[kind].plan(parameters, context)
            seen = set()
            for spec in specs:
                if spec.key in seen or any(key not in seen for key in spec.depends_on):
                    raise ValueError("清单重复或依赖顺序不合法")
                seen.add(spec.key)
        except Exception as exc:
            failure = safe_failure(exc)
            specs = [ItemSpec("inventory-failed", "inventory-failed", {}, "checking")]
        with self.database.write() as session:
            operation = session.get(Operation, operation_id)
            operation.result = {**operation.result, "inventory_complete": failure is None and not any(
                spec.kind == "error" and spec.phase == "checking" for spec in specs), "inventory": {
                "phase": operation.phase, "unit": operation.phase_unit,
                "done": operation.phase_done, "total": operation.phase_total,
            }}
            for position, spec in enumerate(specs):
                session.add(OperationItem(operation_id=operation_id, position=position,
                    item_key=spec.key, kind=spec.kind, target=spec.target, phase=spec.phase,
                    unit=spec.unit, depends_on=list(spec.depends_on),
                    state="failed" if failure else "pending", result=failure or {}))
            operation.state = "ready"
        return True

    def run_round(self) -> bool:
        """Run one frozen round; submissions made after claim stay in the next round."""
        if not self._run_lock.acquire(blocking=False):
            return False
        try:
            round_id = self._claim_round()
            if round_id is None:
                return False
            with self.database.read() as session:
                operations = list(session.scalars(select(Operation).where(
                    Operation.round_id == round_id).order_by(Operation.created_at, Operation.id)))
            operations.sort(key=lambda operation: operation.source == "maintenance")
            included = []
            for operation in operations:
                if self._stop.is_set():
                    return False
                if operation.state in ("queued", "planning"):
                    if not self._plan(operation.id):
                        continue
                included.append(operation)
            with self.database.write() as session:
                current = session.get(TaskRound, round_id)
                current.state = "running"
                current.frozen_at = current.frozen_at or utcnow()
            for operation in included:
                if self._stop.is_set():
                    return False
                if operation.state in SETTLED or operation.state == "finished":
                    continue
                if not self._execute_operation(operation.id):
                    return False
            with self.database.write() as session:
                current = session.get(TaskRound, round_id)
                counts = self._round_counts(session, round_id)
                current.result = {**counts, "outcome": outcome(counts)}
                current.state, current.finished_at = "finished", utcnow()
            return True
        finally:
            self._run_lock.release()

    def _execute_operation(self, operation_id):
        with self.database.read() as session:
            operation = session.get(Operation, operation_id)
            kind = operation.kind
            items = list(session.scalars(select(OperationItem).where(
                OperationItem.operation_id == operation_id).order_by(OperationItem.position)))
        states = {item.item_key: item.state for item in items}
        phase_totals = Counter((item.phase, item.unit) for item in items)
        phase_done = Counter((item.phase, item.unit) for item in items if item.state in SETTLED)
        for item in items:
            if self._stop.is_set():
                return False
            if item.state in SETTLED:
                continue
            phase_key = (item.phase, item.unit)
            context = TaskContext(self, operation_id, item.id)
            context.report(item.phase, phase_done[phase_key], phase_totals[phase_key], item.unit, "正在处理")
            with self.database.write() as session:
                session.get(Operation, operation_id).state = "running"
                stored = session.get(OperationItem, item.id)
                stored.state, stored.attempts = "running", stored.attempts + 1
            if any(states[key] != "succeeded" for key in item.depends_on):
                result = ItemResult("not_executed", {"message": "前置步骤未成功，此项尚未执行"})
            else:
                try:
                    spec = ItemSpec(item.item_key, item.kind, item.target, item.phase, item.unit, tuple(item.depends_on))
                    result = self.handlers[kind].execute(spec, context)
                    if not isinstance(result, ItemResult) or result.state not in SETTLED:
                        raise TypeError("任务必须返回明确的处理结果")
                except Exception as exc:
                    result = ItemResult("failed", safe_failure(exc))
            states[item.item_key] = result.state
            phase_done[phase_key] += 1
            with self.database.write() as session:
                stored = session.get(OperationItem, item.id)
                stored.state, stored.result = result.state, result.detail
                operation = session.get(Operation, operation_id)
                operation.phase_done = phase_done[phase_key]
                operation.updated_at = utcnow()
                operation.result = {k: v for k, v in operation.result.items() if k != "current_item"}
                snapshots = dict(operation.result.get("phase_snapshots", {}))
                snapshots[item.phase + ":" + item.unit] = {"phase": item.phase, "unit": item.unit,
                    "done": phase_done[phase_key], "total": phase_totals[phase_key]}
                operation.result = {**operation.result, "phase_snapshots": snapshots}
        counts = dict(Counter(states.values()))
        with self.database.write() as session:
            operation = session.get(Operation, operation_id)
            operation.result = {**operation.result, **counts, "outcome": outcome(counts)}
            operation.needs_attention = outcome(counts) in ("partial_failure", "failed")
            operation.state, operation.finished_at = "finished", utcnow()
        return True

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
            active = list(session.scalars(select(Operation).where(Operation.state.in_(ACTIVE)).order_by(Operation.created_at)))
            history = list(session.scalars(select(Operation).where(Operation.state == "finished").order_by(
                Operation.finished_at.desc()).limit(max(1, min(history_limit, 100)))))
            attention_count = session.scalar(select(func.count()).select_from(Operation).where(Operation.needs_attention.is_(True)))
            return {"round": rounds, "next_round_count": waiting, "needs_attention_count": attention_count,
                    "automatic_paused": bool((setting := session.get(ApplicationState, "automatic_paused")) and setting.value),
                    "worker_error": self.worker_error,
                    "operations": [self._operation_view(op) for op in active + history]}

    @staticmethod
    def _operation_view(op):
        return {"id": op.id, "round_id": op.round_id, "kind": op.kind, "label": op.label,
                "source": op.source,
                "state": op.state, "phase": {"name": op.phase, "unit": op.phase_unit,
                    "done": op.phase_done, "total": op.phase_total, "detail": op.detail},
                "result": op.result, "needs_attention": op.needs_attention,
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
            return {**self._operation_view(operation), "counts": counts, "has_more_items": len(found) > limit,
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
