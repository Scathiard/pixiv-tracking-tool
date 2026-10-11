"""Three small synthetic checks for default cards, writes and read boundaries."""
import json

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from src.core import gallery_card_projection as cards
from src.core.library import readable_page_url, remote_availability
from src.core.models import Artist, Asset, Base, Derivative, RevisionAsset, RevisionDerivative, Work, WorkRevision
from src.core.read_projection import ProjectionUnavailable
from src.core.revision_views import apply_snapshot, card_media_maps, selected_snapshots


@pytest.fixture
def source():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE read_display_metadata(scope TEXT PRIMARY KEY,availability TEXT NOT NULL,translations TEXT NOT NULL)")
        connection.exec_driver_sql("CREATE TABLE read_model_dirty(entity TEXT NOT NULL,identity TEXT NOT NULL,PRIMARY KEY(entity,identity)) WITHOUT ROWID")
        cards.install(connection)
    with Session(engine) as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="Synthetic"))
        session.add_all([Work(id=identity, pixiv_illust_id=str(identity), artist_id=1,
            title=f"current {identity}", type="illust", page_count=1) for identity in (1, 2, 3)])
        session.flush()
        for identity in (1, 2, 3):
            session.connection().exec_driver_sql("INSERT INTO read_display_metadata VALUES(?,?,?)",
                (f"work:{identity}", "null", "null"))
        session.commit()
        yield session
    engine.dispose()


def test_default_old_revision_and_legacy_match_existing_compact_rules(source):
    session = source
    revision = WorkRevision(id="old", work_id=2, ordinal=1, title="pinned title", type="manga",
        page_count=4, source_signature="synthetic")
    session.add(revision)
    session.add(WorkRevision(id="new", work_id=2, ordinal=2, title="new title", type="illust",
        page_count=1, source_signature="synthetic"))
    session.get(Work, 2).default_revision_id = "old"
    session.get(Work, 2).latest_revision_id = "new"
    # A cross-work pointer does not become that other work's selected version.
    session.get(Work, 3).default_revision_id = "old"
    session.add_all([Asset(id="legacy", work_id=1, role="image", page_index=0,
        state="unknown", relative_path="legacy p0.png"),
        Asset(id="bound", work_id=2, revision_id="old", role="image", page_index=7,
              state="verified", relative_path="bound.png"),
        Asset(id="fallback", work_id=3, role="image", page_index=0,
              state="failed", relative_path="fallback.png")])
    session.flush()
    session.add(RevisionAsset(revision_id="old", role="image", page_index=0, asset_id="bound"))
    availability = {"state": "private", "confirmed": True, "policy": "source-v2", "account_id": "one"}
    session.connection().exec_driver_sql("UPDATE read_display_metadata SET availability=? WHERE scope='work:2'",
        (json.dumps(availability),))
    session.flush()
    assert cards.backfill(session, batch_size=2) == {"cards": 3}
    result = cards.read_cards(session, [3, 2, 1])
    works = list(session.scalars(select(Work).order_by(Work.id)))
    snapshots = selected_snapshots(session, works, compact=True)
    assets, _ = card_media_maps(session, works, snapshots)
    for work in works:
        work._read_metadata = {"remote_availability": availability if work.id == 2 else None}
        original = apply_snapshot({"title": work.title, "type": work.type, "page_count": work.page_count},
            work, snapshots.get(work.id), compact=True)
        actual = cards.card_view(result[work.id], "one")
        for key in ("title", "type", "page_count", "revision_id", "latest_revision_id"):
            assert actual[key] == original[key]
        assert actual["preview"] == readable_page_url(assets.get((work.id, "image", 0)))
        assert actual["availability"] == remote_availability(work, "one")
    assert cards.card_view(result[2], "different")["availability"]["effective"] is False
    assert cards.card_view(result[2])["availability"]["cached"] is True
    assert result[3].revision_id is None
    assert cards.verify(session) == {"cards": 3}
    session.get(Artist, 1).name = "Renamed"
    revision.title = "changed pinned title"
    session.flush()
    dirty = set(session.connection().exec_driver_sql("SELECT entity,identity FROM read_model_dirty"))
    assert ("card_artist", "1") in dirty
    assert ("card_work", "2") in dirty
    cards.reconcile_cards(session, [1, 2, 3])
    assert cards.read_cards(session, [2])[2].title == "changed pinned title"


