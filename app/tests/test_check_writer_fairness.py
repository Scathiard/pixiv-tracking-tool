"""Small synthetic check batches yield to an already waiting user write."""
from contextlib import contextmanager
from threading import Event, RLock, Thread, current_thread

from sqlalchemy import select

from src.core.archive import ArchiveHandler
from src.core.bookmarks import BookmarkService
from src.core.database import ArchiveDatabase
from src.core.media import Publisher
from src.core.models import ArchiveIntent, Artist, Operation, OperationItem, RemoteBookmark, Work
from src.core.tasks import TaskQueue


class Remote:
    def get_account_id(self):
        return "synthetic"


def test_background_cannot_retake_writer_ahead_of_waiting_interaction(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    entered, waiting, next_batch, admit_user, second_finished = Event(), Event(), Event(), Event(), Event()
    order, errors = [], []
    underlying = RLock()

    class DelayedUserLock:
        """Pin the scheduler where a plain RLock allows the owner to barge."""
        def acquire(self):
            if current_thread().name == "user-write":
                waiting.set()
                assert admit_user.wait(3)
            return underlying.acquire()

        def release(self):
            underlying.release()

    db._write_lock = DelayedUserLock()
    # The FIFO gate waits before acquiring the SQLite writer lock. Observe its
    # wait so the same orchestration also exercises the former plain-lock gate.
    admission = getattr(db, "_writer_admission", None)
    if admission is not None:
        original_wait = admission.wait

        def observe_wait(*args, **kwargs):
            if current_thread().name == "user-write":
                waiting.set()
            return original_wait(*args, **kwargs)

        admission.wait = observe_wait
    original_gate = db._write_gate

    @contextmanager
    def observed_gate():
        if current_thread().name == "background-check" and entered.is_set():
            next_batch.set()
        with original_gate():
            yield

    db._write_gate = observed_gate

    def background():
        try:
            with db.write():
                order.append("batch-1")
                entered.set()
                assert waiting.wait(3)
            with db.write():
                order.append("batch-2")
            second_finished.set()
        except BaseException as exc:
            errors.append(exc)

    def user():
        try:
            assert entered.wait(3)
            with db.write():
                order.append("user")
        except BaseException as exc:
            errors.append(exc)

    workers = [Thread(target=background, name="background-check"), Thread(target=user, name="user-write")]
    try:
        for worker in workers:
            worker.start()
        assert next_batch.wait(3)
        # The old gate can finish the second transaction while the user has
        # already requested admission; FIFO must keep it queued instead.
        second_finished.wait(.1)
    finally:
        admit_user.set()
        for worker in workers:
            worker.join(4)
        db.close()
    assert not errors and not any(worker.is_alive() for worker in workers)
    assert order == ["batch-1", "user", "batch-2"]


def test_check_observation_batches_are_small_and_resume_without_duplicates(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    bookmarks = BookmarkService(db, Remote(), current_account=lambda: "synthetic")
    handler = ArchiveHandler("check", db, Publisher(db, tmp_path / "images"), Remote(), bookmarks=bookmarks)
    queue = TaskQueue(db, {"check": handler})
    try:
        with db.write() as session:
            session.add(Artist(id=1, pixiv_user_id="10", name="synthetic"))
            session.flush()
            for ident in range(1, 18):
                session.add(Work(id=ident, pixiv_illust_id=str(100 + ident), artist_id=1,
                                 title="synthetic", type="illust", page_count=1))
                session.add(ArchiveIntent(pixiv_illust_id=str(100 + ident), mode="keep", revision=1))
        ident = queue.submit("check", {"artist_ids": [1]}, "synthetic", "manual")["operation_id"]
        assert queue._plan(ident)
        with db.read() as session:
            cursor = session.get(Operation, ident).result["planning_cursor"]
            assert cursor["observation_after"] == 8
            assert not cursor["observations_complete"]
            assert len(list(session.scalars(select(RemoteBookmark)))) == 8
        # Check tasks stop at the existing offline gate. The manual pause
        # button belongs to reconcile, covered by test_planning_fairness.
        queue.allowed_kinds = lambda: ()
        assert not queue._step("check")
        queue = TaskQueue(db, {"check": handler})
        queue.recover()
        assert queue._plan(ident)
        assert queue._plan(ident)
        with db.read() as session:
            cursor = session.get(Operation, ident).result["planning_cursor"]
            assert cursor["observation_after"] == 17 and cursor["observations_complete"]
            items = list(session.scalars(select(OperationItem).where(OperationItem.operation_id == ident)))
            assert len(items) == 17 and all(item.state == "skipped" for item in items)
            facts = list(session.scalars(select(RemoteBookmark)))
            assert len(facts) == 17 and all(fact.version == 1 for fact in facts)
    finally:
        queue.stop()
        bookmarks.stop()
        db.close()


def test_incremental_check_plans_at_most_eight_items_and_retains_artist_cursor(tmp_path):
    db = ArchiveDatabase.create(tmp_path / "synthetic.db")
    handler = ArchiveHandler("check", db, Publisher(db, tmp_path / "images"), Remote())
    queue = TaskQueue(db, {"check": handler})
    try:
        with db.write() as session:
            session.add_all([Artist(id=ident, pixiv_user_id=str(100 + ident), name="synthetic")
                             for ident in range(1, 10)])
        ident = queue.submit("check", {"artist_ids": list(range(1, 10))}, "synthetic", "automatic")["operation_id"]
        counts = []
        for offset in (4, 8, 9):
            assert queue._plan(ident)
            with db.read() as session:
                operation = session.get(Operation, ident)
                assert operation.result["planning_cursor"]["incremental_artist_offset"] == offset
                assert operation.result["planning_complete"] is (offset == 9)
                counts.append(len(list(session.scalars(select(OperationItem).where(OperationItem.operation_id == ident)))))
            if offset == 4:
                queue = TaskQueue(db, {"check": handler})
                queue.recover()
        assert counts == [8, 16, 18]
    finally:
        queue.stop()
        db.close()
