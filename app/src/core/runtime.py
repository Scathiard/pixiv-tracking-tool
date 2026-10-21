"""One owned local library and one task worker. Authentication is optional."""
from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
from stat import S_ISREG
import threading
import time
from uuid import uuid4
from contextlib import contextmanager, nullcontext

from sqlalchemy import func, select

from .archive import ArchiveHandler
from .bookmarks import BookmarkService
from .database import ArchiveDatabase
from .library import Library
from .reading import ReadingSessions
from .media import Publisher, contained_path
from .maintenance import MissingMediaMaintenance
from .models import ArchiveIntent, Artist, Asset, Derivative, Operation, PublicationJournal, TrackingPolicy, Work, utcnow
from .ownership import LibraryLease
from .profiles import ArtistProfiles
from .reconcile import ReconcileHandler
from .source_status import ArtistSourceHandler, SourceStatus
from .tasks import TaskQueue
from .wal_maintenance import WalMaintenance
from . import diagnostics


class LoginRequired(RuntimeError):
    user_message = "此操作需要连接 Pixiv；本地图库仍可继续使用"


class LibraryBusy(RuntimeError):
    user_message = "还有未完成任务。请等本轮结束后，再删除整位画师的存档"


class RemoteAccess:
    def __init__(self, runtime):
        self.runtime = runtime
        self._task_context = threading.local()

    def _binding(self):
        binding = getattr(self._task_context, "binding", None)
        if binding is not None:
            self.assert_generation(binding[1])
            return binding
        client, generation = self.runtime.client_snapshot()
        return client, self._generation(client, generation)

    def _generation(self, client, generation):
        method = getattr(type(client), "identity_generation", None)
        identity = client.identity_generation() if callable(method) else None
        return {"runtime": self.runtime._runtime_epoch, "client": generation,
            "identity": list(identity) if identity is not None else None}

    def generation_context(self):
        return dict(self._binding()[1])

    def assert_generation(self, expected):
        from ..request_policy import AccountChanged
        client, generation = self.runtime.client_snapshot()
        if self._generation(client, generation) != expected:
            raise AccountChanged()

    @contextmanager
    def task_scope(self, context):
        """Explicit protocol: TaskQueue detects it on the proxy's type."""
        client, generation = self.runtime.client_snapshot()
        previous = getattr(self._task_context, "binding", None)
        previous_guard = getattr(context, "remote_guard", None)
        expected = self._generation(client, generation)
        self._task_context.binding = (client, expected)
        context.remote_guard = lambda: self.assert_generation(expected)
        scope_factory = getattr(type(client), "task_scope", None)
        try:
            with scope_factory(client, context) if callable(scope_factory) else nullcontext():
                yield
        finally:
            context.remote_guard = previous_guard
            self._task_context.binding = previous

    def __getattr__(self, name):
        if self.runtime._closing.is_set():
            from ..request_policy import ShutdownRequested
            raise ShutdownRequested()
        client, expected = self._binding()
        if client is None:
            raise LoginRequired(LoginRequired.user_message)
        value = getattr(client, name)
        if not callable(value):
            return value
        def invoke(*args, **kwargs):
            self.assert_generation(expected)
            try:
                result = value(*args, **kwargs)
            except Exception as exc:
                if getattr(self._task_context, "binding", None) is not None and getattr(exc, "retryable", False):
                    from ..request_policy import TaskRemoteRetry
                    raise TaskRemoteRetry(exc) from None
                raise
            self.assert_generation(expected)
            return result
        return invoke

    def get_account_id(self):
        if self.runtime._closing.is_set():
            from ..request_policy import ShutdownRequested
            raise ShutdownRequested()
        client, expected = self._binding()
        if client is None:
            self.runtime.publish_account(expected, None)
            self.runtime.bookmarks.mark_disconnected()
            raise LoginRequired(LoginRequired.user_message)
        account_id = client.get_account_id()
        # A legacy token can learn its identity through a refresh. That is the
        # same login only when the client/runtime and login generation agree;
        # a known account never changes under an already dispatched scope.
        current_client, generation = self.runtime.client_snapshot()
        current_generation = self._generation(current_client, generation)
        previous_identity, identity = expected.get("identity"), current_generation.get("identity")
        if (previous_identity is not None and not previous_identity[1] and identity is not None
                and previous_identity[0] == identity[0] and identity[1] == str(account_id)
                and {key: value for key, value in expected.items() if key != "identity"} ==
                    {key: value for key, value in current_generation.items() if key != "identity"}):
            expected["identity"] = identity
        # Local catalogue responses remain explicitly cached. An OAuth identity
        # alone does not prove that the current network can reach Pixiv.
        return self.runtime.publish_account(expected, account_id)


