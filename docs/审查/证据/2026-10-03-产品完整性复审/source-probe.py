"""Reproduce transient errors entering unavailable overview without remote I/O."""
import json, os, sys, time
from pathlib import Path
ROOT=Path(r'D:\claude_sandbox\pixiv-tracking-tool')
OUT=Path(__file__).parent / ('source-'+time.strftime('%Y%m%d-%H%M%S'))
OUT.mkdir()
os.environ['DATA_ROOT']=str(OUT/'fixture')
sys.path.insert(0,str(ROOT/'app'))
from src.client import PixivRequestError
from src.core.database import ArchiveDatabase
from src.core.models import Artist, Work
from src.core.source_status import SourceStatus
from src.core.library import Library
results=[]
for previous in (False,True):
    for code,status in (('api_rate_limit',429),('api_unauthorized',401),('api_unavailable',503),('work_unavailable',404)):
        db=ArchiveDatabase.create(OUT/f'{previous}-{status}.db')
        with db.write() as session:
            session.add_all([Artist(id=1,pixiv_user_id='100',name='synthetic'),
                Work(id=1,pixiv_illust_id='1000',artist_id=1,title='synthetic',page_count=1)])
        source=SourceStatus(db)
        if previous:
            source.apply_available('work',1,{'illust_id':'1000'},source.begin('work',1))
        source.apply_failure('work',1,PixivRequestError(status,'Synthetic response',failure_code=code),source.begin('work',1))
        overview=Library(db).source_overview()
        snapshot=source.snapshot('work',1)
        expected=status==404
        results.append({'prior_available':previous,'status':status,'code':code,'snapshot':snapshot,
            'overview_count':overview['total'],'pass':bool(overview['total'])==expected})
        db.close()
(OUT/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'output':str(OUT),'results':results},ensure_ascii=True))
