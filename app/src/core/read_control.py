"""Bounded local read admission, one deadline, and cooperative SQLite cancellation."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps
import inspect
from threading import Condition, Event, Lock
from time import monotonic

from fastapi import HTTPException, Request


class LocalReadError(RuntimeError):
    """A local resource wait, never a failed remote attempt."""


class LocalReadBusy(LocalReadError):
    pass


class ReadDeadline(LocalReadError):
    pass


class ReadCancelled(LocalReadError):
    pass


class ReadBudget:
    def __init__(self, timeout=4.5, lane="background"):
        self.deadline = monotonic() + timeout
        self.lane = lane
        self.cancelled = Event()
        self._lock = Lock()
        self._connections = set()

    def checkpoint(self):
        if self.cancelled.is_set():
            raise ReadCancelled("本地读取已取消")
        if monotonic() >= self.deadline:
            raise ReadDeadline("本地读取超时，请重试")

    def stopped(self):
        return self.cancelled.is_set() or monotonic() >= self.deadline

    def cancel(self):
        # Clearing hooks and removing the connection use this same lock. An old
        # consumer can never interrupt a connection returned to a later borrower.
        with self._lock:
            self.cancelled.set()
            for connection in self._connections:
                connection.interrupt()

    def attach(self, connection):
        with self._lock:
            self.checkpoint()
            connection.set_progress_handler(lambda: int(self.stopped()), 1000)
            self._connections.add(connection)

    def detach(self, connection):
        with self._lock:
            connection.set_progress_handler(None, 0)
            self._connections.discard(connection)


current_read: ContextVar[ReadBudget | None] = ContextVar("archive_read_budget", default=None)


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
                raise LocalReadBusy("本地读取繁忙，请稍后重试")
            self.pending[lane] += 1

    def _try_acquire(self, lane):
        with self._condition:
            if self.active[lane] >= self.capacities[lane]:
                return False
            self.active[lane] += 1
            return True

    def _dequeue(self, lane):
        with self._condition:
            self.pending[lane] -= 1

    def release(self, lane):
        with self._condition:
            self.active[lane] -= 1
            self._condition.notify_all()

    def acquire_sync(self, budget):
        lane = budget.lane
        self._enqueue(lane)
        try:
            while True:
                budget.checkpoint()
                if self._try_acquire(lane):
                    return
                with self._condition:
                    self._condition.wait(min(.02, max(0, budget.deadline - monotonic())))
        finally:
            self._dequeue(lane)

    async def _execute(self, budget, function):
        lane = budget.lane
        self._enqueue(lane)
        try:
            while True:
                budget.checkpoint()
                if self._try_acquire(lane):
                    break
                await asyncio.sleep(.01)
        finally:
            self._dequeue(lane)

        def worker():
            token = current_read.set(budget)
            try:
                budget.checkpoint()
                result = function()
                budget.checkpoint()
                return result
            finally:
                current_read.reset(token)
                self.release(lane)

        # The lease belongs to the worker until it has actually finished. A
        # disk syscall that cannot be interrupted must not open another slot.
        return await asyncio.to_thread(worker)

    async def run(self, request, function, *, lane="frontend", key=None, timeout=4.5):
        loop = asyncio.get_running_loop()
        shared_key = (loop, lane, key) if key is not None else None
        job = self._shared.get(shared_key) if shared_key is not None else None
        if job is None or job[0].done() or job[1].stopped():
            budget = ReadBudget(timeout, lane)
            task = asyncio.create_task(self._execute(budget, function))
            job = [task, budget, 0]
            # Always observe exceptions after a disconnected last consumer.
            task.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)
            if shared_key is not None:
                self._shared[shared_key] = job
        task, budget, _ = job
        job[2] += 1
        consumer_deadline = monotonic() + timeout
        try:
            while not task.done():
                if request is not None and await request.is_disconnected():
                    raise ReadCancelled("本地读取已取消")
                if monotonic() >= consumer_deadline:
                    raise ReadDeadline("本地读取超时，请重试")
                await asyncio.wait({task}, timeout=min(.02, max(0, consumer_deadline - monotonic())))
            return task.result()
        finally:
            job[2] -= 1
            if job[2] == 0:
                if self._shared.get(shared_key) is job:
                    self._shared.pop(shared_key)
                if not task.done():
                    budget.cancel()
                    # Await interruptible SQL cleanup, but do not pretend that
                    # an arbitrary disk wait has finished after this bound.
                    try:
                        await asyncio.wait({task}, timeout=.9)
                    except asyncio.CancelledError:
                        pass


async def run_read(request, database, function, *, lane="frontend", key=None, timeout=4.5):
    try:
        return await database.reads.run(request, function, lane=lane, key=key, timeout=timeout)
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
            return await run_read(request, runtime.database, lambda: function(*args, **kwargs), lane=lane, key=key)
        endpoint.__signature__ = signature
        return endpoint
    return decorate
