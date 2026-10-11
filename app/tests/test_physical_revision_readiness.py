"""Physical file readiness remains distinct from logical revision slots."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from src.core.archive_state import raw_work_states as work_states
from src.core.models import (Base, Artist, Work, WorkRevision, Asset, RevisionAsset,
    Derivative, RevisionDerivative, ApplicationState)

@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.parametrize("field,value,ready", [
    ("kind", "thumbnail", True), ("kind", "gif", False),
    ("page_index", 1, False), ("spec_key", "v2", False),
    ("state", "unknown", False), ("state", "missing", False),
    ("relative_path", "", False), ("relative_path", None, False),
])
def test_thumbnail_readiness_checks_physical_file(pinned, field, value, ready):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        work = Work(id=1, artist_id=1, pixiv_illust_id="10", title="synthetic",
            type="illust", page_count=1, source_metadata={},
            default_revision_id="selected" if pinned else None,
            latest_revision_id="selected" if pinned else None)
        session.add(work)
        if pinned:
            session.add(WorkRevision(id="selected", work_id=1, ordinal=1,
                title="synthetic", type="illust", page_count=1, source_signature="synthetic"))
        asset = Asset(id="original", work_id=1, revision_id="selected" if pinned else None,
            role="image", page_index=0, state="verified", relative_path="synthetic.png")
        session.add(asset)
        if pinned:
            session.add(RevisionAsset(revision_id="selected", role="image", page_index=0,
                asset_id=asset.id))
        record = Derivative(id="preview", work_id=1, revision_id=None,
            kind="thumbnail", page_index=0, spec_key="v1", state="verified",
            relative_path="synthetic-thumb.png")
        setattr(record, field, value)
        session.add(record)
        if pinned:
            # Deliberately unrelated logical slot: readiness uses physical data.
            session.add(RevisionDerivative(revision_id="selected", kind="legacy_alternate",
                page_index=9, spec_key="logical-v7", derivative_id=record.id))
        session.add(ApplicationState(key="maintenance_work:1",
            value={"tracks": {"local": {"status": "attention", "reason": "local gap"}}}))
        session.commit()
        state = work_states(session, [work])[0][1]
        assert state["archive"]["state"] == "complete"
        assert state["processing"]["state"] == ("idle" if ready else "attention")
    engine.dispose()

@pytest.mark.parametrize("same_owner", [False, True])
@pytest.mark.parametrize("same_revision", [False, True])
def test_current_revision_and_physical_owner_are_both_required(same_owner, same_revision):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        works = [Work(id=i, artist_id=1, pixiv_illust_id=str(i), title="synthetic",
            type="illust", page_count=1, source_metadata={},
            default_revision_id=f"r{i}", latest_revision_id=f"r{i}") for i in (1, 2)]
        session.add_all(works)
        for work in works:
            session.add(WorkRevision(id=f"r{work.id}", work_id=work.id, ordinal=1,
                title="synthetic", type="illust", page_count=1, source_signature="synthetic"))
            session.add(Asset(id=f"a{work.id}", work_id=work.id, revision_id=f"r{work.id}",
                role="image", page_index=0, state="verified", relative_path="synthetic.png"))
            session.add(RevisionAsset(revision_id=f"r{work.id}", role="image",
                page_index=0, asset_id=f"a{work.id}"))
        session.add(Derivative(id="preview", work_id=1 if same_owner else 2,
            revision_id=None, kind="thumbnail", page_index=0, spec_key="v1",
            state="verified", relative_path="synthetic-thumb.png"))
        session.add(RevisionDerivative(revision_id="r1" if same_revision else "r2",
            kind="thumbnail", page_index=0, spec_key="v1", derivative_id="preview"))
        session.add(ApplicationState(key="maintenance_work:1",
            value={"tracks": {"local": {"status": "attention", "reason": "local gap"}}}))
        session.commit()
        state = work_states(session, works)[0][1]
        assert state["processing"]["state"] == ("idle" if same_owner and same_revision else "attention")
    engine.dispose()
