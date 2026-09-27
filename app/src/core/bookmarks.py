"""Durable, account-scoped Pixiv bookmark operations.

This queue is deliberately separate from archive jobs. A write is sent at most
once. A crash or uncertain transport result is resolved by readback only.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from threading import RLock
import time
from uuid import UUID, uuid4

from sqlalchemy import func, select

from .models import ArchiveIntent, BookmarkOperation, RemoteBookmark, Work, utcnow


def bookmark_snapshot(bookmark, operation=None, account_id=None, work_id=None):
    """Serialize one already-loaded fact and latest operation without querying."""
    verified = bool(bookmark and bookmark.source in ("readback", "metadata"))
    cached = bool(bookmark and bookmark.value is not None and
                  (not verified or account_id is None or
                   bookmark.account_id != account_id))
    operation_is_current = bool(operation and (
        not bookmark or operation.version >= bookmark.version))
    status = (operation.status if operation_is_current else
              "confirmed" if verified else "idle")
    return {
        "work_id": work_id if work_id is not None else (
            bookmark.work_id if bookmark else operation.work_id if operation else None),
        "operation_id": operation.id if operation_is_current else None,
        "status": status,
        "desired": operation.desired if operation_is_current else None,
        "value": bookmark.value if bookmark else None,
        "confirmed_at": bookmark.confirmed_at.isoformat() + "Z" if verified and bookmark.confirmed_at else None,
        "error": operation.error if operation_is_current else bookmark.error if bookmark else None,
        "version": bookmark.version if bookmark else operation.version if operation else 0,
        "cached": cached,
    }


class BookmarkService:
    def __init__(self, database, remote, max_workers=4, max_pending=128,
                 is_available=None, current_account=None, fresh_seconds=45,
                 clock=None):
        self.database = database
        self.remote = remote
        self.max_workers = max_workers
        self.max_pending = max_pending
        # Runtime supplies connection readiness; tests with an injected remote
        # can omit it because that remote is ready from construction.
        self._is_available = is_available or (lambda: True)
        self._current_account = current_account
        self._fresh_seconds = fresh_seconds
        self._clock = clock or time.monotonic
        self._fresh = OrderedDict()
        self._last_account_id = None
        self._lock = RLock()
        self._executor = None
        self._futures = set()
        self._scheduled_ids = set()
        self._accepting = True

    def start(self):
        with self._lock:
            if self._executor is not None:
                return
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

    def _mark_fresh(self, work_id, version, account_id):
        with self._lock:
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
        """Serialize loaded rows using only recent in-process account evidence."""
        identity = work_id if work_id is not None else (
            bookmark.work_id if bookmark else operation.work_id if operation else None)
        version = bookmark.version if bookmark else operation.version if operation else 0
        account = self._fresh_account_for(identity, version) if identity is not None else None
        result = bookmark_snapshot(bookmark, operation, account_id=account,
                                   work_id=identity)
        operation_is_current = operation is not None and (
            bookmark is None or operation.version >= bookmark.version)
        if operation_is_current and operation.status in ("failed", "unconfirmed"):
            result["cached"] = result["value"] is not None
        return result

    def stop(self, timeout=5.0):
        with self._lock:
            executor = self._executor
            self._accepting = False
            active = {future for future in self._futures if not future.done()}
        if active and wait(active, timeout=timeout).not_done:
            return False
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
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
            return bool(self._scheduled_ids or any(
                not future.done() for future in self._futures))

    def _schedule(self, operation_id, readback_only=False):
        if self._executor is None:
            raise RuntimeError("收藏服务尚未启动")
        self._scheduled_ids.add(operation_id)
        future = self._executor.submit(self._run, operation_id, readback_only)
        self._futures.add(future)
        future.add_done_callback(lambda completed: self._done(operation_id, completed))

    def _done(self, operation_id, future):
        with self._lock:
            self._futures.discard(future)
            self._scheduled_ids.discard(operation_id)
            if future.exception() is not None:
                try:
                    self._finish(operation_id, "unconfirmed", "收藏核对意外中断")
                except Exception:
                    pass
            if self._accepting and self._executor is not None and self._is_available():
                self._refill_recovery()

    def _refill_recovery(self):
        """Bound queued recovery work as well as active threads."""
        room = self.max_pending - len(self._scheduled_ids)
        if room <= 0:
            return
        with self.database.read() as session:
            query = select(BookmarkOperation.id).where(
                BookmarkOperation.status == "pending")
            if self._scheduled_ids:
                query = query.where(BookmarkOperation.id.not_in(self._scheduled_ids))
            pending = session.scalars(query.order_by(
                BookmarkOperation.created_at).limit(room)).all()
        for operation_id in pending:
            self._schedule(operation_id, readback_only=True)
        return len(pending)

    @staticmethod
    def _request_id(value):
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("request_id 必须是 UUID") from exc

    def submit(self, work_id, desired, request_id):
        if type(desired) is not bool:
            raise ValueError("desired 必须是布尔值")
        request_id = self._request_id(request_id)
        return self._enqueue(work_id, desired, request_id, "write")

    def refresh(self, work_id):
        return self._enqueue(work_id, None, str(uuid4()), "refresh")

    def _enqueue(self, work_id, desired, request_id, kind):
        with self._lock:
            if not self._accepting:
                raise RuntimeError("当前正在切换图库，暂不接收收藏操作")
            if self._executor is None:
                raise RuntimeError("收藏服务尚未启动")
            with self.database.write() as session:
                existing = session.scalar(select(BookmarkOperation).where(
                    BookmarkOperation.request_id == request_id))
                if existing:
                    if existing.work_id != work_id or existing.desired != desired or existing.kind != kind:
                        raise ValueError("request_id 已用于另一项收藏操作")
                    return self._state_in_session(session, work_id)
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
                    return self._state_in_session(session, work_id)
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
                    kind=kind, version=fact.version, status="pending", phase="queued")
                session.add(operation)
                session.flush()
                result = self._state_in_session(session, work_id)
                operation_id = operation.id
            self._schedule(operation_id)
            return result

    def state(self, work_id):
        with self._lock:
            with self.database.read() as session:
                if session.get(Work, work_id) is None:
                    raise LookupError("作品不存在")
                return self._state_in_session(session, work_id)

    def _state_in_session(self, session, work_id):
        fact = session.get(RemoteBookmark, work_id)
        operation = session.scalar(select(BookmarkOperation).where(
            BookmarkOperation.work_id == work_id).order_by(
            BookmarkOperation.version.desc(), BookmarkOperation.created_at.desc()).limit(1))
        return self.snapshot(fact, operation, work_id=work_id)

    def operation(self, operation_id):
        with self._lock:
            with self.database.read() as session:
                item = session.get(BookmarkOperation, operation_id)
                if item is None:
                    raise LookupError("收藏操作不存在")
                result = self.snapshot(session.get(RemoteBookmark, item.work_id), item,
                                       work_id=item.work_id)
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
        ids = list(dict.fromkeys(work_ids))
        results = {work_id: None for work_id in ids}
        with self.database.write() as session:
            for start in range(0, len(ids), 900):
                batch = ids[start:start + 900]
                existing = set(session.scalars(select(Work.id).where(Work.id.in_(batch))))
                pending = set(session.scalars(select(BookmarkOperation.work_id).where(
                    BookmarkOperation.work_id.in_(batch),
                    BookmarkOperation.status == "pending")))
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
        with self._lock:
            with self.database.write() as session:
                fact = session.get(RemoteBookmark, work_id)
                if fact is None or fact.version != version:
                    return False
                if session.scalar(select(BookmarkOperation.id).where(
                        BookmarkOperation.work_id == work_id,
                        BookmarkOperation.status == "pending").limit(1)):
                    return False
                fact.value, fact.account_id, fact.source = value, str(account_id), source
                fact.confirmed_at, fact.error = utcnow(), None
            self._mark_fresh(work_id, version, account_id)
        return True

    def _run(self, operation_id, readback_only):
        if readback_only and not self._is_available():
            return
        with self.database.read() as session:
            item = session.get(BookmarkOperation, operation_id)
            if item is None or item.status != "pending":
                return
            work = session.get(Work, item.work_id)
            if work is None:
                self._finish(operation_id, "failed", "作品不存在")
                return
            pixiv_id = work.pixiv_illust_id
            intent = session.get(ArchiveIntent, pixiv_id)
            if intent is not None and intent.mode == "suppressed":
                self._finish(operation_id, "failed", "作品已从本地存档删除")
                return
            expected_account = item.account_id
            desired, kind = item.desired, item.kind
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
            if item.status != "pending":
                return
            item.account_id = account
            if kind == "write" and not readback_only:
                item.phase = "writing"
                item.write_started_at = utcnow()
            else:
                item.phase = "readback"
        write_error = False
        rejected = False
        if kind == "write" and not readback_only:
            try:
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
                write_error = True
                status = getattr(exc, "status", None)
                rejected = isinstance(status, int) and 400 <= status < 500
        last_read = None
        rate_limited = False
        for attempt in range(1, 4):
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
                if getattr(exc, "status", None) == 429:
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
        self._finish(operation_id, "failed" if rejected and not rate_limited else "unconfirmed",
                     "Pixiv 限流，请稍后手动核对" if rate_limited else
                     "远端状态尚未确认" if not write_error else "写入结果不确定，回读未确认")

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
        with self._lock:
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
        with self._lock:
            with self.database.write() as session:
                item = session.get(BookmarkOperation, operation_id)
                if item is None or item.status != "pending":
                    return
                item.status, item.phase, item.error = status, "finished", error
                item.finished_at = utcnow()
                work_id, version = item.work_id, item.version
            marker = self._fresh.get(work_id)
            if marker is not None and marker[0] == version:
                self._fresh.pop(work_id, None)
