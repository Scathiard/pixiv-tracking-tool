"""One owned local library and one task worker. Authentication is optional."""
from __future__ import annotations

from pathlib import Path
from threading import RLock

from sqlalchemy import func, select

from .archive import ArchiveHandler
from .database import ArchiveDatabase
from .library import Library
from .media import Publisher, contained_path
from .models import ArchiveIntent, Artist, Asset, Derivative, Operation, PublicationJournal, RemoteBookmark, TrackingPolicy, Work, utcnow
from .ownership import LibraryLease
from .tasks import TaskQueue, safe_failure


class LoginRequired(RuntimeError):
    user_message = "此操作需要连接 Pixiv；本地图库仍可继续使用"


class LibraryBusy(RuntimeError):
    user_message = "还有未完成任务。请等本轮结束后，再删除整位画师的存档"


class RemoteAccess:
    def __init__(self, runtime):
        self.runtime = runtime

    def __getattr__(self, name):
        client = self.runtime.client
        if client is None:
            raise LoginRequired(LoginRequired.user_message)
        return getattr(client, name)


class Runtime:
    def __init__(self, database_path: Path, images: Path, *, create=False, client=None, downloader=None):
        self.lease = LibraryLease(images).acquire()
        try:
            self.database = ArchiveDatabase.create(database_path) if create else ArchiveDatabase(database_path)
        except BaseException:
            self.lease.close()
            raise
        self.images, self.client = Path(images).resolve(), client
        self.library = Library(self.database)
        self.publisher = Publisher(self.database, self.images)
        remote = RemoteAccess(self)
        self.remote = remote
        self.queue = TaskQueue(self.database, {
            kind: ArchiveHandler(kind, self.database, self.publisher, remote, downloader)
            for kind in ("check", "metadata", "bookmarks", "repair", "verify", "backfill", "thumbnails", "delete")})
        self._bookmark_lock = RLock()

    def start(self):
        self.publisher.recover()
        self.queue.recover()
        self.queue.start()

    def close(self):
        if not self.queue.stop():
            # Do not release ownership or dispose a DB still used by network work.
            return False
        self.database.close()
        self.lease.close()
        return True

    def busy(self):
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
        with self._bookmark_lock:
            with self.database.read() as session:
                work = session.get(Work, work_id)
                if work is None:
                    raise KeyError(work_id)
                remote_id = work.pixiv_illust_id
                current = bool(work.bookmark and work.bookmark.value)
            desired = not current if desired is None else desired
            try:
                if desired:
                    self.remote.add_bookmark(remote_id, restrict="public")
                else:
                    self.remote.delete_bookmark(remote_id)
            except Exception as exc:
                with self.database.write() as session:
                    record = session.get(RemoteBookmark, work_id)
                    if record is None:
                        record = RemoteBookmark(work_id=work_id, value=current)
                        session.add(record)
                    record.error = safe_failure(exc)["message"]
                raise
            with self.database.write() as session:
                record = session.get(RemoteBookmark, work_id)
                if record is None:
                    record = RemoteBookmark(work_id=work_id)
                    session.add(record)
                record.value, record.confirmed_at, record.error = desired, utcnow(), None
            return {"ok": True, "is_bookmarked": desired}

    def media_path(self, relative):
        if relative.startswith((".staging/", ".recovery/", ".archive-writer")):
            return None
        try:
            path = contained_path(self.images, relative)
        except ValueError:
            return None
        with self.database.read() as session:
            allowed = False
            for model in (Asset, Derivative):
                work_id = session.scalar(select(model.work_id).join(Work).where(model.relative_path == relative,
                    ~Work.pixiv_illust_id.in_(select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed"))).limit(1))
                if work_id is not None:
                    allowed = True
                    break
        return path if allowed and path.is_file() else None

    def recovery_issues(self):
        with self.database.read() as session:
            return [{"id": record.id, "action": record.action, "error": record.error} for record in
                session.scalars(select(PublicationJournal).where(PublicationJournal.step == "prepared",
                    PublicationJournal.error.is_not(None)).order_by(PublicationJournal.created_at).limit(100))]

    def has_pending_publications(self):
        with self.database.read() as session:
            return bool(session.scalar(select(func.count()).select_from(PublicationJournal).where(
                PublicationJournal.step == "prepared")))
