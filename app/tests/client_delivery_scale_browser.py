"""Fixed-input production browser load with 5,000 works and one 2,000-page work."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs,urlsplit


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--artifacts',required=True,type=Path)
    out=parser.parse_args().artifacts.resolve();out.mkdir(parents=True,exist_ok=False)
    os.environ['DATA_ROOT']=str(out/'configuration');sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from client_fixture import make_library
    from src.auth import _find_system_browser
    from src.web import create_app
    from src.core.tasks import ItemSpec,ItemResult
    from playwright.sync_api import sync_playwright
    import uvicorn
    runtime,remote=make_library(out/'fixture',works=5000)
    gate=threading.Event();entered=threading.Event()
    class ProbeTask:
        def plan(self,parameters,context):return [ItemSpec('probe','verify',{},'verifying')]
        def execute(self,item,context):
            entered.set();assert gate.wait(40);return ItemResult()
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    server=uvicorn.Server(uvicorn.Config(create_app(lambda:runtime,authenticate=False,schedule=False,reading_ui=True),host='127.0.0.1',port=port,log_level='error',loop='src.networking:http_loop_factory',limit_concurrency=128))
    worker=threading.Thread(target=server.run,daemon=True);worker.start()
    result={'checks':[],'samples':[],'errors':[],'failures':[],'works':5000,'long_work_pages':2000,'real_account_used':False,'backend':'real isolated Runtime','background':'document visibility event simulation','task':'real persisted queue with synthetic gated verification handler'}
    page=None;active=set();requests=[];peak=[0];all_page_ids=set()
    try:
        deadline=time.monotonic()+30
        while not server.started and time.monotonic()<deadline:time.sleep(.05)
        assert server.started
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,executable_path=_find_system_browser())
            context=browser.new_context(viewport={'width':1440,'height':960})
            page=context.new_page();page.set_default_timeout(15000)
            page.on('pageerror',lambda error:result['errors'].append(str(error)))
            def request_started(request):
                active.add(request);requests.append({'url':urlsplit(request.url).path,'at':time.monotonic()});peak[0]=max(peak[0],len(active))
            page.on('request',request_started);page.on('requestfinished',lambda request:active.discard(request));page.on('requestfailed',lambda request:active.discard(request))
            def response_seen(response):
                parameters=parse_qs(urlsplit(response.url).query)
                if urlsplit(response.url).path=='/api/library' and parameters.get('limit')==['500'] and response.ok:
                    payload=response.json();assert len(payload['items'])==500
                    all_page_ids.update(item['id'] for item in payload['items'])
            page.on('response',response_seen)
            cdp=context.new_cdp_session(page);cdp.send('Performance.enable')
            def sample(stage):
                page.wait_for_timeout(100);cdp.send('HeapProfiler.collectGarbage')
                values={item['name']:item['value'] for item in cdp.send('Performance.getMetrics')['metrics']}
                state=page.evaluate("""()=>({cards:document.querySelectorAll('.work-card').length,pages:document.querySelectorAll('.image-page').length,images:document.querySelectorAll('img[src]').length,media:document.querySelectorAll('img[src],video,audio').length,galleryWindow:+(document.querySelector('.gallery-window')?.dataset.windowSize||0),readerWindow:+(document.querySelector('.page-window')?.dataset.windowSize||0),nodes:document.getElementsByTagName('*').length})""")
                state.update(stage=stage,heap_bytes=values['JSHeapUsedSize'],active_requests=len(active),total_requests=len(requests),visit_count=len(runtime.reading._visits),order_cache_max=max((len(visit.orders) for visit in runtime.reading._visits.values()),default=0))
                assert state['cards']<70 and state['pages']<15 and state['readerWindow']<=120,state
                result['samples'].append(state);return state
            page.goto(origin);page.locator('.work-card').first.wait_for()
            page.evaluate("localStorage.setItem('pixiv-local-page-size','500')");page.reload();page.get_by_role('spinbutton',name='图库页码',exact=True).wait_for()
            page.wait_for_function("document.querySelector('input[aria-label=图库页码]')?.max==='10'")
            for number in range(1,11):
                field=page.get_by_role('spinbutton',name='图库页码',exact=True);field.fill(str(number));field.press('Tab')
                start=(number-1)*500
                page.wait_for_function("start=>document.querySelector('.gallery-window')?.dataset.start===String(start)&&document.querySelector('.gallery-viewport')?.getAttribute('aria-busy')==='false'",arg=start)
                page.locator('.gallery-viewport').evaluate('e=>e.scrollTop=e.scrollHeight')
                page.locator(f'.work-card[data-work-id="{5000-number*500+1}"]').wait_for()
                state=sample(f'pagination-500-page-{number}');assert state['galleryWindow']==500
            assert len(all_page_ids)==5000
            result['checks'].append('all-ten-500-record-pages-real-responses-cover-5000-identities-and-render-last-card')
            page.evaluate("localStorage.setItem('pixiv-local-page-size','scroll')");page.goto(origin);page.locator('.work-card').first.wait_for()
            for step in range(30):
                previous=page.locator('.gallery-window').get_attribute('data-start')
                page.locator('.gallery-viewport').evaluate('e=>e.scrollTop=e.scrollHeight')
                page.wait_for_timeout(160)
                page.wait_for_function("document.querySelector('.gallery-viewport').getAttribute('aria-busy')==='false'")
                state=sample(f'continuous-forward-{step}');assert state['galleryWindow']<=480
                if page.locator('.work-card[data-work-id="1"]').count():break
            page.locator('.work-card[data-work-id="1"]').wait_for()
            for step in range(8):
                page.locator('.gallery-viewport').evaluate('e=>e.scrollTop=0');page.wait_for_timeout(180)
                page.wait_for_function("document.querySelector('.gallery-viewport').getAttribute('aria-busy')==='false'")
                state=sample(f'continuous-backward-{step}');assert state['galleryWindow']<=480
            result['checks'].append('continuous-real-scroll-to-work5000-and-back-retains-at-most480-records')
            page.goto(origin+'/illust/5000');page.locator('.image-page').first.wait_for()
            for step in range(55):
                page.locator('.page-viewport').evaluate('e=>e.scrollTop=e.scrollHeight-120')
                page.wait_for_timeout(170)
                if step%4==0:sample(f'long-reader-forward-{step}')
                if page.locator('.image-page[data-page="1999"]').count():break
            page.locator('.image-page[data-page="1999"]').wait_for()
            for step in range(8):
                page.locator('.page-viewport').evaluate('e=>e.scrollTop=0');page.wait_for_timeout(170);sample(f'long-reader-backward-{step}')
            result['checks'].append('actual-2000-page-work-final-page-and-back-retains-at-most120-records')
            # A short actual local scope makes repeated round boundaries bounded.
            page.goto(origin+'/flow?q=本地作品 0499&types=illust');page.locator('.image-page').first.wait_for()
            page.get_by_label('流式阅览顺序').select_option('random');page.wait_for_timeout(300)
            for step in range(44):
                page.get_by_role('button',name='下一件作品',exact=True).click();page.wait_for_timeout(100)
                if step%4==0:sample(f'random-next-{step}')
            for step in range(28):
                page.get_by_role('button',name='上一件作品',exact=True).click();page.wait_for_timeout(100)
                if step%4==0:sample(f'random-return-{step}')
            assert max(row['order_cache_max'] for row in result['samples'])<=3
            result['checks'].append('random-short-scope-rounds-and-return-with-three-round-cache-and120-window')
            for stage in ['foreground-idle','background-idle','foreground-task','background-task']:
                if stage=='foreground-task':
                    runtime.queue.handlers['verify']=ProbeTask();runtime.queue.submit('verify',{},'隔离负载验证');assert entered.wait(5)
                hidden=stage.startswith('background')
                page.evaluate("hidden=>{Object.defineProperty(document,'visibilityState',{configurable:true,get:()=>hidden?'hidden':'visible'});document.dispatchEvent(new Event('visibilitychange'))}",hidden)
                count=len(requests);page.wait_for_timeout(1500);state=sample(stage);state['requests_in_1500ms']=len(requests)-count
            gate.set();result['checks'].append('separate-foreground-background-idle-and-real-queue-running-metrics')
            heaps=[row['heap_bytes'] for row in result['samples'] if row['stage'].startswith('random-')]
            assert max(heaps)-min(heaps)<48*1024*1024
            assert result['samples'][-1]['heap_bytes']<128*1024*1024
            result['peak_active_requests']=peak[0];result['total_requests']=len(requests)
            result['post_gc_heap_range_bytes']=[min(heaps),max(heaps)]
            assert peak[0]<128 and not result['errors'],result['errors']
            page.screenshot(path=str(out/'final-random.png'));context.close();browser.close()
    except Exception:
        result['failures'].append(traceback.format_exc())
        if page:
            try:page.screenshot(path=str(out/'failure.png'))
            except Exception:pass
        raise
    finally:
        gate.set();server.should_exit=True;worker.join(20)
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        if worker.is_alive():raise RuntimeError('Isolated scale server did not stop')
    print(json.dumps({key:value for key,value in result.items() if key!='samples'},ensure_ascii=False))


if __name__=='__main__':main()
