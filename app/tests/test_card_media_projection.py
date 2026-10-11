"""Card slots and actual narrow SQL; synthetic media paths without file/network IO."""
import json

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import InvalidRequestError

from src.core.database import ArchiveDatabase
from src.core.library import Library, LibraryFilter
from src.core.media import CARD_MOTION_SPEC
from src.core.models import Artist, Asset, Derivative, RevisionAsset, RevisionDerivative, Work, WorkRevision
from src.core.revision_views import media_maps, selected_snapshots


@pytest.fixture
def database(tmp_path):
    result = ArchiveDatabase.create(tmp_path / "synthetic.db")
    with result.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        session.flush()
        session.add_all([Work(id=i, pixiv_illust_id=str(i), artist_id=1, title=str(i),
                              type="illust") for i in (1, 2)])
        session.flush()
        session.add_all([WorkRevision(id=name, work_id=2, ordinal=position, title=name,
            type="ugoira", page_count=1, source_signature=name) for position, name in enumerate(("old", "new"), 1)])
        session.flush()
        work = session.get(Work, 2)
        work.default_revision_id, work.latest_revision_id = "old", "new"
        session.add_all([Asset(id="first", work_id=1, role="image", state="verified", relative_path="first.png"),
            Asset(id="extra", work_id=1, role="image", page_index=1, state="verified", relative_path="extra.png"),
            Asset(id="bound", work_id=2, revision_id="new", role="image", page_index=7,
                  state="verified", relative_path="bound.png"),
            Asset(id="zip", work_id=2, revision_id="old", role="ugoira_zip", state="verified", relative_path="old.zip")])
        session.add_all([Derivative(id="failed-thumb", work_id=1, kind="thumbnail", spec_key="v1",
            state="failed", relative_path="failed.webp"),
            Derivative(id="old-gif", work_id=2, revision_id="new", kind="legacy_alternate", page_index=7,
                state="verified", relative_path="old.gif", sha256="old-sha"),
            Derivative(id="old-thumb", work_id=2, revision_id="new", kind="thumbnail", page_index=7,
                state="verified", relative_path="old.webp", sha256="old-thumb-sha"),
            Derivative(id="old-motion", work_id=2, revision_id="new", kind="thumbnail", page_index=8,
                state="verified", relative_path="old-motion.webp", sha256="motion-sha", source_version="old-sha"),
            Derivative(id="new-gif", work_id=2, revision_id="new", kind="gif",
                state="verified", relative_path="new.gif", sha256="new-sha"),
            Derivative(id="new-motion", work_id=2, revision_id="new", kind="thumbnail", spec_key=CARD_MOTION_SPEC,
                state="verified", relative_path="new-motion.webp", sha256="new-motion-sha", source_version="new-sha")])
        session.flush()
        session.add_all([RevisionAsset(revision_id="old", role="image", page_index=0, asset_id="bound"),
            RevisionAsset(revision_id="old", role="ugoira_zip", page_index=0, asset_id="zip"),
            RevisionAsset(revision_id="new", role="image", page_index=0, asset_id="first")])
        for revision, slots in {"old": [("gif", "v1", "old-gif"), ("thumbnail", "v1", "old-thumb"),
                ("thumbnail", CARD_MOTION_SPEC, "old-motion")],
                "new": [("gif", "v1", "new-gif"), ("thumbnail", "v1", "failed-thumb"),
                ("thumbnail", CARD_MOTION_SPEC, "new-motion")]}.items():
            for kind, spec, record in slots:
                session.add(RevisionDerivative(revision_id=revision, kind=kind, page_index=0,
                                               spec_key=spec, derivative_id=record))
    try:
        yield result
    finally:
        result.close()


def test_default_explicit_slots_ownership_and_narrow_ordered_sql(database, tmp_path):
    sql = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT"):
            sql.append((statement, parameters))
    evidence = []
    for explicit, expected in (({}, "old-gif"), ({2: "new"}, "new-gif")):
        with database.read() as session:
            works = list(session.scalars(select(Work).order_by(Work.id)))
            snapshots = selected_snapshots(session, works, explicit, compact=True)
            event.listen(database.engine, "before_cursor_execute", capture)
            try:
                assets, derived = media_maps(session, works, snapshots, compact=True)
            finally:
                event.remove(database.engine, "before_cursor_execute", capture)
            assert derived[(2, "gif", 0, "v1")].id == expected
            assert (1, "image", 1) not in assets and (2, "ugoira_zip", 0) not in assets
            if explicit:
                assert (2, "image", 0) not in assets  # foreign work's physical asset
                assert (2, "thumbnail", 0, "v1") not in derived
            else:
                assert assets[(2, "image", 0)].id == "bound"  # physical page 7 -> logical page 0
            assert "source_url" in inspect(assets[(1, "image", 0)]).unloaded
            with pytest.raises(InvalidRequestError):
                _ = assets[(1, "image", 0)].source_url
            with pytest.raises(InvalidRequestError):
                _ = derived[(2, "gif", 0, "v1")].error
        assert len(sql) == 4
        for statement, parameters in sql:
            assert "assets.source_url" not in statement and "assets.error" not in statement
            assert "derivatives.error" not in statement and "derivatives.byte_size" not in statement
            if "ORDER BY" in statement:
                assert "ORDER BY assets.rowid" in statement or "ORDER BY derivatives.rowid" in statement
        with database.engine.connect() as connection:
            evidence.append({"explicit": bool(explicit), "queries": [{"sql": statement,
                "plan": [row[3] for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)]}
                for statement, parameters in sql]})
        sql.clear()
    (tmp_path / "card-media-checks.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")


def test_product_card_keeps_first_image_fallback_and_motion_sha_binding(database):
    library = Library(database)
    cards = {item["id"]: item for item in library.list_cursor(LibraryFilter(), limit=2, compact=True)["items"]}
    assert cards[1]["thumbnail"] is None and cards[1]["preview"] == "/images/first.png"
    assert cards[2]["revision_id"] == "old" and cards[2]["latest_revision_id"] == "new"
    assert cards[2]["preview"] == "/images/old-motion.webp?v=motion-sha"
    with database.write() as session:
        session.get(Derivative, "old-motion").source_version = "new-sha"
    cards = {item["id"]: item for item in library.list_cursor(LibraryFilter(), limit=2, compact=True)["items"]}
    assert cards[2]["preview"] is None and cards[2]["preview_status"] == "pending"


def test_all_animation_union_chunks_remain_bounded(database):
    with database.write() as session:
        session.add_all([Work(id=i, pixiv_illust_id=str(i), artist_id=1, title=str(i),
                              type="ugoira") for i in range(3, 244)])
    statements = []
    def capture(connection, cursor, sql, parameters, context, executemany):
        if sql.startswith("SELECT"):
            statements.append(sql)
    with database.read() as session:
        works = list(session.scalars(select(Work).where(Work.id >= 3)))
        event.listen(database.engine, "before_cursor_execute", capture)
        try:
            assert media_maps(session, works, {}, compact=True) == ({}, {})
        finally:
            event.remove(database.engine, "before_cursor_execute", capture)
    assert len(statements) == 3  # 241 image branches, then 480 + 2 derivative branches
    assert all(sql.count("UNION ALL") + 1 <= 480 for sql in statements)
