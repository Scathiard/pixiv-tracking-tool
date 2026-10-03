"""Faithful 0002 fixture generated from baseline 43705a8, reusable after main upgrades."""
import os
from pathlib import Path
import subprocess
import sys
import sqlite3


def strict_client_database(path):
    path=Path(path).resolve()
    fixture=Path(__file__).with_name('legacy_client_fixture.sql')
    if fixture.is_file():
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():raise ValueError('Strict synthetic fixture path must be new')
        with sqlite3.connect(path) as connection:
            connection.executescript(fixture.read_text(encoding='utf-8'))
            assert connection.execute('SELECT version_num FROM alembic_version').fetchone()[0]=='0002_client'
            assert 'work_revisions' not in {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert 'default_revision_id' not in {row[1] for row in connection.execute('PRAGMA table_info(works)')}
            assert 'revision_id' not in {row[1] for row in connection.execute('PRAGMA table_info(assets)')}
        return path
    # Regeneration is intentionally limited to the separate, still-old checkout.
    repository=Path(__file__).resolve().parents[4]
    app=repository/'app'
    if not (app/'src/core/database.py').is_file():
        raise RuntimeError('Strict fixture needs the separate baseline main checkout')
    environment=dict(os.environ,DATA_ROOT=str(path.parent/'isolated configuration'))
    environment.pop('PYTHONPATH',None)
    code='''
from pathlib import Path
import sqlite3,sys
from src.core.database import ArchiveDatabase,SCHEMA_REVISION
from src.core.models import Artist,Work,Asset,Derivative,LocalPreference,RemoteBookmark,ArchiveIntent,Operation,OperationItem,PublicationJournal
assert SCHEMA_REVISION=='0002_client'
path=Path(sys.argv[1]);path.parent.mkdir(parents=True,exist_ok=True)
assert not path.exists()
db=ArchiveDatabase.create(path)
with db.write() as session:
    artist=Artist(id=3,pixiv_user_id='30',name='old artist',directory='原路径')
    work=Work(id=7,artist=artist,pixiv_illust_id='70',title='old',type='manga',page_count=3,time_basis='legacy_unknown',legacy_posted_at='2020-01-01 09:00:00')
    work.preference=LocalPreference(is_hidden=True,rating=2)
    work.bookmark=RemoteBookmark(value=None,source='legacy_cache')
    session.add_all([work,ArchiveIntent(pixiv_illust_id='70'),ArchiveIntent(pixiv_illust_id='80',mode='suppressed',revision=4)])
    session.flush()
    session.add_all([Asset(id='keep-original',work_id=7,role='image',page_index=0,state='verified',relative_path='原路径/70_p0.png',sha256='a'*64,byte_size=12),Asset(id='keep-gap',work_id=7,role='image',page_index=1,state='failed',error='synthetic'),Derivative(id='keep-preview',work_id=7,kind='thumbnail',relative_path='.thumbnails/70.webp',state='unknown')])
    operation=Operation(id='keep-operation',kind='repair',dedupe_key='keep-dedupe',label='pending')
    operation.items=[OperationItem(id='keep-item',item_key='keep-key',kind='download',phase='downloading',target={'work_id':7,'asset_id':'keep-gap','revision':1},depends_on=[])]
    session.add(operation);session.flush()
    session.add(PublicationJournal(id='keep-journal',operation_item_id='keep-item',asset_id='keep-gap',pixiv_illust_id='70',intent_revision=1,action='publish',staging_path='.staging/keep',target_path='原路径/70_p1.png',verification={'record_id':'keep-gap','derivative':False,'sha256':'b'*64}))
    session.add(PublicationJournal(id='keep-delete',pixiv_illust_id='80',intent_revision=4,action='delete',staging_path='old/80.png',target_path='.recovery/80.deleted'))
db.close()
connection=sqlite3.connect(path)
assert 'work_revisions' not in {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert 'default_revision_id' not in {row[1] for row in connection.execute('PRAGMA table_info(works)')}
assert 'revision_id' not in {row[1] for row in connection.execute('PRAGMA table_info(assets)')}
connection.close()
'''
    subprocess.run([sys.executable,'-c',code,str(path)],cwd=app,env=environment,check=True,capture_output=True,text=True,encoding='utf-8')
    return path
