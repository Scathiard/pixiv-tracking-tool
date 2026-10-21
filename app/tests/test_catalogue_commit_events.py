"""Committed catalogue notifications, using small isolated synthetic databases."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from src.core.database import ArchiveDatabase
from src.core.models import (
    ArchiveIntent, Artist, Asset, Derivative, LocalPreference, Operation,
    RemoteBookmark, Work, WorkRevision, WorkTag,
)
from src.core.read_projection import require_ready


@pytest.fixture
def database(tmp_path):
    database = ArchiveDatabase.create(tmp_path / "synthetic.db")
    with database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="Synthetic artist"))
        session.flush()
        session.add_all(Work(id=i, pixiv_illust_id=str(i), artist_id=1,
            title=f"Work {i}") for i in (1, 2, 3))
    try:
        yield database
    finally:
        database.close()


def generation(database):
    with database.read() as session:
        return require_ready(session.connection())


def test_success_is_published_after_commit_and_unsubscribe_is_idempotent(database):
    observed = []
    baseline = generation(database)
    event.listen(database.writer_session, "after_commit", lambda session: observed.append("commit"))
    unsubscribe = database.subscribe_catalogue_changes(
        lambda number, ids: observed.append((number, ids)))
    with database.write() as session:
        session.get(Work, 1).title = "changed"
        session.flush()
        assert observed == []
    assert observed == ["commit", (baseline + 1, frozenset({1}))]
    unsubscribe()
    unsubscribe()
    with database.write() as session:
        session.get(Work, 2).title = "changed again"
    assert observed == ["commit", (baseline + 1, frozenset({1})), "commit"]


def test_body_flush_projection_and_commit_failure_do_not_publish(database, monkeypatch):
    observed = []
    database.subscribe_catalogue_changes(lambda number, ids: observed.append((number, ids)))
    baseline = generation(database)
    with pytest.raises(RuntimeError, match="body failed"):
        with database.write() as session:
            session.get(Work, 1).title = "rollback"
            session.flush()
            raise RuntimeError("body failed")
    with pytest.raises(IntegrityError):
        with database.write() as session:
            session.add(Work(id=1, pixiv_illust_id="duplicate", artist_id=1, title="duplicate"))
    with monkeypatch.context() as patch:
        def fail_projection(*args, **kwargs):
            raise RuntimeError("projection failed")
        from src.core import gallery_card_projection
        patch.setattr(gallery_card_projection, "reconcile_cards", fail_projection)
        with pytest.raises(RuntimeError, match="projection failed"):
            with database.write() as session:
                session.get(Work, 1).title = "projection rollback"
    with monkeypatch.context() as patch:
        def fail_commit(session):
            raise RuntimeError("commit failed")
        patch.setattr(database.writer_session.class_, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit failed"):
            with database.write() as session:
                session.get(Work, 1).title = "commit rollback"
    assert observed == []
    assert generation(database) == baseline
    with database.read() as session:
        assert session.get(Work, 1).title == "Work 1"
    checkpoints = []

    def checkpoint():
        checkpoints.append(True)
        assert len(checkpoints) <= 2, "failed commit retained publication lock"

    database.catalogue_commit_barrier(checkpoint)
    assert len(checkpoints) == 2


def test_catalogue_shapes_preferences_tags_and_deletion(database):
    observed = []
    database.subscribe_catalogue_changes(lambda number, ids: observed.append((number, ids)))
    baseline = generation(database)

    def change(mutate, expected):
        before = len(observed)
        with database.write() as session:
            mutate(session)
        assert len(observed) == before + 1
        assert observed[-1] == (baseline + len(observed), frozenset(expected))

    change(lambda s: s.add(Work(id=4, pixiv_illust_id="4", artist_id=1, title="new")), {4})
    change(lambda s: s.add(LocalPreference(work_id=1, is_hidden=True)), {1})
    change(lambda s: setattr(s.get(LocalPreference, 1), "is_hidden", False), {1})
    change(lambda s: s.add(RemoteBookmark(work_id=2, value=True)), {2})
    change(lambda s: setattr(s.get(RemoteBookmark, 2), "value", False), {2})
    change(lambda s: s.add(WorkTag(work_id=1, tag="original")), {1})
    change(lambda s: s.connection().exec_driver_sql(
        "UPDATE work_tags SET work_id=2,tag='changed' WHERE work_id=1"), {1, 2})
    change(lambda s: s.delete(s.get(WorkTag, (2, "changed"))), {2})
    change(lambda s: s.add(ArchiveIntent(pixiv_illust_id="3", mode="suppressed")), {3})
    change(lambda s: s.connection().exec_driver_sql(
        "UPDATE works SET page_count=3,title='ordered',posted_at='2026-01-02' WHERE id=1"), {1})

    def version_switch(session):
        session.add(WorkRevision(id="new-version", work_id=1, ordinal=1,
            title="version", type="manga", page_count=5, source_signature="synthetic"))
        session.flush()
        session.get(Work, 1).default_revision_id = "new-version"
        session.get(Work, 1).page_count = 5
    change(version_switch, {1})
    change(lambda s: s.delete(s.get(Work, 4)), {4})


def test_artist_name_publishes_empty_ids_to_advance_complex_filter_generation(database):
    observed = []
    database.subscribe_catalogue_changes(lambda number, ids: observed.append((number, ids)))
    baseline = generation(database)
    with database.write() as session:
        session.get(Artist, 1).name = "renamed"
    assert observed == [(baseline + 1, frozenset())]
    assert generation(database) == baseline + 1


def test_empty_progress_thumbnail_and_media_writes_do_not_publish(database):
    with database.write() as session:
        session.add(Operation(id="task", kind="repair", state="running", label="task",
            dedupe_key="task", parameters={"work_ids": [2, 3]}))
        session.add(Asset(id="asset", work_id=1, role="image", page_index=0))
        session.add(Derivative(id="thumbnail", work_id=1, kind="thumbnail", page_index=0))
        session.add(LocalPreference(work_id=1, rating=1))
        session.add(RemoteBookmark(work_id=1, value=False))
    observed = []
    database.subscribe_catalogue_changes(lambda number, ids: observed.append((number, ids)))
    baseline = generation(database)
    with database.write():
        pass
    with database.write() as session:
        session.get(Operation, "task").phase_done = 4
        session.get(Operation, "task").state = "completed"
        session.get(Artist, 1).biography = "biography only"
        session.get(Work, 1).caption = "caption only"
        session.get(LocalPreference, 1).rating = 2
        session.get(RemoteBookmark, 1).version = 3
        session.get(Asset, "asset").state = "verified"
        session.get(Asset, "asset").relative_path = "synthetic.png"
        session.get(Derivative, "thumbnail").state = "ready"
        session.get(Derivative, "thumbnail").relative_path = "thumbnail.png"
    assert generation(database) == baseline
    assert observed == []
    # A simultaneous task update must not broaden the catalogue event to all
    # works mentioned by the task's history or parameters.
    with database.write() as session:
        session.get(Work, 1).title = "catalogue change"
        session.get(Operation, "task").state = "running"
    assert observed == [(baseline + 1, frozenset({1}))]


def test_parallel_notifications_follow_serialized_commit_order(database):
    observed, committed = [], []
    barrier = Barrier(3)
    baseline = generation(database)
    database.subscribe_catalogue_changes(lambda number, ids: observed.append((number, ids)))

    def update(work_id):
        barrier.wait(timeout=5)
        with database.write() as session:
            session.get(Work, work_id).title = f"parallel {work_id}"
            committed.append(work_id)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(update, work_id) for work_id in (1, 2, 3)]
        for future in futures:
            future.result(timeout=10)
    assert observed == [(baseline + offset, frozenset({work_id}))
        for offset, work_id in enumerate(committed, 1)]


def test_subscriber_failure_cannot_rollback_commit_or_block_other_subscribers(database, caplog):
    observed = []
    baseline = generation(database)

    def broken(number, ids):
        raise RuntimeError("synthetic subscriber failure")

    database.subscribe_catalogue_changes(broken)
    database.subscribe_catalogue_changes(lambda number, ids: observed.append((number, ids)))
    with database.write() as session:
        session.get(Work, 1).title = "persisted"
    assert observed == [(baseline + 1, frozenset({1}))]
    assert "after committed generation" in caplog.text
    with database.read() as session:
        assert session.get(Work, 1).title == "persisted"
    assert generation(database) == baseline + 1
