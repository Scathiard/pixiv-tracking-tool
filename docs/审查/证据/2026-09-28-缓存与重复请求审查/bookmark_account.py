from pathlib import Path
import json,sys,time
from datetime import datetime
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'app'))
from src.core.bookmarks import BookmarkService
from src.core.database import ArchiveDatabase
from src.core.models import Artist,Work,RemoteBookmark
root=Path(__file__).resolve().parent/'account-01';root.mkdir(exist_ok=False)
db=ArchiveDatabase.create(root/'synthetic.db')
with db.write() as session:
    session.add(Artist(id=1,pixiv_user_id='100',name='Synthetic'))
    session.add(Work(id=1,pixiv_illust_id='1001',artist_id=1,title='Synthetic'))
    session.add(RemoteBookmark(work_id=1,value=True,source='readback',account_id='account-A',confirmed_at=datetime(2026,9,28),version=1))
class Remote:
    account='account-B';values={'account-A':True,'account-B':True};writes=[]
    def get_account_id(self):return self.account
    def write_bookmark(self,work,desired,account):
        self.writes.append({'account':account,'desired':desired});self.values[account]=desired
    def read_bookmark_bound(self,work,account):return self.values[account]
remote=Remote();service=BookmarkService(db,remote,current_account=lambda:remote.account);service.start()
try:
    state=service.state(1)
    desired=not state['value'] if type(state['value']) is bool else True
    result={'ui_state':state,'ui_heart_on':state['value'] is True and state['status']=='confirmed' and not state['cached'],'frontend_target':desired,'account_B_before':remote.values['account-B']}
    receipt=service.submit(1,desired,str(uuid4()))
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        settled=service.operation(receipt['operation_id'])
        if settled['status']!='pending':break
        time.sleep(.005)
    result.update({'writes':remote.writes,'account_B_after':remote.values['account-B'],'result':settled,'real_account_used':False})
finally:service.stop();db.close()
(root/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:result[k] for k in ['ui_heart_on','frontend_target','account_B_before','writes','account_B_after']},indent=2))
assert result['account_B_after'] is True,'An empty displayed heart cancelled the current synthetic account bookmark using another account cache'
