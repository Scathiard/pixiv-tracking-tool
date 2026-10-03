"""Startup upgrade UI contract uses isolated old SQLite and no Pixiv login."""
import asyncio
import json
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace
from fastapi.testclient import TestClient
from src.core.database import ArchiveDatabase, SCHEMA_REVISION
from src.core.models import ApplicationState
from src.core.startup_upgrade import StartupUpgrade
from test_upgrade_revisions import client_database
from strict_legacy_fixture import strict_client_database


def schema(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def test_first_start_requires_confirmation_then_backup_activate_and_open_library(tmp_path,monkeypatch):
    from src import web
    data=tmp_path/"data"; data.mkdir(); images=tmp_path/"images"
    source=strict_client_database(data/"pixiv_tracker.db")
    before=source.read_bytes()
    monkeypatch.setattr(web._cfg,"DATA_DIR",data);monkeypatch.setattr(web._cfg,"IMAGES_DIR",images)
    app=web.create_app(authenticate=False,schedule=False)
    with TestClient(app,base_url="http://127.0.0.1",client=("127.0.0.1",50000)) as client:
        assert client.get("/api/startup").json()["state"]=="required"
        assert client.get("/api/library").status_code==503
        client.headers.update({"Origin":"http://127.0.0.1","X-CSRF-Token":client.cookies.get("pixiv_csrf")})
        assert client.post("/api/startup/upgrade",json={"confirmed":False}).status_code==422
        assert schema(source)=="0002_client"
        assert source.read_bytes()==before
        accepted=client.post("/api/startup/upgrade",json={"confirmed":True})
        assert accepted.status_code==200
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            state=client.get("/api/startup").json()
            if state["state"] in ("ready","failed"):break
            time.sleep(.02)
        assert state["state"]=="ready",state
        assert schema(source)==SCHEMA_REVISION
        assert client.get("/api/library?show_hidden=1").json()["total"]==1
        assert client.post("/api/startup/upgrade",json={"confirmed":True}).status_code==409
        assert client.get("/api/status").json()["upgrade_required"] is False
        backups=list((data/"upgrade").glob("*/backup.db"));receipts=list((data/"upgrade").glob("*/receipt.json"))
        assert len(backups)==len(receipts)==1 and schema(backups[0])=="0002_client"
        with closing(sqlite3.connect(source)) as connection:
            assert connection.execute("SELECT relative_path FROM assets WHERE id='keep-original'").fetchone()[0]=="原路径/70_p0.png"
            assert connection.execute("SELECT rating,is_hidden FROM local_preferences WHERE work_id=7").fetchone()==(2,1)
            assert connection.execute("SELECT time_basis,legacy_posted_at FROM works WHERE id=7").fetchone()==("legacy_unknown","2020-01-01 09:00:00")
            target=json.loads(connection.execute("SELECT target FROM operation_items WHERE id='keep-item'").fetchone()[0])
            assert target["asset_id"]=="keep-gap" and target["work_revision_id"]
            assert connection.execute("SELECT count(*) FROM publication_journal WHERE id IN ('keep-journal','keep-delete')").fetchone()[0]==2


def run(controller):
    async def execute():
        async def ready(current):pass
        await controller.start(ready);await controller.task
    asyncio.run(execute())


def test_constructor_failure_before_workers_rolls_back_old_database_and_keeps_candidates(tmp_path):
    source=client_database(tmp_path/"source.db")
    def broken():raise RuntimeError("SECRET token")
    controller=StartupUpgrade(source,tmp_path/"images",broken);controller.require();run(controller)
    state=controller.snapshot()
    assert state["state"]=="failed" and state["activated"] is False
    assert schema(source)=="0002_client" and "SECRET" not in str(state)
    assert list((tmp_path/"upgrade").glob("*/backup.db"))
    assert list((tmp_path/"upgrade").glob("*/candidate.db"))


def test_failure_after_runtime_recovery_writes_never_restores_old_snapshot(tmp_path):
    source=client_database(tmp_path/"source.db")
    class Runtime:
        def __init__(self):self.database=ArchiveDatabase(source)
        def start(self):
            with self.database.write() as session:session.add(ApplicationState(key="post-activation",value={"preserve":True}))
            raise RuntimeError("SECRET internal")
        def request_stop(self,**kwargs):pass
        def close(self,**kwargs):self.database.close()
    controller=StartupUpgrade(source,tmp_path/"images",Runtime);controller.require();run(controller)
    assert controller.snapshot()["state"]=="failed" and controller.snapshot()["activated"] is True
    assert schema(source)==SCHEMA_REVISION
    db=ArchiveDatabase(source)
    with db.read() as session:assert session.get(ApplicationState,"post-activation").value=={"preserve":True}
    db.close()


def test_other_writer_blocks_preparation_without_touching_old_database(tmp_path):
    from src.core.ownership import LibraryLease
    source=client_database(tmp_path/"source.db");before=source.read_bytes();images=tmp_path/"images"
    controller=StartupUpgrade(source,images,lambda:SimpleNamespace());controller.require()
    with LibraryLease(images):run(controller)
    assert controller.snapshot()["state"]=="failed"
    assert source.read_bytes()==before
    assert not (tmp_path/"upgrade").exists()


def test_shutdown_after_preparation_preserves_unactivated_old_library(tmp_path,monkeypatch):
    import src.core.startup_upgrade as startup
    source=client_database(tmp_path/"source.db");stopping=[False]
    prepare=startup.prepare_upgrade
    def prepared(*args):
        result=prepare(*args);stopping[0]=True;return result
    monkeypatch.setattr(startup,"prepare_upgrade",prepared)
    controller=StartupUpgrade(source,tmp_path/"images",lambda:None,stopping=lambda:stopping[0]);controller.require();run(controller)
    assert schema(source)=="0002_client"
    assert controller.snapshot()["activated"] is False
    assert controller.snapshot()["can_start"] is False
    assert list((tmp_path/"upgrade").glob("*/backup.db"))
