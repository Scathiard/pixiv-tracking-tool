"""One owned local library and one task worker. Authentication is optional."""
from __future__ import annotations

from pathlib import Path
from stat import S_ISREG
import threading
import time

from sqlalchemy import func, select

from .archive import ArchiveHandler
from .bookmarks import BookmarkService
from .database import ArchiveDatabase
from .library import Library
from .media import Publisher, contained_path
from .maintenance import MissingMediaMaintenance
from .models import ArchiveIntent, Artist, Asset, Derivative, Operation, PublicationJournal, TrackingPolicy, Work, utcnow
from .ownership import LibraryLease
from .profiles import ArtistProfiles
from .tasks import TaskQueue


class LoginRequired(RuntimeError):
    user_message = "此操作需要连接 Pixiv；本地图库仍可继续使用"


class LibraryBusy(RuntimeError):
    user_message = "还有未完成任务。请等本轮结束后，再删除整位画师的存档"


class RemoteAccess:
    def __init__(self, runtime):
        self.runtime = runtime

    def __getattr__(self, name):
        if self.runtime._closing.is_set():
            from ..request_policy import ShutdownRequested
            raise ShutdownRequested()
        client = self.runtime.client
        if client is None:
            raise LoginRequired(LoginRequired.user_message)
        return getattr(client, name)

    def get_account_id(self):
        if self.runtime._closing.is_set():
            from ..request_policy import ShutdownRequested
            raise ShutdownRequested()
        client = self.runtime.client
        if client is None:
            self.runtime.account_id = self.runtime.library.account_id = None
            self.runtime.bookmarks.mark_disconnected()
            raise LoginRequired(LoginRequired.user_message)
        account_id = client.get_account_id()
        # Local catalogue responses remain explicitly cached. An OAuth identity
        # alone does not prove that the current network can reach Pixiv.
        self.runtime.account_id = account_id
        return account_id


class Runtime:
    def __init__(self, database_path: Path, images: Path, *, create=False, client=None, downloader=None):
        self._closing = threading.Event()
        self._closed = False
        self._close_lock = threading.Lock()
        self.lease = LibraryLease(images).acquire()
        try:
            self.database = ArchiveDatabase.create(database_path) if create else ArchiveDatabase(database_path)
        except BaseException:
            self.lease.close()
            raise
        self.images, self.client = Path(images).resolve(), client
        self.account_id = None
        self.library = Library(self.database)
        self.publisher = Publisher(self.database, self.images)
        remote = RemoteAccess(self)
        self.remote = remote
        self.bookmarks = BookmarkService(self.database, remote,
                                         is_available=lambda: self.client is not None,
                                         current_account=lambda: self.account_id)
        self.library.bookmarks = self.bookmarks
        self.profiles = ArtistProfiles(self.database, self.publisher, remote)
        self.queue = TaskQueue(self.database, {
            kind: ArchiveHandler(kind, self.database, self.publisher, remote, downloader,
                                 bookmarks=self.bookmarks, profiles=self.profiles)
            for kind in ("check", "metadata", "bookmarks", "repair", "verify", "backfill", "thumbnails", "delete")})
        self.maintenance = MissingMediaMaintenance(self.database, self.queue,
                                                    remote_available=lambda: self.client is not None)
        self.queue.maintenance = self.maintenance

    def start(self):
        self.publisher.recover()
        self.bookmarks.start()
        self.queue.recover()
        self.queue.start()

    def request_stop(self, *, cancel_remote=False):
        self._closing.set()
        self.queue.request_stop()
        self.bookmarks.request_stop()
        # A replacement runtime can reuse the client's connection. Only actual
        # application exit cancels that client's request/retry boundaries.
        if cancel_remote and self.client is not None:
            stop = getattr(self.client, "request_stop", None)
            if callable(stop):
                stop()

    def close(self, timeout=5):
        self.request_stop()
        with self._close_lock:
            if self._closed:
                return True
            deadline = None if timeout is None else time.monotonic() + timeout
            if not self.queue.stop(timeout=timeout):
                # Retain ownership and DB while a worker is still writing.
                return False
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if not self.bookmarks.stop(timeout=remaining):
                return False
            self.database.close()
            self.lease.close()
            self._closed = True
            return True

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
        if relative.startswith((".staging/", ".recovery/", ".archive-writer")):
            return None
        try:
            path = contained_path(self.images, relative)
        except ValueError:
            return None
        with self.database.read() as session:
            allowed = session.scalar(select(Artist.id).where(Artist.avatar_path == relative).limit(1)) is not None
            for model in (Asset, Derivative):
                work_id = session.scalar(select(model.work_id).join(Work).where(model.relative_path == relative,
                    ~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed"))).limit(1))
                if work_id is not None:
                    allowed = True
                    break
        observed_state = "missing"
        if allowed:
            try:
                stat = path.stat()
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
                    current_stat = path.stat()
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
