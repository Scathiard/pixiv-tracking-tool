"""Independent synthetic audit probes; never access real account/gallery/config."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'app/tests'))

import pytest
from sqlalchemy import select
from test_reconcile import archive, seed
from src.core.archive import ArchiveHandler
from src.core.library import Library
from src.core.models import Asset, Work
from src import data_migration

OUT = Path(__file__).resolve().parent

def record(name, data):
    (OUT / (name + '.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

@pytest.mark.parametrize('local,remote', [(1,3),(6,2)])
def test_reconcile_retry_and_acknowledge_do_not_resolve_shape(archive, local, remote):
    db, root, client, downloader, queue = archive
    rid = seed(archive, pages=local)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.png')}
    client.counts[rid] = remote
    op = queue.submit('reconcile', {'work_ids':[1]}, 'synthetic')['operation_id']
    queue.run_round()
    first = queue.details(op)
    queue.acknowledge(op)
    repeated = queue.retry(op)['operation_id']
    queue.run_round()
    second = queue.details(repeated)
    detail = Library(db).detail(1)
    with db.read() as session:
        work = session.get(Work,1)
        assert work.page_count == local and len(work.assets) == local
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.png')}
    assert before == after
    assert first['result']['outcome'] in ('failed','partial_failure')
    assert second['result']['outcome'] in ('failed','partial_failure')
    assert downloader.calls == [] and client.calls == [rid,rid]
    assert detail['archive']['state'] == 'complete' and detail['archive']['total'] == local
    record(f'reconcile-{local}-to-{remote}', {'first':first,'after_ack_retry':second,'archive':detail['archive'],
        'availability':detail['availability'],'before':before,'after':after,'downloads':downloader.calls})

@pytest.mark.parametrize('kind', ['repair','metadata','reconcile'])
@pytest.mark.parametrize('cached_url', [False,True])
def test_missing_existing_page_with_larger_remote(archive, kind, cached_url):
    db, root, client, downloader, queue = archive
    rid = seed(archive,pages=2,missing=(1,))
    client.counts[rid] = 3
    base = queue.handlers['reconcile']
    if kind != 'reconcile':
        queue.handlers[kind] = ArchiveHandler(kind,db,base.publisher,client,downloader,
            bookmarks=base.bookmarks,source_status=base.source_status)
    if not cached_url:
        with db.write() as session:
            session.scalar(select(Asset).where(Asset.work_id == 1,Asset.page_index == 1)).source_url = None
    op = queue.submit(kind,{'work_ids':[1]},'synthetic')['operation_id']
    queue.run_round()
    result = queue.details(op)
    with db.read() as session:
        work = session.get(Work,1)
        assert work.page_count == 2 and len(work.assets) == 2
    if kind == 'repair' and cached_url:
        assert len(downloader.calls) == 1 and client.calls == []
        assert result['result']['outcome'] == 'succeeded'
    else:
        assert downloader.calls == [] and client.calls == [rid]
        assert result['result']['outcome'] in ('failed','partial_failure')
    record(f'gap-{kind}-cached-{cached_url}',{'operation':result,'downloads':downloader.calls,
        'remote_requests':client.calls,'detail':Library(db).detail(1)})

def test_complete_old_archive_repair_does_not_discover_added_pages(archive):
    db, root, client, downloader, queue = archive
    rid = seed(archive,pages=1)
    client.counts[rid] = 3
    base = queue.handlers['reconcile']
    queue.handlers['repair'] = ArchiveHandler('repair',db,base.publisher,client,downloader,
        bookmarks=base.bookmarks,source_status=base.source_status)
    op=queue.submit('repair',{'work_ids':[1]},'synthetic')['operation_id']
    queue.run_round()
    result=queue.details(op)
    assert result['result']['outcome']=='succeeded' and client.calls==[] and downloader.calls==[]
    detail=Library(db).detail(1)
    assert detail['page_count']==1 and detail['archive']['total']==1
    record('complete-old-archive-repair',{'operation':result,'detail':detail,'remote_requests':client.calls})

def test_migration_rejects_same_size_content_change(tmp_path,monkeypatch):
    source,target=tmp_path/'source',tmp_path/'target'
    (source/'images').mkdir(parents=True)
    (source/'data').mkdir()
    (source/'images'/'synthetic.bin').write_bytes(b'original')
    original_copy=data_migration.shutil.copy2
    def tamper(src,dst,*args,**kwargs):
        result=original_copy(src,dst,*args,**kwargs)
        if Path(dst).name=='synthetic.bin':
            Path(dst).write_bytes(b'changed!')
        return result
    monkeypatch.setattr(data_migration.shutil,'copy2',tamper)
    with pytest.raises(data_migration.MigrationError) as failure:
        data_migration.execute_migration(data_migration.prepare_migration(source,target))
    assert (source/'images'/'synthetic.bin').read_bytes()==b'original'
    assert (source/'images'/'synthetic.bin').stat().st_size==(target/'images'/'synthetic.bin').stat().st_size
    record('same-size-migration-tamper',{'failure':str(failure.value),
        'source':data_migration._manifest(source/'images'),'target':data_migration._manifest(target/'images')})

def test_packaged_migration_has_hash_comparison():
    import dis, types
    from PyInstaller.archive.readers import CArchiveReader
    exe=ROOT/'release/v0.2.0/build-20261003.3/PixivTracker_v0.2.0_build20261003.3.exe'
    digest=hashlib.sha256(exe.read_bytes()).hexdigest()
    assert digest=='89503bd9d6568d8e5ac2de6fea1f6068c8882c77ffd1c7f2b5394a7b454739e7'
    archive_reader=CArchiveReader(str(exe)).open_embedded_archive('PYZ.pyz')
    code=archive_reader.extract('src.data_migration')
    functions={item.co_name:item for item in code.co_consts if isinstance(item,types.CodeType)}
    manifest=functions['_manifest']
    assert {'hashlib','file_digest','hexdigest'}.issubset(set(manifest.co_names)) and 'sha256' in manifest.co_consts
    text='EXE SHA-256: '+digest+'\n\n'+dis.Bytecode(manifest).dis()+'\n'+dis.Bytecode(functions['execute_migration']).dis()
    (OUT/'packaged-migration-disassembly.txt').write_text(text,encoding='utf-8')
    record('packaged-migration-summary',{'sha256':digest,'manifest_names':list(manifest.co_names),
        'manifest_constants':[x for x in manifest.co_consts if isinstance(x,(str,int,type(None)))],
        'module_executed':False})
