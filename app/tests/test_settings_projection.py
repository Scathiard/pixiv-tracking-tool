"""Settings shape materialization and unusual persisted reuse semantics."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import psutil
from sqlalchemy import create_engine, event, insert, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.core.models import ArchiveIntent, Artist, Asset, Base, LocalPreference, RevisionAsset, Work, WorkRevision
from src.core.database import ArchiveDatabase
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
        assert "current_works AS MATERIALIZED" in sql, plan
        assert "revision_shapes AS MATERIALIZED" not in sql, plan
        assert any("SEARCH r USING COVERING INDEX ix_revisions_settings_shape" in row[3]
                   for row in plan), plan
        assert not any("SCAN work_revisions" in row[3] or "SCAN r " in row[3] for row in plan), plan
        assert not any("SEARCH works USING INTEGER PRIMARY KEY" in row[3] for row in plan), plan
    finally:
        if event.contains(engine,"before_cursor_execute",capture):
            event.remove(engine,"before_cursor_execute",capture)
        engine.dispose()


def test_settings_dense_history_stays_bounded_and_default_switch_is_immediate(tmp_path, monkeypatch):
    import src.routes.settings_api as settings
    monkeypatch.setattr(settings, 'lan_addresses', lambda port: [])
    monkeypatch.setattr(settings, 'get_pending_login_url', lambda: None)
    monkeypatch.setattr(settings, 'interval_snapshot', lambda db: {'minutes': 60, 'next_check_at': None})
    process = psutil.Process()
    def memory():
        info = process.memory_info()
        return {'rss_bytes': info.rss, 'vms_bytes': info.vms,
                'process_lifetime_peak_working_set_bytes': getattr(info, 'peak_wset', info.rss)}
    history_count, batch_size = 20_000, 500
    evidence = {'history_revisions': history_count, 'insert_batch_size': batch_size,
                'memory_before_fixture': memory(), 'requests': [], 'queries': []}
    database = ArchiveDatabase.create(tmp_path/'dense-history.db')
    try:
        with database.write() as session:
            session.execute(insert(Artist), [{'id': 1, 'pixiv_user_id': '1', 'name': 'synthetic'}])
            # The fallback work and old versions differ from the selected
            # manga shape. Statistics must use the default version's shape.
            session.execute(insert(Work), [{'id': 1, 'artist_id': 1, 'pixiv_illust_id': '101',
                'title': 'synthetic', 'type': 'illust', 'page_count': 97,
                'default_revision_id': 'current-manga', 'latest_revision_id': 'current-animation'}])
            for start in range(0, history_count, batch_size):
                session.execute(insert(WorkRevision), [{'id': f'{ident:032x}', 'work_id': 1,
                    'ordinal': ident + 1, 'title': 'synthetic history', 'type': 'ugoira', 'page_count': 1,
                    'source_signature': f'history-{ident}', 'caption': 'c'*512,
                    'source_metadata': {'synthetic': 'm'*512}}
                    for ident in range(start, min(start + batch_size, history_count))])
            session.execute(insert(WorkRevision), [
                {'id': 'current-manga', 'work_id': 1, 'ordinal': history_count + 1,
                 'title': 'current manga', 'type': 'manga', 'page_count': 3, 'source_signature': 'manga'},
                {'id': 'current-animation', 'work_id': 1, 'ordinal': history_count + 2,
                 'title': 'current animation', 'type': 'ugoira', 'page_count': 8, 'source_signature': 'animation'},
            ])
            # Original ownership is historical, but only selected revisions
            # bind the reused files; an unbound historical file is excluded.
            session.execute(insert(Asset), [
                {'id': 'shared-image', 'work_id': 1, 'revision_id': f'{0:032x}', 'role': 'image',
                 'page_index': 0, 'state': 'verified', 'relative_path': 'synthetic/shared.png'},
                {'id': 'shared-unknown', 'work_id': 1, 'revision_id': f'{0:032x}', 'role': 'image',
                 'page_index': 1, 'state': 'unknown', 'relative_path': 'synthetic/unknown.png'},
                {'id': 'shared-zip', 'work_id': 1, 'revision_id': f'{0:032x}', 'role': 'ugoira_zip',
                 'page_index': 0, 'state': 'verified', 'relative_path': 'synthetic/animation.zip'},
                {'id': 'unbound-history', 'work_id': 1, 'revision_id': f'{1:032x}', 'role': 'image',
                 'page_index': 0, 'state': 'verified', 'relative_path': 'synthetic/history.png'},
            ])
            session.execute(insert(RevisionAsset), [
                {'revision_id': 'current-manga', 'role': 'image', 'page_index': 0, 'asset_id': 'shared-image'},
                {'revision_id': 'current-manga', 'role': 'image', 'page_index': 1, 'asset_id': 'shared-unknown'},
                {'revision_id': 'current-animation', 'role': 'image', 'page_index': 0, 'asset_id': 'shared-image'},
                {'revision_id': 'current-animation', 'role': 'ugoira_zip', 'page_index': 0, 'asset_id': 'shared-zip'},
            ])
        evidence['memory_after_fixture'] = memory()
        evidence['fixture_bytes'] = sum(path.stat().st_size for path in tmp_path.glob('dense-history.db*'))
        assert evidence['fixture_bytes'] < 100 * 1024 * 1024, evidence['fixture_bytes']
        runtime = SimpleNamespace(database=database, client=None,
                                  queue=SimpleNamespace(automatic_paused=lambda: False))
        app = FastAPI()
        app.include_router(create_settings_router(lambda: runtime))
        captured, starts = [], {}
        def before(conn, cursor, sql, params, context, many):
            if sql.lstrip().upper().startswith('WITH') and 'downloaded_pages' in sql:
                starts[id(context)] = time.perf_counter()
        def after(conn, cursor, sql, params, context, many):
            began = starts.pop(id(context), None)
            if began is not None:
                captured.append((sql, params, time.perf_counter() - began))
        event.listen(database.engine, 'before_cursor_execute', before)
        event.listen(database.engine, 'after_cursor_execute', after)
        manga = {'artists': 1, 'works': 1, 'downloaded_pages': 1, 'registered_originals': 2,
                 'verified_originals': 1, 'unverified_originals': 1, 'missing_originals': 1}
        animation = {'artists': 1, 'works': 1, 'downloaded_pages': 0, 'registered_originals': 1,
                     'verified_originals': 1, 'unverified_originals': 0, 'missing_originals': 0}
        try:
            with TestClient(app) as client:
                for revision, expected in [('current-manga', manga), ('current-manga', manga),
                                           ('current-animation', animation), ('current-manga', manga)]:
                    with database.write() as session:
                        session.execute(update(Work).where(Work.id == 1).values(default_revision_id=revision))
                    memory_before = memory()
                    began = time.perf_counter()
                    response = client.get('/api/settings')
                    elapsed = time.perf_counter() - began
                    evidence['requests'].append({'revision': revision, 'seconds': elapsed,
                        'status': response.status_code, 'memory_before': memory_before, 'memory_after': memory()})
                    assert response.status_code == 200, response.text
                    assert response.json()['stats'] == expected
                    evidence['requests'][-1]['stats'] = response.json()['stats']
        finally:
            event.remove(database.engine, 'before_cursor_execute', before)
            event.remove(database.engine, 'after_cursor_execute', after)
            with database.engine.connect() as connection:
                for sql, params, seconds in captured:
                    plan = [list(row) for row in connection.exec_driver_sql('EXPLAIN QUERY PLAN ' + sql, params)]
                    evidence['queries'].append({'sql': sql, 'seconds': seconds, 'plan': plan})
        assert len(captured) == 4, evidence
        for query in evidence['queries']:
            plan = query['plan']
            assert any('SEARCH r USING COVERING INDEX ix_revisions_settings_shape' in row[3]
                       for row in plan), plan
            assert not any('SCAN work_revisions' in row[3] or 'SCAN r ' in row[3] for row in plan), plan
            assert query['seconds'] < 4.5, evidence['queries']
        assert max(request['seconds'] for request in evidence['requests']) < 5.0, evidence['requests']
    finally:
        evidence['memory_at_finish'] = memory()
        (tmp_path/'dense-history-metrics.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        artifact = os.environ.get('SETTINGS_PROJECTION_ARTIFACTS')
        if artifact:
            Path(artifact).mkdir(parents=True, exist_ok=True)
            (Path(artifact)/'dense-history-metrics.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        database.close()
