"""One cancellable catalogue preparer, independent of browser read admission."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, InvalidStateError
from threading import Condition, Lock, Thread
import time
from uuid import uuid4

from . import diagnostics
from .library import LibraryFilter
from .read_control import LocalReadBusy, ReadBudget, ReadCancelled, current_read


class ReadingPreparation:
    MAX_SCOPES = 8
    MAX_PENDING = 64

    def __init__(self, library, make_visit):
        self.library, self.make_visit = library, make_visit
        self._condition, self._lifecycle = Condition(), Lock()
        self._thread = None
        self._stopping = False
        self._unsubscribe = None
        self._budget = None
        self._generation = -1
        self._notified = -1
        self._events = {}
        self._rows = None
        self._default_members = None
        self._stock = None
        self._scopes = OrderedDict()
        self._requests = []
        self._active_future = None
        self._retry_at, self._failures = 0., 0
        self._default_key = library._filter_key(LibraryFilter())

    def start(self):
        with self._lifecycle:
            if self._stopping:
                raise ReadCancelled("阅读清单准备已停止")
            if self._thread is None:
                # Subscription may take the writer gate. Never hold our state
                # mutex while subscribing or unsubscribing.
                self._unsubscribe = self.library.database.subscribe_catalogue_changes(self._changed)
                self._thread = Thread(target=self._run, name="reading-preparation", daemon=False)
                self._thread.start()

    def _changed(self, generation, work_ids):
        # Called in the write gate: memory operations only, no SQL or shuffle.
        with self._condition:
            if self._stopping or generation <= self._generation:
                return
            self._events.update((identity, generation) for identity in work_ids)
            self._notified = max(self._notified, generation)
            self._retry_at, self._failures = 0., 0
            self._condition.notify_all()

    def request_stop(self):
        with self._condition:
            self._stopping = True
            budget = self._budget
            pending, self._requests = self._requests, []
            self._condition.notify_all()
        if budget is not None:
            budget.cancel(reason="shutdown")
        for request in pending:
            self._finish(request[0], error=ReadCancelled("阅读清单准备已停止"))

    def stop(self, timeout=None):
        self.request_stop()
        with self._lifecycle:
            if self._unsubscribe is not None:
                self._unsubscribe()
                self._unsubscribe = None
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def prepare(self, filters, ceiling=None):
        self.start()
        future = Future()
        with self._condition:
            if self._stopping:
                future.set_exception(ReadCancelled("阅读清单准备已停止"))
                return future
            self._requests = [item for item in self._requests if not item[0].cancelled()]
            active = self._active_future is not None and not self._active_future.done()
            if len(self._requests) + int(active) >= self.MAX_PENDING:
                future.set_exception(LocalReadBusy("阅读清单准备繁忙，请稍后重试"))
                return future
            target = max(self._notified, self._generation)
            # An unfrozen relative date is intentionally unique per request.
            key = self.library._filter_key(filters)
            if filters.recent and filters.as_of is None:
                key += uuid4().hex
            self._requests.append((future, filters, ceiling, target, key))
            if self._retry_at == float("inf"):
                self._retry_at, self._failures = 0., 0
            self._condition.notify_all()
        return future

    def try_claim(self, filters, ceiling=None):
        """Legacy admitted callers use SQL rather than wait on the commit fence."""
        return None

    def _checkpoint(self):
        with self._condition:
            if self._stopping:
                raise ReadCancelled("阅读清单准备已停止")

    @staticmethod
    def _finish(future, result=None, error=None):
        try:
            if error is None:
                future.set_result(result)
            else:
                future.set_exception(error)
        except InvalidStateError:
            # A consumer can cancel between the worker's check and delivery.
            pass

    @staticmethod
    def _event(kind, generation, count, began=None, **fields):
        diagnostics.emit("reading.preparation." + kind, generation=generation, count=count,
            duration_ms=(time.monotonic() - began) * 1000 if began is not None else 0., **fields)

    def _read(self, function):
        database = self.library.database
        budget = ReadBudget(lane="background", label="reading_preparation")
        with self._condition:
            self._budget = budget
            if self._stopping:
                budget.cancel(reason="shutdown")
        admitted, token, outcome = False, None, "ok"
        try:
            database.reads.acquire_sync(budget)
            admitted = True
            token = current_read.set(budget)
            result = function()
            budget.checkpoint()
            return result
        except ReadCancelled:
            outcome = "read_cancelled"
            raise
        except Exception:
            outcome = "read_error"
            raise
        finally:
            if token is not None:
                current_read.reset(token)
            if admitted:
                database.reads.release(budget.lane, budget)
            with self._condition:
                self._budget = None
            budget.report(outcome)

    @staticmethod
    def _memory_scope(filters):
        return not (filters.show_hidden or filters.recent or filters.q.strip()
            or filters.tags is not None or filters.tags_exclude is not None
            or filters.posted_after is not None or filters.posted_before is not None)

    def _members(self, filters, ceiling):
        self._checkpoint()
        selected = []
        for index, row in enumerate(self._rows.values()):
            if index % 256 == 0:
                self._checkpoint()
            if (row[1] and (ceiling is None or row[0] <= ceiling)
                    and (filters.artist_ids is None or row[2] in filters.artist_ids)
                    and (filters.artist_ids_exclude is None or row[2] not in filters.artist_ids_exclude)
                    and (filters.types is None or row[3] in filters.types)
                    and (not filters.favorites or row[4])):
                selected.append(row)
        self._checkpoint()
        column = {"title": 6, "added": 7}.get(filters.sort, 5)
        selected.sort(key=lambda row: (row[column], row[0]), reverse=filters.sort not in ("oldest", "title"))
        self._checkpoint()
        members = []
        for index, row in enumerate(selected):
            if index % 256 == 0:
                self._checkpoint()
            members.append({"work_id": row[0], "revision_id": row[8], "page_count": row[9]})
        self._checkpoint()
        return tuple(members)

    def _refresh(self):
        self._checkpoint()
        began = time.monotonic()
        with self._condition:
            target = self._notified
            ids = frozenset(identity for identity, generation in self._events.items()
                if generation > self._generation)
        if self._rows is None:
            generation, rows = self._read(self.library.reading_basis)
            kind = "full"
        elif ids:
            generation, delta = self._read(lambda: self.library.reading_basis(ids))
            if generation > target:
                # Commit is visible before its short notification callback. Do
                # not publish a mixture of this newer delta and older members.
                with self._condition:
                    while self._notified < generation and not self._stopping:
                        self._condition.wait()
                return
            self._checkpoint()
            rows = dict(self._rows)
            for index, identity in enumerate(ids):
                if index % 256 == 0:
                    self._checkpoint()
                rows.pop(identity, None)
            rows.update(delta)
            self._checkpoint()
            generation = target
            kind = "delta"
        else:
            generation, rows, kind = target, self._rows, "delta"
        with self._condition:
            if self._stopping:
                return
            self._rows, self._generation = rows, generation
            self._notified = max(self._notified, generation)
            self._events = {identity: changed for identity, changed in self._events.items()
                if changed > generation}
            self._scopes.clear()
            self._stock = None
            self._default_members = None
        self._event(kind, generation, len(rows), began)

    def _satisfy(self, request):
        future, filters, ceiling, _target, filter_key = request
        if future.cancelled():
            return
        # A SQL commit can be visible before its enqueue callback. This fence
        # runs outside our condition so the writer can publish its generation.
        self.library.database.catalogue_commit_barrier(self._checkpoint)
        self._checkpoint()
        if future.cancelled():
            return
        with self._condition:
            generation = self._generation
            _target = max(_target, self._notified)
            if _target > generation:
                self._requests.insert(0, request)
                return
        key = (filter_key, ceiling, generation)
        default = filter_key == self._default_key and ceiling is None
        if default:
            with self._condition:
                visit, self._stock = self._stock, None
            if visit is not None:
                self._checkpoint()
                self._finish(future, visit)
                self._event("claim", generation, len(visit.members))
                return
        members = self._scopes.get(key)
        if members is None:
            if default and self._default_members is not None:
                members = self._default_members
            elif self._memory_scope(filters):
                members = self._members(filters, ceiling)
            else:
                read_generation, members = self._read(
                    lambda: self.library.reading_manifest_snapshot(filters, ceiling=ceiling))
                if read_generation != generation:
                    # Refresh the basis before assigning a result from a newer
                    # snapshot to a generation-specific cache entry.
                    with self._condition:
                        while self._notified < read_generation and not self._stopping:
                            self._condition.wait()
                        if not self._stopping:
                            self._requests.insert(0, request)
                        else:
                            self._finish(future, error=ReadCancelled("阅读清单准备已停止"))
                    return
                members = tuple(members)
            if default:
                self._default_members = members
            self._scopes[key] = members
            while len(self._scopes) > self.MAX_SCOPES:
                self._scopes.popitem(last=False)
        else:
            self._scopes.move_to_end(key)
        began = time.monotonic()
        self._checkpoint()
        visit = self.make_visit(members)
        self._checkpoint()
        self._event("prepared", generation, len(visit.members), began)
        with self._condition:
            stopping = self._stopping
        self._finish(future, visit, ReadCancelled("阅读清单准备已停止") if stopping else None)
        if not stopping:
            self._event("claim", generation, len(visit.members))

    def _run(self):
        while True:
            request = None
            with self._condition:
                self._requests = [item for item in self._requests if not item[0].cancelled()]
                while not self._stopping:
                    work = self._rows is None or self._generation < self._notified or self._requests or self._stock is None
                    delay = self._retry_at - time.monotonic()
                    if work and delay <= 0:
                        break
                    self._condition.wait(None if not work or delay == float("inf") else delay)
                if self._stopping:
                    return
            try:
                if self._rows is None or self._generation < self._notified:
                    self._refresh()
                    continue
                with self._condition:
                    if self._requests:
                        request = self._requests.pop(0)
                        self._active_future = request[0]
                if request is not None:
                    self._satisfy(request)
                else:
                    began, generation = time.monotonic(), self._generation
                    if self._default_members is None:
                        self._default_members = self._members(LibraryFilter(), None)
                    self._checkpoint()
                    visit = self.make_visit(self._default_members)
                    self._checkpoint()
                    with self._condition:
                        if not self._stopping and self._notified == generation:
                            self._stock = visit
                    self._event("prepared", generation, len(visit.members), began)
                self._failures, self._retry_at = 0, 0.
            except Exception as exc:
                with self._condition:
                    pending, self._requests = self._requests, []
                    self._failures += 1
                    self._retry_at = (time.monotonic() + .25 * 2 ** (self._failures - 1)
                        if self._failures < 3 else float("inf"))
                    stopping = self._stopping
                if request is not None:
                    pending.append(request)
                for waiting in pending:
                    self._finish(waiting[0], error=exc)
                self._event("failed", self._generation, len(pending), error_type=type(exc).__name__)
                if stopping:
                    return
            finally:
                with self._condition:
                    self._active_future = None
