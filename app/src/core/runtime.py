"""One owned local library and one task worker. Authentication is optional."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from .archive import ArchiveHandler
from .bookmarks import BookmarkService
from .database import ArchiveDatabase
from .library import Library
from .media import Publisher, contained_path
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
        client = self.runtime.client
        if client is None:
            raise LoginRequired(LoginRequired.user_message)
        return getattr(client, name)

    def get_account_id(self):
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

    def start(self):
        self.publisher.recover()
        self.bookmarks.start()
        self.queue.recover()
        self.queue.start()

    def close(self):
        self.bookmarks.accept_submissions(False)
        if not self.queue.stop():
            # Do not release ownership or dispose a DB still used by network work.
            return False
        if not self.bookmarks.stop():
            return False
        self.database.close()
        self.lease.close()
        return True

    def busy(self):
        if self.bookmarks.busy():
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