def test_card_motion_hash_ownership_and_transactional_targeted_update(source):
    session = source
    session.get(Work, 1).type = "ugoira"
    session.add_all([Derivative(id="gif", work_id=1, kind="gif", state="verified",
        sha256="a" * 64, relative_path="original.gif"),
        Derivative(id="motion", work_id=1, kind="thumbnail", spec_key=cards.CARD_MOTION_SPEC,
        state="verified", sha256="b" * 64, source_version="a" * 64, relative_path="motion.webp"),
        Derivative(id="thumb", work_id=1, kind="thumbnail", state="verified",
        sha256="c" * 64, relative_path="thumb.webp")])
    session.flush()
    cards.backfill(session)
    session.commit()
    view = cards.card_view(cards.read_cards(session, [1])[1])
    assert view["preview"] == "/images/motion.webp?v=" + "b" * 16
    assert view["thumbnail"] == "/images/thumb.webp?v=" + "c" * 16
    payload_bytes = session.connection().exec_driver_sql("SELECT length(first_page)+length(thumbnail)+length(motion)+length(gif) FROM read_gallery_cards WHERE work_id=1").scalar_one()
    assert payload_bytes < 1100
    print(f"populated motion card resource JSON: {payload_bytes} bytes")
    untouched = session.connection().exec_driver_sql("SELECT * FROM read_gallery_cards WHERE work_id=2").first()
    session.get(Derivative, "motion").source_version = "different"
    session.flush()
    assert session.connection().exec_driver_sql("SELECT 1 FROM read_model_dirty WHERE entity='card_work' AND identity='1'").first()
    cards.reconcile_cards(session, [1])
    assert cards.card_view(cards.read_cards(session, [1])[1])["preview_status"] == "pending"
    assert tuple(session.connection().exec_driver_sql("SELECT * FROM read_gallery_cards WHERE work_id=2").first()) == tuple(untouched)
    session.rollback()
    assert cards.card_view(cards.read_cards(session, [1])[1])["preview_status"] == "ready"
    # Foreign physical files in a valid logical binding must never leak.
    session.add(WorkRevision(id="foreign-binding", work_id=2, ordinal=1, title="second",
        type="ugoira", page_count=1, source_signature="synthetic"))
    session.get(Work, 2).default_revision_id = "foreign-binding"
    session.flush()
    session.add(RevisionDerivative(revision_id="foreign-binding", kind="thumbnail", page_index=0,
        spec_key=cards.CARD_MOTION_SPEC, derivative_id="motion"))
    session.flush()
    cards.reconcile_cards(session, [2])
    assert cards.read_cards(session, [2])[2].motion is None


def test_read_does_not_touch_sources_or_repair_missing_rows_and_deletion_is_local(source):
    session = source
    cards.backfill(session)
    session.commit()
    statements = []
    def record(connection, cursor, sql, params, context, many):
        statements.append(sql)
    event.listen(session.bind, "before_cursor_execute", record)
    try:
        assert set(cards.read_cards(session, [1, 2, 3])) == {1, 2, 3}
    finally:
        event.remove(session.bind, "before_cursor_execute", record)
    assert len(statements) == 1
    assert statements[0].startswith("SELECT * FROM read_gallery_cards")
    row_size = session.connection().exec_driver_sql("SELECT length(first_page)+length(thumbnail)+length(motion)+length(gif) FROM read_gallery_cards WHERE work_id=1").scalar_one()
    assert row_size == 16  # Four JSON nulls, independent of source manifests.
    plan = session.connection().exec_driver_sql("""EXPLAIN QUERY PLAN
        SELECT c.work_id,a.id FROM read_gallery_cards c JOIN assets a
        ON a.work_id=c.work_id AND a.revision_id IS NULL AND a.role='image' AND a.page_index=0
        WHERE c.work_id IN (1,2,3) AND c.revision_id IS NULL""").all()
    details = [row[3] for row in plan]
    print("card original write lookup: " + " | ".join(details))
    assert any("SEARCH c USING INTEGER PRIMARY KEY" in detail for detail in details)
    assert any("SEARCH a USING" in detail and "work_id=?" in detail and "role=?" in detail
               and "page_index=?" in detail for detail in details)
    assert not any("SCAN a" in detail for detail in details)
    session.connection().exec_driver_sql("DELETE FROM read_gallery_cards WHERE work_id=1")
    with pytest.raises(ProjectionUnavailable):
        cards.read_cards(session, [1])
    with pytest.raises(ProjectionUnavailable):
        cards.verify(session)
    cards.reconcile_cards(session, [1])
    session.connection().exec_driver_sql("DELETE FROM works WHERE id=3")
    cards.reconcile_cards(session, [3])
    assert cards.verify(session) == {"cards": 2}
