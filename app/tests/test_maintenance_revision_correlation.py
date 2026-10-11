"""A foreign current binding must not complete this work's asset inventory."""
from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from src.core.maintenance import MissingMediaMaintenance
from src.core.models import Base, Artist, Work, WorkRevision, ArchiveIntent, Asset, RevisionAsset


def test_incomplete_inventory_correlates_the_current_work():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add(Artist(id=1, pixiv_user_id="synthetic", name="synthetic"))
            for work_id in (1, 2):
                session.add(Work(id=work_id, pixiv_illust_id=str(work_id), artist_id=1,
                    title="synthetic", latest_revision_id=f"r{work_id}"))
                session.add(WorkRevision(id=f"r{work_id}", work_id=work_id, ordinal=1,
                    title="synthetic", type="illust", page_count=1, source_signature="synthetic"))
                session.add(ArchiveIntent(pixiv_illust_id=str(work_id)))
                session.add(Asset(id=f"a{work_id}", work_id=work_id, role="image",
                    page_index=0, state="verified", relative_path="synthetic.png"))
            # An unrelated current revision references the physical file. That
            # cannot supply work 1's missing r1 page, irrespective of provenance.
            session.add(RevisionAsset(revision_id="r2", role="image", page_index=0, asset_id="a1"))
            session.commit()

        @contextmanager
        def read():
            with Session(engine) as session:
                yield session

        statements = []
        def capture(connection, cursor, statement, parameters, context, executemany):
            if "count(*)" in statement:
                statements.append(statement)
        event.listen(engine, "before_cursor_execute", capture)
        maintenance = MissingMediaMaintenance(SimpleNamespace(read=read),
            SimpleNamespace(handlers={}), remote_available=lambda: True)
        remote, _, _, _, _, _ = maintenance._candidates()
        assert set(remote) == {1, 2}
        assert statements and "FROM revision_assets, works" not in statements[0]
    finally:
        engine.dispose()
