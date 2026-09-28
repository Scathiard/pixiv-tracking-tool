from pathlib import Path
import json,sys,time
from datetime import datetime
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'app'))
from src.core.bookmarks import BookmarkService
from src.core.database import ArchiveDatabase
from src.core.models import Artist,Work,RemoteBookmark,BookmarkOperation
root=Path(__file__).resolve().parent/'service-01'
root.mkdir(exist_ok=False)
db=ArchiveDatabase.create(root/'synthetic.db')
with db.write() as session:
    session.add(Artist(id=1,pixiv_user_id='100',name='Synthetic'))
    session.add(Work(id=1,pixiv_illust_id='1001',artist_id=1,title='Synthetic'))
    session.add(RemoteBookmark(work_id=1,value=True,source='readback',account_id='90001',confirmed_at=datetime(2026,9,28),version=1))
class Remote:
    reads=0
    def get_account_id(self):return '90001'
    def read_bookmark_bound(self,work,account):self.reads+=1;return True
remote=Remote(); clock=[100.]
service=BookmarkService(db,remote,clock=lambda:clock[0],current_account=lambda:'90001')
service.start()
def refresh():
    receipt=service.refresh(1)
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        state=service.operation(receipt['operation_id'])
        if state['status']!='pending':return state
        time.sleep(.005)
    raise AssertionError('synthetic refresh did not settle')
try:
    result={'after_restart_before_network':service.state(1)}
    result['first_refresh']=refresh()
    result['fresh_repeat_refresh']=refresh()
    result['remote_reads_before_ttl_expired']=remote.reads
    clock[0]+=46
    result['expired_cached_fact']=service.state(1)
finally:service.stop();db.close()
(root/'result.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps({'persisted_true_value':result['after_restart_before_network']['value'],'cached_on_restart':result['after_restart_before_network']['cached'],'reads_within_45s':remote.reads,'cached_after_46s':result['expired_cached_fact']['cached']},indent=2))
assert remote.reads==1,'Automatic refresh bypasses existing fresh fact'
