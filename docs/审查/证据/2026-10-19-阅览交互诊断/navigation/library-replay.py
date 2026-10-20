"""Real ArchiveDatabase and Library, three synthetic works, no Runtime worker/account."""
import os,sys,json,faulthandler
from pathlib import Path
from datetime import datetime,timedelta
from uuid import uuid4
faulthandler.dump_traceback_later(15)
root=Path(__file__).resolve().parents[4];out=Path(__file__).resolve().parent
run=out/('library-'+uuid4().hex);run.mkdir();os.environ['DATA_ROOT']=str(run/'default-isolation')
sys.path.insert(0,str(root/'app'))
print('loading database/library modules',flush=True)
from src.core.database import ArchiveDatabase
from src.core.library import Library, LibraryFilter, CursorExpired
from src.core.models import Artist, Work, Asset, RemoteBookmark
print('creating synthetic database',flush=True)
db=ArchiveDatabase.create(run/'synthetic.db');lib=Library(db);results={'synthetic_root':str(run),'observations':[]}
try:
 with db.write() as s:
  s.add(Artist(id=1,pixiv_user_id='synthetic',name='Synthetic'));s.flush()
  for i in range(1,4):
   w=Work(id=i,pixiv_illust_id=str(i),artist_id=1,title=f'Synthetic {i}',page_count=1,posted_at=datetime(2026,1,1)+timedelta(days=i));w.assets=[Asset(role='image',page_index=0,relative_path='synthetic.png',state='missing',width=160,height=120)];s.add(w)
 first=lib.list_cursor(limit=3,compact=True);cursor=first['items'][0]['cursor']
 def read(label,cursor):
  try:
   batch=lib.list_cursor(limit=2,compact=True,cursor=cursor,anchor_work_id=3)
   results['observations'].append({'case':label,'outcome':'ok','ids':[x['id'] for x in batch['items']],'generation':batch['generation']})
  except CursorExpired:results['observations'].append({'case':label,'outcome':'cursor_expired'})
 read('normal-next',cursor)
 with db.write() as s:s.add(RemoteBookmark(work_id=1,value=False,account_id='synthetic'))
 read('unrelated-bookmark-insert-default-sort',cursor)
 recovered=lib.cursor_neighbors(3)['anchor_cursor'];read('identity-recovery',recovered)
 with db.write() as s:s.get(RemoteBookmark,1).value=True
 read('unrelated-bookmark-value-change-default-sort',recovered)
 recovered=lib.cursor_neighbors(3)['anchor_cursor']
 with db.write() as s:s.get(Artist,1).last_checked_at=datetime(2026,10,19)
 read('artist-last-checked-write',recovered)
finally:db.close();faulthandler.cancel_dump_traceback_later()
(out/'library-replay-result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(results,ensure_ascii=False,indent=2))
assert [x['outcome'] for x in results['observations']]==['ok','cursor_expired','ok','cursor_expired','ok']
