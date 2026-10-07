"""Durable, account-scoped Pixiv bookmark operations.

This queue is deliberately separate from archive jobs. A write is sent at most
once. A crash or uncertain transport result is resolved by readback only.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import timedelta
from threading import Condition, Event, RLock
import time
from uuid import UUID, uuid4

from sqlalchemy import func, select

from .models import ArchiveIntent, BookmarkOperation, RemoteBookmark, Work, utcnow


def bookmark_snapshot(bookmark, operation=None, account_id=None, work_id=None,
                      fresh=False, account_epoch=0):
    """Serialize one already-loaded fact and latest operation without querying."""
    verified = bool(bookmark and bookmark.source in ("readback", "metadata")
                    and bookmark.account_id)
    cached = bool(bookmark and bookmark.value is not None and
                  (not verified or not fresh or account_id is None or
                   bookmark.account_id != account_id))
    operation_is_current = bool(operation and (
        not bookmark or operation.version >= bookmark.version) and
        (account_id is None or operation.account_id is None or
         operation.account_id == account_id))
    status = (operation.status if operation_is_current else
              "confirmed" if verified else "idle")
    error = operation.error if operation_is_current else bookmark.error if bookmark else None
    if operation_is_current and error == "写入结果不确定，回读未确认":
        action = "收藏" if operation.desired else "取消收藏"
        error = (f"{action}失败；旧记录未保存具体原因" if status == "failed" else
                 "暂时无法确认 Pixiv 上的收藏状态，已保留上次确认的状态")
    return {
        "work_id": work_id if work_id is not None else (
            bookmark.work_id if bookmark else operation.work_id if operation else None),
        "operation_id": operation.id if operation_is_current else None,
        "status": status,
        "desired": operation.desired if operation_is_current else None,
        "value": bookmark.value if bookmark else None,
        "confirmed_at": bookmark.confirmed_at.isoformat() + "Z" if verified and bookmark.confirmed_at else None,
        "error": error,
        "version": bookmark.version if bookmark else operation.version if operation else 0,
        "cached": cached,
        "verified": verified,
        "fact_account_id": bookmark.account_id if verified else None,
        "current_account_id": account_id,
        "account_epoch": account_epoch,
    }


class BookmarkService:
    def __init__(self, database, remote, max_workers=4, max_pending=128,
                 is_available=None, current_account=None, fresh_seconds=45,
                 clock=None, auto_refresh_seconds=300):
        self.database = database
        self.remote = remote
        self.max_workers = max_workers
        self.max_pending = max_pending
        # Runtime supplies connection readiness; tests with an injected remote
        # can omit it because that remote is ready from construction.
        self._is_available = is_available or (lambda: True)
        self._current_account = current_account
        self._fresh_seconds = fresh_seconds
        self._auto_refresh_seconds = auto_refresh_seconds
        self._clock = clock or time.monotonic
        self._fresh = OrderedDict()
        self._last_account_id = None
        self._observed_account_id = None
        # Microseconds fit JavaScript's safe integer range and order responses
        # across server restarts as well as account switches within a process.
        self._account_epoch = time.time_ns() // 1000
        self._lock = RLock()
        self._idle = Condition(self._lock)
        self._submitting = 0
        self._recovering = False
        self._callbacks = 0
        self._executor = None
        self._futures = set()
        self._scheduled_ids = set()
        self._accepting = True
        self._stopping = Event()

    def start(self):
        with self._lock:
            if self._executor is not None:
                return
            self._stopping.clear()
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="bookmark")
        self.reconcile_pending()

    def reconcile_pending(self):
        """Wake persisted intents only when a remote connection is ready.

        Recovery always schedules readback-only work, regardless of whether the
        original operation reached its write phase before shutdown.
        """
        with self._lock:
            available = self._is_available()
            if self._executor is None or not self._accepting or not available:
                if not available:
                    self._fresh.clear()
                return 0
        return self._refill_recovery()

    def mark_disconnected(self):
        """Drop in-process confidence when Runtime knows the connection ended."""
        with self._lock:
            self._fresh.clear()
            self._last_account_id = None

    def _account_context(self):
        with self._lock:
            try:
                current = (self._current_account() if self._current_account is not None
                           else self._last_account_id)
            except Exception:
                current = None
            current = str(current) if current is not None else None
            if current != self._observed_account_id:
                self._observed_account_id = current
                self._account_epoch = max(time.time_ns() // 1000, self._account_epoch + 1)
            return current, self._account_epoch

    def _mark_fresh(self, work_id, version, account_id, *, expected_context=None):
        with self._lock:
            if expected_context is not None and self._account_context() != expected_context:
                return
            current = self._current_account() if self._current_account is not None else account_id
            if current is None or str(current) != str(account_id):
                return
            marker = self._fresh.get(work_id)
            if marker is not None and marker[0] > version:
                return
            self._last_account_id = str(account_id)
            self._fresh[work_id] = (version, str(account_id),
                                    self._clock() + self._fresh_seconds)
            self._fresh.move_to_end(work_id)
            while len(self._fresh) > 4096:
                self._fresh.popitem(last=False)

    def _fresh_account_for(self, work_id, version):
        with self._lock:
            try:
                available = self._is_available()
            except Exception:
                available = False
            if not available:
                self._fresh.clear()
                return None
            if self._current_account is None:
                current = self._last_account_id
            else:
                try:
                    current = self._current_account()
                except Exception:
                    current = None
            marker = self._fresh.get(work_id)
            if not marker or not current or marker[0] != version or marker[1] != str(current):
                return None
            if marker[2] <= self._clock():
                self._fresh.pop(work_id, None)
                return None
            self._fresh.move_to_end(work_id)
            return str(current)

    def snapshot(self, bookmark, operation=None, work_id=None):
        """Keep durable account facts separate from short-lived remote freshness."""
        identity = work_id if work_id is not None else (
            bookmark.work_id if bookmark else operation.work_id if operation else None)
        version = bookmark.version if bookmark else operation.version if operation else 0
        account, epoch = self._account_context()
        fresh_account = self._fresh_account_for(identity, version) if identity is not None else None
        result = bookmark_snapshot(bookmark, operation, account_id=account,
                                   work_id=identity, fresh=fresh_account == account and account is not None,
                                   account_epoch=epoch)
        operation_is_current = operation is not None and (
            bookmark is None or operation.version >= bookmark.version) and (
            account is None or operation.account_id is None or operation.account_id == account)
        if operation_is_current and operation.status in ("failed", "unconfirmed"):
            result["cached"] = result["value"] is not None
        return result

    def request_stop(self):
        self._stopping.set()
        with self._lock:
            self._accepting = False
            futures = list(self._futures)
        # Cancel only calls that have not entered a worker. Their durable pending
        # intent stays available for readback-only recovery at next startup.
        for future in futures:
            future.cancel()

    def stop(self, timeout=5.0):
        deadline = None if timeout is None else time.monotonic() + timeout
        def remaining():
            return None if deadline is None else max(0, deadline - time.monotonic())
        with self._idle:
            executor = self._executor
            self._accepting = False
            if not self._idle.wait_for(lambda: not self._submitting and not self._recovering and not self._callbacks,
                                       timeout=remaining()):
                return False
            active = {future for future in self._futures if not future.done()}
        if active and wait(active, timeout=remaining()).not_done:
            return False
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=self._stopping.is_set())
        with self._lock:
            self._executor = None
            self._fresh.clear()
            self._last_account_id = None
        return True

    def accept_submissions(self, accepting: bool):
        with self._lock:
            self._accepting = bool(accepting)

    def busy(self):
        with self._lock:
            # Dormant persisted intents are safe to carry in a SQLite snapshot.
            # Scheduled work is not: the caller must retain DB/lease until it exits.
            return bool(self._submitting or self._recovering or self._callbacks or self._scheduled_ids or any(
                not future.done() for future in self._futures))

    def _schedule(self, operation_id, readback_only=False):
        with self._lock:
            if (self._executor is None or not self._accepting
                    or operation_id in self._scheduled_ids
                    or sum(not future.done() for future in self._futures) >= self.max_pending
                    or readback_only and self._submitting):
                return False
            self._scheduled_ids.add(operation_id)
            future = self._executor.submit(self._run, operation_id, readback_only)
            self._futures.add(future)
        # A completed Future calls its callback immediately. Register after
        # leaving the memory lock: recovery/finishing may need a DB connection.
        future.add_done_callback(lambda completed: self._done(operation_id, completed))
        return True

    def _done(self, operation_id, future):
        with self._lock:
            self._callbacks += 1
            self._futures.discard(future)
            self._scheduled_ids.discard(operation_id)
            recover = self._accepting and self._executor is not None and self._is_available()
        try:
            if not future.cancelled() and future.exception() is not None:
                try:
                    self._finish(operation_id, "unconfirmed", "收藏核对意外中断")
                except Exception:
                    # The persisted pending intent remains readback-only on restart.
                    pass
            if recover:
                self._refill_recovery()
        finally:
            with self._idle:
                self._callbacks -= 1
                self._idle.notify_all()

    def _refill_recovery(self):
        """Bound queued recovery work as well as active threads."""
        with self._lock:
            # A Future is complete before its callback runs. Such a callback
            # still counts toward safe shutdown, but cannot occupy a pending
            # slot and turn a newly accepted write into recovery-only work.
            room = self.max_pending - sum(not future.done() for future in self._futures)
            if room <= 0 or self._recovering or self._submitting or not self._accepting:
                return 0
            self._recovering = True
            scheduled = tuple(self._scheduled_ids)
        try:
            with self.database.read() as session:
                query = select(BookmarkOperation.id).where(BookmarkOperation.status == "pending")
                if scheduled:
                    query = query.where(BookmarkOperation.id.not_in(scheduled))
                pending = session.scalars(query.order_by(BookmarkOperation.created_at).limit(room)).all()
            return sum(self._schedule(identity, readback_only=True) for identity in pending)
        finally:
            with self._idle:
                self._recovering = False
                self._idle.notify_all()

    @staticmethod
    def _request_id(value):
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("request_id 必须是 UUID") from exc

    def submit(self, work_id, desired, request_id, expected_account_id=None):
        if type(desired) is not bool:
            raise ValueError("desired 必须是布尔值")
        request_id = self._request_id(request_id)
        if expected_account_id is not None:
            account, _ = self._account_context()
            if not account or account != str(expected_account_id):
                raise ValueError("当前 Pixiv 账号已变化，请核对后重试")
        return self._enqueue(work_id, desired, request_id, "write", expected_account_id)

    def refresh(self, work_id, automatic=False):
        if automatic:
            account, _ = self._account_context()
            with self.database.read() as session:
                if session.get(Work, work_id) is None:
                    raise LookupError("作品不存在")
                fact, operation = self._state_records(session, work_id)
                cached = (not account or not self._is_available() or
                    fact and fact.source in ("readback", "metadata") and
                    fact.account_id == account and fact.confirmed_at and
                    timedelta(0) <= utcnow() - fact.confirmed_at <
                    timedelta(seconds=self._auto_refresh_seconds))
            if cached:
                return self.snapshot(fact, operation, work_id=work_id)
        return self._enqueue(work_id, None, str(uuid4()), "refresh")

    def _enqueue(self, work_id, desired, request_id, kind, expected_account_id=None):
        with self._lock:
            if not self._accepting:
                raise RuntimeError("当前正在切换图库，暂不接收收藏操作")
            if self._executor is None:
                raise RuntimeError("收藏服务尚未启动")
            self._submitting += 1
        try:
            return self._persist_submission(work_id, desired, request_id, kind, expected_account_id)
        finally:
            with self._idle:
                self._submitting -= 1
                self._idle.notify_all()

    def _persist_submission(self, work_id, desired, request_id, kind, expected_account_id):
        operation_id = None
        with self.database.write() as session:
            operation_id = self._enqueue_in_session(session, work_id, desired, request_id, kind, expected_account_id)
            records = self._state_records(session, work_id)
        if operation_id is not None:
            self._schedule(operation_id)
        return self.snapshot(*records, work_id=work_id)

    def _enqueue_in_session(self, session, work_id, desired, request_id, kind, expected_account_id):
        existing = session.scalar(select(BookmarkOperation).where(
            BookmarkOperation.request_id == request_id))
        if existing:
            if (existing.work_id != work_id or existing.desired != desired or existing.kind != kind
                    or expected_account_id is not None and existing.account_id != str(expected_account_id)):
                raise ValueError("request_id 已用于另一项收藏操作")
            return None
        work = session.get(Work, work_id)
        if work is None:
            raise LookupError("作品不存在")
        intent = session.get(ArchiveIntent, work.pixiv_illust_id)
        if intent is not None and intent.mode == "suppressed":
            raise LookupError("作品已从本地存档删除")
        existing = session.scalar(select(BookmarkOperation).where(
            BookmarkOperation.work_id == work_id,
            BookmarkOperation.status == "pending").order_by(
            BookmarkOperation.version.desc()).limit(1))
        if existing:
            if expected_account_id is not None and existing.account_id != str(expected_account_id):
                raise ValueError("另一账号的收藏操作尚未核对，请稍后重试")
            return None
        pending_total = session.scalar(select(func.count()).select_from(
            BookmarkOperation).where(BookmarkOperation.status == "pending"))
        if pending_total >= self.max_pending:
            raise RuntimeError("收藏操作繁忙，请稍后重试")
        fact = session.get(RemoteBookmark, work_id)
        if fact is None:
            fact = RemoteBookmark(work_id=work_id, value=None,
                                  source="legacy_cache", version=0)
            session.add(fact)
        fact.version += 1
        operation = BookmarkOperation(
            request_id=request_id, work_id=work_id, desired=desired,
            kind=kind, version=fact.version, status="pending", phase="queued",
            account_id=str(expected_account_id) if expected_account_id is not None else None)
        session.add(operation)
        session.flush()
        return operation.id

    def state(self, work_id):
        with self.database.read() as session:
            if session.get(Work, work_id) is None:
                raise LookupError("作品不存在")
            records = self._state_records(session, work_id)
        return self.snapshot(*records, work_id=work_id)

    def _state_records(self, session, work_id):
        fact = session.get(RemoteBookmark, work_id)
        operation = session.scalar(select(BookmarkOperation).where(
            BookmarkOperation.work_id == work_id).order_by(
            BookmarkOperation.version.desc(), BookmarkOperation.created_at.desc()).limit(1))
        return fact, operation

    def operation(self, operation_id):
        with self.database.read() as session:
            item = session.get(BookmarkOperation, operation_id)
            if item is None:
                raise LookupError("收藏操作不存在")
            fact = session.get(RemoteBookmark, item.work_id)
        result = self.snapshot(fact, item, work_id=item.work_id)
        result.update({"id": item.id, "request_id": item.request_id,
                       "kind": item.kind, "phase": item.phase,
                       "read_attempts": item.read_attempts,
                       "created_at": item.created_at.isoformat() + "Z"})
        result.update({"operation_id": item.id, "status": item.status,
                       "desired": item.desired, "error": item.error,
                       "version": item.version})
        return result

    def begin_observation(self, work_id):
        """Reserve a version before a remote metadata request begins."""
        return self.begin_observations([work_id]).get(work_id)

    def begin_observations(self, work_ids):
        """Reserve versions for a page in one transaction before network work."""
        with self.database.write() as session:
            return self._begin_observations(session, work_ids)

    def _begin_observations(self, session, work_ids):
        """Reserve using an explicitly owned transaction; no I/O or cache publication."""
        ids = list(dict.fromkeys(work_ids))
        results = {work_id: None for work_id in ids}
        for start in range(0, len(ids), 900):
            batch = ids[start:start + 900]
            existing = set(session.scalars(select(Work.id).where(Work.id.in_(batch))))
            pending = set(session.scalars(select(BookmarkOperation.work_id).where(
                BookmarkOperation.work_id.in_(batch), BookmarkOperation.status == "pending")))
            facts = {fact.work_id: fact for fact in session.scalars(
                select(RemoteBookmark).where(RemoteBookmark.work_id.in_(batch)))}
            for work_id in batch:
                if work_id not in existing or work_id in pending:
                    continue
                fact = facts.get(work_id)
                if fact is None:
                    fact = RemoteBookmark(work_id=work_id, value=None,
                                          source="legacy_cache", version=0)
                    session.add(fact)
                fact.version += 1
                results[work_id] = fact.version
        return results

    def apply_observation(self, work_id, value, account_id, version, source="metadata"):
        if type(value) is not bool or not account_id or version is None:
            return False
        account_context = self._account_context()
        if self._current_account is not None and account_context[0] != str(account_id):
            return False
        with self.database.write() as session:
            applied = (self._account_context() == account_context and
                self._apply_observation(session, work_id, value, account_id, version, source))
        if applied:
            self._mark_fresh(work_id, version, account_id, expected_context=account_context)
        return applied

    def _apply_observation(self, session, work_id, value, account_id, version, source="metadata"):
        """Apply durable facts only; the transaction owner publishes freshness after commit."""
        if type(value) is not bool or not account_id or version is None:
            return False
        if self._current_account is not None and self._account_context()[0] != str(account_id):
            return False
        fact = session.get(RemoteBookmark, work_id)
        if fact is None or fact.version != version:
            return False
        if session.scalar(select(BookmarkOperation.id).where(
                BookmarkOperation.work_id == work_id,
                BookmarkOperation.status == "pending").limit(1)):
            return False
        fact.value, fact.account_id, fact.source = value, str(account_id), source
        fact.confirmed_at, fact.error = utcnow(), None
        return True

    def _run(self, operation_id, readback_only):
        from ..request_policy import AccountChanged, RequestDeferred, ShutdownRequested
        from .read_control import LocalReadError
        try:
            for pressure_attempt in range(3):
                try:
                    self._run_active(operation_id, readback_only)
                    return
                except LocalReadError:
                    if self._stopping.wait(.1 * (pressure_attempt + 1)):
                        return
            self._finish(operation_id, "unconfirmed", "本地读取繁忙，收藏状态待确认")
        except ShutdownRequested:
            # A write may already have reached Pixiv: retain pending for a
            # readback on restart, never mark it successful or replay the write.
            return
        except (AccountChanged, RequestDeferred):
            self._finish(operation_id, "unconfirmed", "账号或连接状态变化，收藏状态待确认")

    def _run_active(self, operation_id, readback_only):
        if self._stopping.is_set():
            return
        if readback_only and not self._is_available():
            return
        early_error = None
        with self.database.read() as session:
            item = session.get(BookmarkOperation, operation_id)
            if item is None or item.status != "pending":
                return
            work = session.get(Work, item.work_id)
            if work is None:
                early_error = "作品不存在"
                pixiv_id = None
            else:
                pixiv_id = work.pixiv_illust_id
                intent = session.get(ArchiveIntent, pixiv_id)
                if intent is not None and intent.mode == "suppressed":
                    early_error = "作品已从本地存档删除"
            expected_account = item.account_id
            desired, kind = item.desired, item.kind
            # A local read can be interrupted after a remote write. Retrying
            # that local boundary must never send the intent a second time.
            readback_only = readback_only or item.write_started_at is not None
        if early_error:
            self._finish(operation_id, "failed", early_error)
            return
        try:
            account = self._account_id()
            if expected_account and account != expected_account:
                raise RuntimeError("账号已变化")
        except Exception:
            if readback_only and not self._is_available():
                return
            self._finish(operation_id, "unconfirmed", "无法确认当前账号")
            return
        with self.database.write() as session:
            item = session.get(BookmarkOperation, operation_id)
            if item.status != "pending" or self._stopping.is_set():
                return
            item.account_id = account
            if kind == "write" and not readback_only:
                item.phase = "writing"
                item.write_started_at = utcnow()
            else:
                item.phase = "readback"
        rejected = False
        write_reason = None
        write_rate_limited = False
        if kind == "write" and not readback_only:
            try:
                if self._stopping.is_set():
                    return
                if self._is_suppressed(pixiv_id):
                    self._finish(operation_id, "failed", "作品已从本地存档删除")
                    return
                if self._account_id() != account:
                    raise RuntimeError("账号已变化")
                if hasattr(self.remote, "write_bookmark"):
                    self.remote.write_bookmark(pixiv_id, desired, account)
                elif desired:
                    self.remote.add_bookmark(pixiv_id, restrict="public")
                else:
                    self.remote.delete_bookmark(pixiv_id)
            except Exception as exc:
                status = getattr(exc, "status", None)
                rejected = isinstance(status, int) and 400 <= status < 500 and status not in (408, 429)
                write_rate_limited = status == 429 or getattr(exc, "failure_code", None) == "api_rate_limit"
                # Only the request layer's deliberately safe message is kept;
                # raw exceptions may contain request URLs or credentials.
                write_reason = getattr(exc, "user_message", None)
                if not isinstance(write_reason, str) or not write_reason:
                    write_reason = f"请求未成功（{status}）" if rejected else None
        last_read = None
        rate_limited = False
        for attempt in range(1, 4):
            if self._stopping.is_set():
                return
            if self._is_suppressed(pixiv_id):
                self._finish(operation_id, "failed", "作品已从本地存档删除")
                return
            with self.database.write() as session:
                item = session.get(BookmarkOperation, operation_id)
                if item.status != "pending":
                    return
                item.phase, item.read_attempts = "readback", attempt
            try:
                if self._account_id() != account:
                    raise RuntimeError("账号已变化")
                if hasattr(self.remote, "read_bookmark_bound"):
                    value = self.remote.read_bookmark_bound(pixiv_id, account)
                else:
                    value = self.remote.read_bookmark(pixiv_id)
                if self._account_id() != account:
                    raise RuntimeError("账号已变化")
            except Exception as exc:
                if readback_only and not self._is_available():
                    return
                if (getattr(exc, "status", None) == 429 or
                        getattr(exc, "failure_code", None) == "api_rate_limit"):
                    rate_limited = True
                    break
                value = None
            if type(value) is not bool:
                continue
            last_read = value
            if kind == "refresh" or value == desired:
                self._confirm(operation_id, value, account)
                return
            # A mismatch may be propagation delay. All three reads are bounded.
        if last_read is not None:
            self._record_readback(operation_id, last_read, account)
        action = "收藏" if desired else "取消收藏"
        if rejected:
            # A later unavailable read does not make a definite write rejection
            # ambiguous. A matching read above can still confirm the intent.
            self._finish(operation_id, "failed", f"{action}失败：{write_reason}")
        elif rate_limited or write_rate_limited:
            self._finish(operation_id, "unconfirmed",
                         "Pixiv 暂时限制请求，收藏状态待确认；已保留上次确认的状态")
        else:
            self._finish(operation_id, "unconfirmed",
                         "暂时无法确认 Pixiv 上的收藏状态，已保留上次确认的状态")

    def _is_suppressed(self, pixiv_id):
        with self.database.read() as session:
            intent = session.get(ArchiveIntent, pixiv_id)
            return intent is not None and intent.mode == "suppressed"

    def _account_id(self):
        value = self.remote.get_account_id()
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
            raise RuntimeError("无法确认账号")
        return str(value)

    def _record_readback(self, operation_id, value, account):
        with self.database.write() as session:
            item = session.get(BookmarkOperation, operation_id)
            fact = session.get(RemoteBookmark, item.work_id)
            if item.status != "pending" or fact.version != item.version:
                return
            fact.value, fact.account_id, fact.source = value, account, "readback"
            fact.confirmed_at, fact.error = utcnow(), None

    def _confirm(self, operation_id, value, account):
        with self.database.write() as session:
            item = session.get(BookmarkOperation, operation_id)
            fact = session.get(RemoteBookmark, item.work_id)
            if item.status != "pending" or fact.version != item.version:
                return
            fact.value, fact.account_id, fact.source = value, account, "readback"
            fact.confirmed_at, fact.error = utcnow(), None
            item.status, item.phase, item.error = "confirmed", "finished", None
            item.finished_at = utcnow()
            confirmed = (item.work_id, item.version)
        self._mark_fresh(*confirmed, account)

    def _finish(self, operation_id, status, error):
        with self.database.write() as session:
            item = session.get(BookmarkOperation, operation_id)
            if item is None or item.status != "pending":
                return
            item.status, item.phase, item.error = status, "finished", error
            item.finished_at = utcnow()
            work_id, version = item.work_id, item.version
        with self._lock:
            marker = self._fresh.get(work_id)
            if marker is not None and marker[0] == version:
                self._fresh.pop(work_id, None)
