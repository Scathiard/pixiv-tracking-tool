"""Icon-only v0.2.4 browser checks using synthetic media and account only."""
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback

APP = Path(r'D:\claude_sandbox\pixiv-tracking-tool\app')
artifacts = Path(sys.argv[1]).resolve()
artifacts.mkdir(parents=True, exist_ok=False)
os.environ['DATA_ROOT'] = str(artifacts / 'fixture')
sys.path[:0] = [str(APP), str(APP / 'tests')]
with socket.socket() as probe:
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
os.environ['PORT'] = str(port)
import uvicorn
from playwright.sync_api import sync_playwright, expect
from client_fixture import make_library
from src.core.models import Work

runtime, remote = make_library(artifacts / 'fixture', 32)
from datetime import datetime
from src.core.models import RemoteBookmark
with runtime.database.write() as session:
    for row in session.query(RemoteBookmark).all():
        row.value=True; row.account_id='90001'; row.source='readback'
        row.confirmed_at=datetime.utcnow(); row.version=1
        remote.values[str(100000+row.work_id)]=True
remote_reads=[]
gate=threading.Event()
def measured_read(remote_id):
    remote_reads.append({'id':remote_id,'at':time.monotonic()})
    if not gate.wait(15):
        raise TimeoutError('Synthetic gate held for audit')
    return True
remote.read_bookmark=measured_read
from src.auth import _find_system_browser
from src.web import create_app
server=uvicorn.Server(uvicorn.Config(create_app(lambda:runtime,authenticate=False,schedule=False,reading_ui=True),host='127.0.0.1',port=port,log_level='error',loop='src.networking:http_loop_factory'))
thread=threading.Thread(target=server.run,daemon=True)
thread.start()
origin=f'http://127.0.0.1:{port}'
requests=[]
observations={}
started=time.monotonic()
def count(): return len(remote_reads)
try:
    deadline=time.monotonic()+20
    while not server.started and time.monotonic()<deadline: time.sleep(.05)
    assert server.started
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,executable_path=_find_system_browser())
        context=browser.new_context(viewport={'width':1280,'height':900})
        page=context.new_page()
        page.on('request',lambda req:requests.append({'method':req.method,'url':req.url.replace(origin,''),'at':round(time.monotonic()-started,3)}) if '/api/' in req.url else None)
        page.goto(origin)
        page.locator('.work-card').first.wait_for()
        page.wait_for_timeout(200)
        local_gallery=page.request.get(origin+'/api/library?compact=1&limit=50').json()
        card_states=page.locator('.work-card').evaluate_all("cards=>cards.map(c=>({id:Number(c.dataset.workId),heart:c.querySelector('.bookmark-button')?.className}))")
        observations['home_persisted_facts']={'true_local_values':sum(item['bookmark']['value'] is True for item in local_gallery['items']),'visible_cards':len(card_states),'red_hearts':sum('on' in (card['heart'] or '').split() for card in card_states),'remote_reads':count()}
        page.goto(origin+'/illust/31')
        heart=page.locator('.detail-bottom-actions .bookmark-button')
        heart.wait_for()
        deadline=time.monotonic()+5
        while not remote_reads and time.monotonic()<deadline: page.wait_for_timeout(50)
        local=page.request.get(origin+'/api/works/31/bookmark').json()
        observations['persisted_true_before_readback']={'local':local,'class':heart.get_attribute('class'),'red': 'on' in heart.get_attribute('class').split(),'remote_reads':count()}
        assert local['value'] is True
        gate.set()
        page.wait_for_function("document.querySelector('.detail-bottom-actions .bookmark-button')?.classList.contains('on')")
        observations['after_readback']={'red':True,'remote_reads':count()}
        before=count()
        page.reload()
        heart.wait_for()
        page.wait_for_function("document.querySelector('.detail-bottom-actions .bookmark-button')?.classList.contains('on')")
        page.wait_for_timeout(250)
        observations['reload_within_fresh_period']={'new_remote_reads':count()-before}
        before=count()
        second=context.new_page()
        second.goto(origin+'/illust/31')
        second.locator('.detail-bottom-actions .bookmark-button').wait_for()
        second.wait_for_function("document.querySelector('.detail-bottom-actions .bookmark-button')?.classList.contains('on')")
        second.wait_for_timeout(250)
        observations['second_tab_same_fresh_work']={'new_remote_reads':count()-before}
        second.close()
        before=count()
        page.get_by_role('navigation',name='主导航').get_by_role('button',name='流式',exact=False).click()
        page.locator('.page-viewport.feed').wait_for()
        for _ in range(4):
            page.get_by_role('button',name='跳过作品',exact=False).click()
            page.wait_for_timeout(2700)
        observations['four_feed_skips']={'new_remote_reads':count()-before,'toolbar_class':page.locator('.reading-toolbar .bookmark-button').get_attribute('class')}
        context.close();browser.close()
finally:
    gate.set();server.should_exit=True;thread.join(20)
    (artifacts/'result.json').write_text(json.dumps({'observations':observations,'requests':requests,'remote_reads':remote_reads,'real_account_used':False,'real_library_used':False},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(observations,ensure_ascii=False,indent=2))
assert observations['home_persisted_facts']['red_hearts'] > 0, 'Home ignores all saved confirmed true values'
assert observations['persisted_true_before_readback']['red'], 'Persisted true bookmark is hidden until remote readback'
assert observations['reload_within_fresh_period']['new_remote_reads']==0, 'Fresh confirmed bookmark is rechecked on reload'
