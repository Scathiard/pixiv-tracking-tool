"""Settings shape materialization and unusual persisted reuse semantics."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.core.models import ArchiveIntent, Artist, Asset, Base, LocalPreference, RevisionAsset, Work, WorkRevision
from src.routes.settings_api import create_settings_router


def test_settings_materializes_shape_once_without_losing_cross_work_reuse(monkeypatch):
    import src.routes.settings_api as settings
    monkeypatch.setattr(settings, "lan_addresses", lambda port: [])
    monkeypatch.setattr(settings, "get_pending_login_url", lambda: None)
    monkeypatch.setattr(settings, "interval_snapshot", lambda db: {"minutes": 60, "next_check_at": None})
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        session.add_all([Work(id=i, pixiv_illust_id=str(100+i), artist_id=1, title="synthetic",
            type="ugoira" if i == 4 else "manga", page_count=99 if i == 1 else 2 if i == 5 else 1,
            default_revision_id={1:"r1",2:"r2",3:"r3",6:"dangling"}.get(i),
            caption="x"*4096, source_metadata={"synthetic":"x"*4096}) for i in range(1,7)])
        session.add_all([WorkRevision(id=rid,work_id=wid,ordinal=ordinal,title="synthetic",
            type="manga",page_count=pages,source_signature=rid,caption="x"*4096,
            source_metadata={"synthetic":"x"*4096}) for rid,wid,ordinal,pages in
            [("r-old",1,1,1),("r1",1,2,2),("r2",2,1,3),("r3",3,1,1)]])
        session.add_all([
            Asset(id="shared",work_id=1,revision_id="r-old",role="image",page_index=0,state="verified",relative_path="old.png"),
            Asset(id="suppressed-selection-only",work_id=1,revision_id="r-old",role="image",page_index=1,state="unknown",relative_path="old-second.png"),
            Asset(id="dangling-owner",work_id=6,role="image",page_index=0,state="failed",relative_path="failed.png"),
            Asset(id="zip",work_id=4,role="ugoira_zip",state="verified",relative_path=None),
            Asset(id="timing",work_id=4,role="ugoira_timing",state="verified",relative_path="timing.json"),
            Asset(id="invalid",work_id=5,role="image",page_index=0,state="invalid",relative_path="invalid.png"),
            Asset(id="empty",work_id=5,role="image",page_index=1,state="unknown",relative_path=""),
            Asset(id="out-of-range",work_id=5,role="image",page_index=2,state="verified",relative_path="outside.png"),
        ])
        session.add_all([
            RevisionAsset(revision_id="r1",role="image",page_index=0,asset_id="shared"),
            RevisionAsset(revision_id="r2",role="image",page_index=0,asset_id="shared"),
            RevisionAsset(revision_id="r3",role="image",page_index=0,asset_id="shared"),
            # Selection membership is global, while statistics belong to the
            # Asset's work. Preserve this unusual pre-existing boundary.
            RevisionAsset(revision_id="r2",role="image",page_index=1,asset_id="suppressed-selection-only"),
            RevisionAsset(revision_id="r2",role="image",page_index=2,asset_id="dangling-owner"),
            LocalPreference(work_id=1,is_hidden=True), ArchiveIntent(pixiv_illust_id="102",mode="suppressed"),
        ])
    class MemoryDatabase:
        from src.core.read_control import ReadController
        reads = ReadController()
        @contextmanager
        def read(self):
            with Session(engine) as session:
                session.connection().exec_driver_sql("BEGIN")
                yield session
    runtime = SimpleNamespace(database=MemoryDatabase(),client=None,queue=SimpleNamespace(automatic_paused=lambda: False))
    app = FastAPI()
    app.include_router(create_settings_router(lambda:runtime))
    captured = []
    def capture(conn,cursor,sql,params,context,many):
        if "downloaded_pages" in sql:
            captured.append((sql,params))
    event.listen(engine,"before_cursor_execute",capture)
    try:
        with TestClient(app) as client:
            response = client.get("/api/settings")
            assert response.status_code == 200
            assert response.json()["stats"] == {"artists":1,"works":6,"downloaded_pages":1,
                "registered_originals":4,"verified_originals":2,"unverified_originals":1,"missing_originals":5}
        event.remove(engine,"before_cursor_execute",capture)
        assert len(captured)==1
        sql,params=captured[0]
        with engine.connect() as connection:
            plan=[tuple(row) for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN "+sql,params)]
        evidence={"plan":plan,"stats":response.json()["stats"],"sql":sql}
        artifact=os.environ.get("SETTINGS_PROJECTION_ARTIFACTS")
        if artifact:
            Path(artifact).mkdir(parents=True,exist_ok=True)
            Path(artifact,"settings-projection-plan.json").write_text(json.dumps(evidence,indent=2),encoding="utf-8")
        assert "WITH current_works AS MATERIALIZED" in sql, plan
        assert sum("SEARCH work_revisions " in row[3] for row in plan)==1, plan
        assert not any("SEARCH works USING INTEGER PRIMARY KEY" in row[3] for row in plan), plan
    finally:
        if event.contains(engine,"before_cursor_execute",capture):
            event.remove(engine,"before_cursor_execute",capture)
        engine.dispose()
