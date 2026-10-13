"""Bounded local read admission, one deadline, and cooperative SQLite cancellation."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import logging
from threading import Condition, Event, Lock
from time import monotonic
from uuid import uuid4

from fastapi import HTTPException, Request
from . import diagnostics


log = logging.getLogger(__name__)
_READ_STAGES = frozenset(("progress_active_hydrate", "progress_history_hydrate",
    "library_works", "library_metadata", "library_revisions", "library_bookmarks",
    "library_states", "library_media"))


def _read_fields(budget):
    return {"read_ref": diagnostics.ref(budget.request_id), "lane": budget.lane,
        "endpoint": budget.label}


def _read_event(event, budget, **fields):
    if diagnostics.enabled():
        diagnostics.emit(event, **_read_fields(budget), **fields)


@contextmanager
def _read_span(event, budget, **fields):
    if not diagnostics.enabled():
        yield
        return
    with diagnostics.span(event, **_read_fields(budget), **fields):
        yield


class LocalReadError(RuntimeError):
    """A local resource wait, never a failed remote attempt."""


class LocalReadBusy(LocalReadError):
    pass


class ReadDeadline(LocalReadError):
    pass


class ReadCancelled(LocalReadError):
    pass


class ReadBudget:
    def __init__(self, timeout=4.5, lane="background", label="local_read"):
        self.started = monotonic()
        self.deadline = self.started + timeout
        self.lane = lane
        self.label = label
        self.request_id = uuid4().hex[:12]
        self.admitted = None
        self.pool_seconds = 0.0
        self.database_seconds = 0.0
        self.sql_seconds = 0.0
        self.statement_count = 0
        self.last_read_stage = None
        self.last_stage_seconds = 0.0
        self.slowest_read_stage = None
        self.slowest_stage_seconds = 0.0
        self.cancelled = Event()
        self.cancelled_by_deadline = False
        self._lock = Lock()
        self._connections = set()
        self.diagnostic_cancel_at = None
        _read_event("read.budget_created", self, timeout_seconds=timeout)

    def report(self, outcome):
        elapsed = monotonic() - self.started
        _read_event("read.completed", self, outcome=outcome, total_ms=elapsed * 1000,
            queue_ms=((self.admitted - self.started) if self.admitted is not None else elapsed) * 1000,
            pool_ms=self.pool_seconds * 1000, database_ms=self.database_seconds * 1000,
            execute_ms=self.sql_seconds * 1000, statement_count=self.statement_count,
            last_read_stage=self.last_read_stage, last_stage_ms=self.last_stage_seconds * 1000)
        if (outcome == "ok" and elapsed < 1.0) or outcome == "read_cancelled":
            return
        wait = (self.admitted - self.started) if self.admitted is not None else elapsed
        stage_detail = ""
        if self.last_read_stage is not None:
            stage_detail = (f" last_read_stage={self.last_read_stage}"
                f" last_stage_ms={self.last_stage_seconds * 1000:.1f}"
                f" slowest_read_stage={self.slowest_read_stage}"
                f" slowest_stage_ms={self.slowest_stage_seconds * 1000:.1f}")
        # Only fixed endpoint names and timings: no URL, filters, SQL, paths or
        # exception text can leak artwork metadata or credentials into the log.
        log.warning("local_read id=%s endpoint=%s outcome=%s lane=%s total_ms=%.1f "
                    "queue_ms=%.1f pool_ms=%.1f database_ms=%.1f execute_ms=%.1f statements=%d%s",
                    self.request_id, self.label, outcome, self.lane, elapsed * 1000,
                    wait * 1000, self.pool_seconds * 1000, self.database_seconds * 1000,
                    self.sql_seconds * 1000, self.statement_count, stage_detail)

    def checkpoint(self):
        if self.cancelled.is_set():
            _read_event("read.checkpoint_rejected", self,
                reason="deadline_cancelled" if self.cancelled_by_deadline else "cancelled")
            if self.cancelled_by_deadline:
                raise ReadDeadline("本地读取超时，请重试")
            raise ReadCancelled("本地读取已取消")
        if monotonic() >= self.deadline:
            _read_event("read.checkpoint_rejected", self, reason="deadline")
            raise ReadDeadline("本地读取超时，请重试")

    def stopped(self):
        return self.cancelled.is_set() or monotonic() >= self.deadline

    def cancel(self, *, timed_out=False):
        # Clearing hooks and removing the connection use this same lock. An old
        # consumer can never interrupt a connection returned to a later borrower.
        with self._lock:
            self.cancelled_by_deadline |= timed_out
            self.cancelled.set()
            if diagnostics.enabled() and self.diagnostic_cancel_at is None:
                self.diagnostic_cancel_at = monotonic()
            _read_event("read.cancel", self, reason="deadline" if timed_out else "consumer_gone",
                connection_count=len(self._connections))
            for connection in self._connections:
                connection.interrupt()

    def attach(self, connection):
        with self._lock:
            self.checkpoint()
            connection.set_progress_handler(lambda: int(self.stopped()), 1000)
            self._connections.add(connection)
            _read_event("read.connection_attached", self, connection_count=len(self._connections))

    def detach(self, connection):
        with self._lock:
            connection.set_progress_handler(None, 0)
            self._connections.discard(connection)
            _read_event("read.connection_detached", self, connection_count=len(self._connections))


current_read: ContextVar[ReadBudget | None] = ContextVar("archive_read_budget", default=None)


@contextmanager
def track_read_stage(stage):
    """Time fixed hydration scopes, including result fetching and decoding."""
    budget = current_read.get()
    if budget is None or stage not in _READ_STAGES:
        yield
        return
    began = monotonic()
    try:
        with _read_span("read.stage", budget, stage=stage):
            yield
    finally:
        elapsed = monotonic() - began
        budget.last_read_stage, budget.last_stage_seconds = stage, elapsed
        if budget.slowest_read_stage is None or elapsed > budget.slowest_stage_seconds:
            budget.slowest_read_stage, budget.slowest_stage_seconds = stage, elapsed


class ReadController:
    def __init__(self):
        self.capacities = {"frontend": 4, "background": 2, "logs": 1}
        self.queue_bounds = {"frontend": 64, "background": 64, "logs": 8}
        self._condition = Condition()
        self.active = dict.fromkeys(self.capacities, 0)
        self.pending = dict.fromkeys(self.capacities, 0)
        self._shared = {}
        self._generation = 0

    @property
    def generation(self):
        with self._condition:
            return self._generation

    def advance_generation(self):
        """Fence old in-flight snapshots after a successful local change."""
        with self._condition:
            self._generation += 1

    def _enqueue(self, lane):
        with self._condition:
            if self.pending[lane] >= self.queue_bounds[lane]:
                if diagnostics.enabled():
                    diagnostics.emit("read.queue_rejected", lane=lane, pending_count=self.pending[lane],
                        active_count=self.active[lane], capacity=self.capacities[lane], queue_bound=self.queue_bounds[lane])
                raise LocalReadBusy("本地读取繁忙，请稍后重试")
            self.pending[lane] += 1
            if diagnostics.enabled():
                diagnostics.emit("read.queue_enqueued", lane=lane, pending_count=self.pending[lane],
                    active_count=self.active[lane], capacity=self.capacities[lane])

    def _try_acquire(self, lane):
        with self._condition:
            if self.active[lane] >= self.capacities[lane]:
                return False
            self.active[lane] += 1
            return True

    def _dequeue(self, lane):
        with self._condition:
            self.pending[lane] -= 1
            if diagnostics.enabled():
                diagnostics.emit("read.queue_dequeued", lane=lane, pending_count=self.pending[lane],
                    active_count=self.active[lane])

    def release(self, lane):
        with self._condition:
            self.active[lane] -= 1
            if diagnostics.enabled():
                diagnostics.emit("read.admission_released", lane=lane, active_count=self.active[lane], pending_count=self.pending[lane])
            self._condition.notify_all()

    def acquire_sync(self, budget):
        lane = budget.lane
        self._enqueue(lane)
        try:
            with _read_span("read.admission_wait", budget, mode="sync"):
                while True:
                    budget.checkpoint()
                    if self._try_acquire(lane):
                        budget.admitted = monotonic()
                        _read_event("read.admitted", budget, queue_ms=(budget.admitted - budget.started) * 1000,
                            active_count=self.active[lane], pending_count=self.pending[lane], capacity=self.capacities[lane])
                        return
                    with self._condition:
                        self._condition.wait(min(.02, max(0, budget.deadline - monotonic())))
        finally:
            self._dequeue(lane)

    async def _execute(self, budget, function):
        outcome = "ok"
        try:
            with _read_span("read.execute", budget):
                return await self._execute_admitted(budget, function)
        except ReadDeadline:
            outcome = "read_timeout"
            raise
        except LocalReadBusy:
            outcome = "read_busy"
            raise
        except (ReadCancelled, asyncio.CancelledError):
            outcome = "read_cancelled"
            raise
        except HTTPException as exc:
            outcome = "ok" if exc.status_code < 500 else f"http_{exc.status_code}"
            raise
        except Exception:
            outcome = "read_error"
            raise
        finally:
            budget.report(outcome)

    async def _execute_admitted(self, budget, function):
        lane = budget.lane
        self._enqueue(lane)
        try:
            with _read_span("read.admission_wait", budget, mode="async"):
                while True:
                    budget.checkpoint()
                    if self._try_acquire(lane):
                        budget.admitted = monotonic()
                        _read_event("read.admitted", budget, queue_ms=(budget.admitted - budget.started) * 1000,
                            active_count=self.active[lane], pending_count=self.pending[lane], capacity=self.capacities[lane])
                        break
                    await asyncio.sleep(.01)
        finally:
            self._dequeue(lane)

        def worker():
            token = current_read.set(budget)
            try:
                _read_event("read.worker_started", budget,
                    dispatch_wait_ms=(monotonic() - budget.admitted) * 1000)
                with _read_span("read.worker", budget):
                    budget.checkpoint()
                    result = function()
                    budget.checkpoint()
                    return result
            finally:
                _read_event("read.worker_cleanup", budget, cancelled=budget.cancelled.is_set(),
                    after_cancel_ms=(monotonic() - budget.diagnostic_cancel_at) * 1000
                        if budget.diagnostic_cancel_at is not None else None)
                current_read.reset(token)
                self.release(lane)

        # The lease belongs to the worker until it has actually finished. A
        # disk syscall that cannot be interrupted must not open another slot.
        with _read_span("read.thread_wait", budget):
            return await asyncio.to_thread(worker)

    async def run(self, request, function, *, lane="frontend", key=None, timeout=4.5, label="local_read"):
        loop = asyncio.get_running_loop()
        shared_key = (loop, lane, key) if key is not None else None
        job = self._shared.get(shared_key) if shared_key is not None else None
        if job is None or job[0].done() or job[1].stopped():
            budget = ReadBudget(timeout, lane, label)
            task = asyncio.create_task(self._execute(budget, function))
            job = [task, budget, 0]
            # Always observe exceptions after a disconnected last consumer.
            task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
            if shared_key is not None:
                self._shared[shared_key] = job
            _read_event("read.shared_created", budget, shared=shared_key is not None)
        else:
            _read_event("read.shared_reused", job[1], existing_consumer_count=job[2])
        task, budget, _ = job
        job[2] += 1
        _read_event("read.consumer_joined", budget, consumer_count=job[2],
            http_request_id=getattr(getattr(request, "state", None), "diagnostic_request_id", None))
        consumer_deadline = monotonic() + timeout
        consumer_timed_out = False
        try:
            with _read_span("read.consumer_wait", budget):
                while not task.done():
                    if request is not None and await request.is_disconnected():
                        _read_event("read.consumer_disconnected", budget, consumer_count=job[2])
                        raise ReadCancelled("本地读取已取消")
                    if monotonic() >= consumer_deadline:
                        _read_event("read.consumer_deadline", budget, consumer_count=job[2])
                        raise ReadDeadline("本地读取超时，请重试")
                    await asyncio.wait({task}, timeout=min(.02, max(0, consumer_deadline - monotonic())))
                return task.result()
        except ReadDeadline:
            consumer_timed_out = True
            raise
        finally:
            job[2] -= 1
            _read_event("read.consumer_left", budget, consumer_count=job[2], task_done=task.done(), timed_out=consumer_timed_out)
            if job[2] == 0:
                if self._shared.get(shared_key) is job:
                    self._shared.pop(shared_key)
                    _read_event("read.shared_removed", budget, task_done=task.done())
                if not task.done():
                    budget.cancel(timed_out=consumer_timed_out)
                    # Await interruptible SQL cleanup, but do not pretend that
                    # an arbitrary disk wait has finished after this bound.
                    try:
                        with _read_span("read.cancel_cleanup_wait", budget, timeout_seconds=.9):
                            await asyncio.wait({task}, timeout=.9)
                        _read_event("read.cancel_cleanup_result", budget, task_done=task.done())
                    except asyncio.CancelledError:
                        pass


async def run_read(request, database, function, *, lane="frontend", key=None, timeout=4.5, label="local_read"):
    try:
        return await database.reads.run(request, function, lane=lane, key=key, timeout=timeout, label=label)
    except ReadCancelled as exc:
        raise HTTPException(499, {"code": "read_cancelled", "message": str(exc)}) from exc
    except ReadDeadline as exc:
        raise HTTPException(504, {"code": "read_timeout", "message": str(exc)}) from exc
    except LocalReadBusy as exc:
        raise HTTPException(503, {"code": "read_busy", "message": str(exc)}, headers={"Retry-After": "1"}) from exc


def local_read(get_runtime, *, lane="frontend", coalesce=False, coalesce_context=None):
    """Keep endpoint validation while dispatching only after async admission."""
    def decorate(function):
        signature = inspect.signature(function, eval_str=True)
        request_name = next((name for name, value in signature.parameters.items()
                             if value.annotation is Request), None)
        injected = request_name is None
        request_name = request_name or "_read_request"
        if injected:
            signature = signature.replace(parameters=[*signature.parameters.values(),
                inspect.Parameter(request_name, inspect.Parameter.KEYWORD_ONLY, annotation=Request)])

        @wraps(function)
        async def endpoint(*args, **kwargs):
            request = kwargs.get(request_name)
            if injected:
                kwargs.pop(request_name, None)
            runtime = get_runtime()
            if runtime is None:
                raise HTTPException(503, "图库尚未准备好")
            key = None
            if coalesce and request is not None and request.method == "GET":
                snapshot = getattr(runtime, "client_snapshot", None)
                client, generation = snapshot() if callable(snapshot) else (getattr(runtime, "client", None), None)
                # Only short memory snapshots: never resolve credentials, query
                # SQL or wait for the writer while preparing the sharing key.
                account = getattr(runtime, "account_id", None)
                key = (str(request.url), runtime.database.reads.generation,
                       id(runtime), id(client), generation, account,
                       coalesce_context(runtime) if coalesce_context is not None else None)
            try:
                return await run_read(request, runtime.database, lambda: function(*args, **kwargs), lane=lane, key=key, label=function.__name__)
            finally:
                if diagnostics.enabled() and request is not None:
                    request.scope.setdefault("state", {})["diagnostic_business_ready_at"] = monotonic()
        endpoint.__signature__ = signature
        return endpoint
    return decorate