@contextmanager
def _startup_phase(name):
    began = time.perf_counter()
    print(f"startup phase={name} state=begin", flush=True)
    state = "ok"
    try:
        yield
    except BaseException:
        state = "error"
        raise
    finally:
        print(f"startup phase={name} state={state} elapsed_ms={(time.perf_counter() - began) * 1000:.1f}", flush=True)


class Runtime:
    def __init__(self, database_path: Path, images: Path, *, create=False, client=None, downloader=None,
                 index_progress=None, check_workers=1):
        self._client_lock = threading.Lock()
        self._runtime_epoch = uuid4().hex
        self._client = None
        self._client_generation = 0
        self._published_account_context = None
        self._closing = threading.Event()
        self._closed = False
        self._started = False
        self._close_lock = threading.Lock()
        self.lease = LibraryLease(images).acquire()
        try:
            from .upgrade import guard_upgrade_recovery
            guard_upgrade_recovery(database_path)
            with _startup_phase("database_open"):
                self.database = ArchiveDatabase.create(database_path) if create else ArchiveDatabase(database_path)
            from .index_preparation import prepare_task_indexes
            with _startup_phase("query_indexes"):
                self.index_preparation = prepare_task_indexes(self.database, self.lease, progress=index_progress)
                self.database.refresh_schema_contract_after_index_preparation()
        except BaseException:
            if hasattr(self, 'database'):
                self.database.close()
            self.lease.close()
            raise
        self.images, self.client = Path(images).resolve(), client
        self.wal_maintenance = WalMaintenance(self.database.path)
        self.account_id = None
        self.library = Library(self.database)
        self.reading = ReadingSessions(self.library)
        self.publisher = Publisher(self.database, self.images)
        remote = RemoteAccess(self)
        self.remote = remote
        self.bookmarks = BookmarkService(self.database, remote,
                                         is_available=lambda: self.client is not None,
                                         current_account=lambda: self.account_id)
        self.library.bookmarks = self.bookmarks
        self.source_status = SourceStatus(self.database, remote)
        self.profiles = ArtistProfiles(self.database, self.publisher, remote, source_status=self.source_status)
        self.queue = TaskQueue(self.database, {
            kind: ArchiveHandler(kind, self.database, self.publisher, remote, downloader,
                                 bookmarks=self.bookmarks, profiles=self.profiles, source_status=self.source_status)
            for kind in ("check", "metadata", "bookmarks", "repair", "verify", "backfill", "thumbnails", "delete")},
            allowed_kinds=lambda: None if self.client is not None else ("verify", "thumbnails", "delete"),
            check_workers=check_workers)
        self.queue.handlers["reconcile"] = ReconcileHandler(self.database, self.publisher, remote, downloader,
            bookmarks=self.bookmarks, profiles=self.profiles, source_status=self.source_status)
        self.queue.handlers["compare"] = ReconcileHandler(self.database, self.publisher, remote, downloader,
            bookmarks=self.bookmarks, profiles=self.profiles, source_status=self.source_status, compare_content=True)
        self.queue.handlers["artist_source"] = ArtistSourceHandler(self.source_status)
        self.maintenance = MissingMediaMaintenance(self.database, self.queue,
                                                    remote_available=lambda: self.client is not None)
        self.queue.maintenance = self.maintenance

    @property
    def client(self):
        with self._client_lock:
            return self._client

    @client.setter
    def client(self, value):
        changed = False
        with self._client_lock:
            if value is not self._client:
                self._client_generation += 1
                self._client = value
                self.account_id = None
                if hasattr(self, "library"):
                    self.library.account_id = None
                changed = True
        # Bookmark snapshots can hold their memory lock while reading client;
        # never acquire that lock while holding the client publication lock.
        if changed and hasattr(self, "bookmarks"):
            self.bookmarks.mark_disconnected()

    def client_snapshot(self):
        with self._client_lock:
            return self._client, self._client_generation

    def publish_account(self, expected, account_id):
        """Validate and publish one bound identity before replacement can win."""
        from ..request_policy import AccountChanged
        account = str(account_id) if account_id is not None else None
        client, generation = self.client_snapshot()
        scope_factory = getattr(type(client), "identity_scope", None)
        scope = (client.identity_scope() if callable(scope_factory) else
                 nullcontext(self.remote._generation(client, generation)["identity"]))
        # Token wait/read is outside the client lock. Production clients hold
        # token -> client until publication; the inner section compares only
        # already-loaded values and never acquires SQL/bookmark/token locks.
        with scope as identity:
            current = {"runtime": self._runtime_epoch, "client": generation,
                "identity": list(identity) if identity is not None else None}
            with self._client_lock:
                if self._client is not client or self._client_generation != generation or current != expected:
                    raise AccountChanged()
                changed = self.account_id != account or self._published_account_context != current
                self.account_id = self.library.account_id = account
                self._published_account_context = current
        # A login can change before web replaces the client. Forget confidence
        # even in that interval; a later publication may safely lose freshness,
        # but cleanup must never alter its account or durable bookmark facts.
        if changed:
            self.bookmarks.mark_disconnected()
        return account

    def disconnect_client(self, expected_client):
        """A failed connection may clear only the client it installed."""
        with self._client_lock:
            if self._client is not expected_client:
                return False
            self._client = None
            self._client_generation += 1
            self.account_id = self.library.account_id = None
        self.bookmarks.mark_disconnected()
        return True

    def start(self):
        with self._close_lock:
            if self._closing.is_set():
                raise RuntimeError("已开始关闭的图库不能重新启动")
            if self._started:
                return
            with _startup_phase("wal_start"):
                self.wal_maintenance.start()
                self.database.enable_wal_maintenance(self.wal_maintenance)
            with _startup_phase("publication_recovery"):
                self.publisher.recover()
            if self._closing.is_set():
                raise RuntimeError("图库正在关闭，未启动业务工作者")
            with _startup_phase("bookmark_start"):
                self.bookmarks.start()
            with _startup_phase("task_recovery"):
                self.queue.recover()
            if self._closing.is_set():
                raise RuntimeError("图库正在关闭，未启动任务工作者")
            with _startup_phase("workers_start"):
                self.queue.start()
            # Register one process-owned preparation worker. It does not wait
            # for a browser or delay service readiness until the scan completes.
            with _startup_phase("reading_prepare_start"):
                self.reading.start()
            self._started = True

    def request_stop(self, *, cancel_remote=False):
        self._closing.set()
        self.queue.request_stop()
        self.bookmarks.request_stop()
        self.reading.request_stop()
        self.database.reads.request_stop()
        # A replacement runtime can reuse the client's connection. Only actual
        # application exit cancels that client's request/retry boundaries.
        if cancel_remote and self.client is not None:
            stop = getattr(self.client, "request_stop", None)
            if callable(stop):
                stop()

    def close(self, timeout=5):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        self.request_stop()
        acquired = (self._close_lock.acquire() if deadline is None else
            self._close_lock.acquire(timeout=max(0, deadline - time.monotonic())))
        if not acquired:
            return False
        try:
            if self._closed:
                return True
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if not self.queue.stop(timeout=remaining):
                # Retain ownership and DB while a worker is still writing.
                return False
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if not self.bookmarks.stop(timeout=remaining):
                return False
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if not self.reading.stop(timeout=remaining):
                return False
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if not self.wal_maintenance.stop(timeout=remaining):
                return False
            self.database.close()
            self.lease.close()
            self._closed = True
            return True
        finally:
            self._close_lock.release()

    def busy(self):
        if self.bookmarks.busy() or self.queue._planning or self.queue._resources:
            return True
        with self.database.read() as session:
            return bool(session.scalar(select(func.count()).select_from(Operation).where(
                Operation.state.in_(("queued", "planning", "ready", "running", "interrupted")))))

    def add_artist(self, remote_id):
        remote_id = str(remote_id)
        if not remote_id.isdigit():
            raise ValueError("请输入数字形式的 Pixiv 画师 ID")
        data = self.remote.get_artist_detail(remote_id)
        with self.database.write() as session:
            artist = session.scalar(select(Artist).where(Artist.pixiv_user_id == remote_id))
            if artist is None:
                artist = Artist(pixiv_user_id=remote_id, name=data["name"], account=data.get("account"), avatar_url=data.get("avatar_url"))
                session.add(artist)
            artist.policy = TrackingPolicy(mode="active") if artist.policy is None else artist.policy
            artist.policy.mode, artist.policy.updated_at = "active", utcnow()
            session.flush()
            artist_id = artist.id
        # Fetching profile text/avatar is part of the visible background task.
        # Adding an artist still succeeds when a remote avatar is unavailable.
        self.queue.submit("metadata", {"artist_ids": [artist_id]}, f"刷新 {data['name']} 的资料")
        operation = self.queue.submit("check", {"artist_ids": [artist_id]}, f"检查 {data['name']}")
        return {"artist_id": artist_id, **operation}

    def delete(self, work_ids):
        ids = sorted(set(work_ids))
        with self.database.read() as session:
            known = set(session.scalars(select(Work.id).where(Work.id.in_(ids))))
            if known != set(ids):
                raise KeyError("删除清单中有不存在的作品，未开始删除")
        for work_id in ids:
            self.publisher.suppress(work_id)
        return self.queue.submit("delete", {"work_ids": ids}, "删除所选作品")

    def delete_artist(self, artist_id):
        # Whole-artist deletion must first close discovery's scope. Work deletion
        # can fence a known Pixiv ID immediately; an artist may still yield new IDs.
        self.queue.accept_submissions(False)
        try:
            if self.busy():
                raise LibraryBusy(LibraryBusy.user_message)
            with self.database.read() as session:
                ids = list(session.scalars(select(Work.id).where(Work.artist_id == artist_id)))
            self.library.set_tracking(artist_id, "stopped")
            for work_id in ids:
                self.publisher.suppress(work_id)
        finally:
            self.queue.accept_submissions(True)
        return self.queue.submit("delete", {"work_ids": ids}, "删除画师的已有存档")

    def bookmark(self, work_id, desired: bool | None = None):
        """Legacy adapter shares the same confirmed-fact service as the new UI."""
        from uuid import uuid4
        if desired is None:
            current = self.bookmarks.state(work_id)
            if current["value"] is None:
                return self.bookmarks.refresh(work_id)
            desired = not current["value"]
        return self.bookmarks.submit(work_id, desired, uuid4().hex)

    def media_path(self, relative):
        with diagnostics.span('media.authorize') as observation:
            result = self._media_path_observed(relative)
            observation['outcome'] = 'available' if result is not None else 'unavailable'
            return result

    def _media_path_observed(self, relative):
        if relative.startswith((".staging/", ".recovery/", ".archive-writer")):
            return None
        try:
            path = contained_path(self.images, relative)
        except ValueError:
            return None
        with self.database.read() as session:
            allowed = False
            # Generated originals, previews and historical revisions retain the
            # Pixiv work ID in their basename. It is only a lookup hint: the full
            # registered path and deletion intent still decide authorisation.
            match = re.fullmatch(r"([0-9]+)(?:_p[0-9]+(?:_v[0-9]+)?|_card_motion_v[0-9]+|_ugoira)?(?:\.[^.]+)+", PurePosixPath(relative).name)
            if match:
                work_id = session.scalar(select(Work.id).where(Work.pixiv_illust_id == match[1]))
                if work_id is not None:
                    models = (Derivative, Asset) if "/.thumbnails/" in relative else (Asset, Derivative)
                    for model in models:
                        if session.scalar(select(model.id).where(model.work_id == work_id,
                                model.relative_path == relative,
                                ~select(ArchiveIntent.pixiv_illust_id).where(
                                    ArchiveIntent.pixiv_illust_id == match[1], ArchiveIntent.mode == "suppressed").exists()).limit(1)) is not None:
                            allowed = True
                            break
            if not allowed:
                allowed = session.scalar(select(Artist.id).where(Artist.avatar_path == relative).limit(1)) is not None
            for model in (Asset, Derivative):
                if allowed:
                    break
                work_id = session.scalar(select(model.work_id).join(Work).where(model.relative_path == relative,
                    ~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed"))).limit(1))
                if work_id is not None:
                    allowed = True
                    break
        observed_state = "missing"
        if allowed:
            try:
                with diagnostics.span('media.stat') as observation:
                    stat = path.stat()
                    observation['file_bytes'] = stat.st_size
                if S_ISREG(stat.st_mode) and stat.st_size > 0:
                    return path
                if S_ISREG(stat.st_mode):
                    observed_state = "invalid"
            except OSError:
                pass
        if allowed:
            from .catalogue_revision import bump_catalogue_revision
            with self.database.write() as session:
                # Publication may have won the writer while this observation waited.
                try:
                    with diagnostics.span('media.stat', reason='recheck') as observation:
                        current_stat = path.stat()
                        observation['file_bytes'] = current_stat.st_size
                    if S_ISREG(current_stat.st_mode) and current_stat.st_size > 0:
                        return path
                    observed_state = "invalid" if S_ISREG(current_stat.st_mode) else "missing"
                except OSError:
                    observed_state = "missing"
                changed = False
                for model in (Asset, Derivative):
                    for record in session.scalars(select(model).where(model.relative_path == relative)):
                        record_state = "invalid" if model is Derivative else observed_state
                        if record.state != record_state:
                            record.state, record.error = record_state, "请求的本地文件缺失或为空，等待补齐"
                            changed = True
                if changed:
                    bump_catalogue_revision(session)
            self.queue._wake.set()
            self.maintenance._next_scan = 0
        return None

    def recovery_issues(self):
        with self.database.read() as session:
            return [{"id": record.id, "action": record.action, "error": record.error} for record in
                session.scalars(select(PublicationJournal).where(PublicationJournal.step == "prepared",
                    PublicationJournal.error.is_not(None)).order_by(PublicationJournal.created_at).limit(100))]

    def has_pending_publications(self):
        with self.database.read() as session:
            return bool(session.scalar(select(func.count()).select_from(PublicationJournal).where(
                PublicationJournal.step == "prepared")))
