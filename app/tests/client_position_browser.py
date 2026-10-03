"""Production navigation checks; all 520 work records are synthetic GET replays.

Uses a small isolated server or the EXE's three-work smoke library for media.
Never connects to a real account or library. Keeps all artifacts and old storage.
"""
from __future__ import annotations
import argparse, copy, hashlib, json, os, re, socket, subprocess, sys, threading, time, traceback
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--artifacts',required=True,type=Path)
    parser.add_argument('--exe',type=Path)
    parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--width',type=int)
    parser.add_argument('--mouse-navigation',action='store_true',help='Diagnose accepted clicks during a touch fling')
    parser.add_argument('--failure-only',action='store_true',help='Check retained cards and retry on a failed home request')
    args=parser.parse_args()
    out=args.artifacts.resolve(); out.mkdir(parents=True,exist_ok=False)
    os.environ['DATA_ROOT']=str(out/'fixture')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    result={'checks':[],'failures':[],'errors':[],'metrics':{},'baseline':args.baseline,
      'mode':'exe' if args.exe else 'source','real_library_used':False,'real_account_used':False,
      'real_phone_tested':False,'metadata':'520 work / 522 page synthetic browser API replay',
      'interaction':'real mouse wheel / CDP touch and actual navigation buttons'}
    server=runtime=process=log=page=None
    stop=out/'stop.signal'
    def read(path):
        with urlopen(origin+path,timeout=8) as r: return json.loads(r.read())
    def scenario(name,action):
        try:
            metric=action(); result['checks'].append(name)
            if metric is not None: result['metrics'][name]=metric
            print('PASS '+name,flush=True)
        except Exception as exc:
            state=page.evaluate("({url:location.href,gallery:document.querySelector('.gallery-viewport')?.scrollTop,reader:document.querySelector('.page-viewport')?.scrollTop,trace:window.__navigationTrace})") if page and not page.is_closed() else None
            result['failures'].append({'name':name,'error':str(exc),'state':state,'traceback':traceback.format_exc()})
            if page and not page.is_closed(): page.screenshot(path=str(out/(name+'.png')))
            print('FAIL '+name+': '+str(exc),flush=True)
    try:
        if args.exe:
            exe=args.exe.resolve(strict=True)
            result.update(exe=str(exe),sha256=hashlib.sha256(exe.read_bytes()).hexdigest())
            env=os.environ.copy();env['TEMP']=env['TMP']=str(out)
            log=(out/'exe-output.txt').open('wb')
            process=subprocess.Popen([str(exe),'--smoke-test','--smoke-output',str(out/'smoke.json'),
                '--smoke-serve-port',str(port),'--smoke-stop-file',str(stop)],cwd=out,env=env,
                stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        else:
            from client_fixture import make_library
            import uvicorn
            runtime,_=make_library(out/'fixture',works=3)
            from src.web import create_app
            server=uvicorn.Server(uvicorn.Config(create_app(lambda:runtime,authenticate=False,schedule=False,reading_ui=True),
                host='127.0.0.1',port=port,log_level='error',loop='src.networking:http_loop_factory'))
            threading.Thread(target=server.run,daemon=True).start()
        for _ in range(320):
            try: read('/api/status');break
            except OSError: time.sleep(.2)
        else: raise TimeoutError('Isolated service did not start')
        base=read('/api/library?limit=1&compact=1')['items'][0]
        detail=read(f"/api/works/{base['id']}?page_limit=1")
        result['build']=read('/api/settings').get('build')
        with urlopen(origin+'/',timeout=8) as r: html=r.read()
        result['frontend_assets']=re.findall(r'/reader/assets/[^"\s>]+',html.decode())
        page_source=detail['pages'][0]
        cards=[]; pages=[]
        def bookmark(identity):
            return {'work_id':identity,'operation_id':None,'status':'confirmed','desired':None,'value':False,
                'confirmed_at':'2026-10-03T03:00:00Z','error':None,'version':1,'cached':False,'verified':True,
                'fact_account_id':'90001','current_account_id':'90001','account_epoch':1}
        for index in range(520):
            identity=index+1001; count=3 if index==1 else 1
            cards.append({**copy.deepcopy(base),'id':identity,'pixiv_id':str(identity+100000),
                'title':f'位置测试 {identity}','page_count':count,'type':'manga' if count==3 else 'illust',
                'bookmark':bookmark(identity),'is_bookmarked':False,'preview':None})
            for part in range(count):
                pages.append({**copy.deepcopy(page_source),'index':len(pages),'page':part,'work_id':identity,
                    'work_index':index,'page_count':count,'title':f'位置测试 {identity}','artist_name':base['artist_name'],
                    'artist_id':base['artist_id'],'type':'manga' if count==3 else 'illust','bookmark':bookmark(identity)})
        from src.auth import _find_system_browser
        from playwright.sync_api import expect,sync_playwright
        seed=json.dumps({'all':{'updated':1,'gallery':{'work_id':1301,'index':300},
            'feed':{'work_id':1301,'work_index':300,'page':0,'index':302,'offset':180},
            'reader':{'work_id':1002,'work_index':1,'page':2,'index':2,'offset':200}}})
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,executable_path=_find_system_browser())
            for width in ((args.width,) if args.width else (1440,) if args.baseline else (1440,390,320)):
                context=browser.new_context(viewport={'width':width,'height':900},is_mobile=width<500,has_touch=width<500)
                page=context.new_page();page.set_default_timeout(5000)
                page.on('pageerror',lambda error:result['errors'].append(str(error)))
                page.add_init_script("localStorage.setItem('pixiv-local-positions-v1',"+json.dumps(seed)+");localStorage.setItem('pixiv-local-page-size','scroll');localStorage.setItem('pixiv-local-theme','dark')")
                page.add_init_script("""window.__navigationTrace=[];const originalScroll=Element.prototype.scrollTo;Element.prototype.scrollTo=function(...args){if(this.classList.contains('gallery-viewport'))window.__navigationTrace.push({at:performance.now(),kind:'scrollTo',args});return originalScroll.apply(this,args)};document.addEventListener('scroll',e=>{if(e.target.classList?.contains('gallery-viewport')){window.__navigationTrace.push({at:performance.now(),kind:'scroll',top:e.target.scrollTop});if(window.__navigationTrace.length>400)window.__navigationTrace.shift()}},true);document.addEventListener('click',e=>{const b=e.target.closest('button');if(b)window.__navigationTrace.push({at:performance.now(),kind:'click',name:b.getAttribute('aria-label')||b.textContent})},true)""")
                controls={'fail':False,'requests':[]}
                def route_api(route):
                    path=urlsplit(route.request.url).path;q=parse_qs(urlsplit(route.request.url).query)
                    offset=int(q.get('offset',['0'])[0]);limit=int(q.get('limit',['1'])[0])
                    if path=='/api/library':
                        if controls['fail']:
                            route.fulfill(status=503,content_type='application/json',body='{"detail":"synthetic unavailable"}');return
                        controls['requests'].append(offset)
                        route.fulfill(json={'items':cards[offset:offset+limit],'offset':offset,'limit':limit,'total':len(cards),
                            'ceiling':1520,'as_of':'2026-10-03T03:00:00Z','catalogue_revision':1});return
                    if path=='/api/library/locate':
                        work=int(q.get('work_id',['1001'])[0]);part=int(q.get('page',['0'])[0])
                        item=next(x for x in pages if x['work_id']==work and x['page']==part)
                        route.fulfill(json={k:item[k] for k in ('work_id','work_index','page','index')});return
                    if path=='/api/library/feed':
                        route.fulfill(json={'items':pages[offset:offset+limit],'offset':offset,'total':len(pages),'catalogue_revision':1});return
                    match=re.fullmatch(r'/api/works/(\d+)(/bookmark(?:/refresh)?)?',path)
                    if match:
                        identity=int(match[1])
                        if match[2]:route.fulfill(json=bookmark(identity));return
                        card=cards[identity-1001];start=int(q.get('page_offset',['0'])[0]);amount=int(q.get('page_limit',['48'])[0])
                        all_pages=[{**x,'index':x['page']} for x in pages if x['work_id']==identity]
                        route.fulfill(json={**copy.deepcopy(detail),**card,'display_page_count':len(all_pages),
                            'pages':all_pages[start:start+amount],'page_offset':start,'page_limit':amount});return
                    route.continue_()
                page.route('**/api/**',route_api)
                def gallery_top():
                    expect(page.locator('.gallery-viewport')).to_be_visible()
                    page.wait_for_function("document.querySelector('.gallery-viewport')?.scrollTop < 2 && document.querySelector('.gallery-window')?.dataset.start === '0' && !!document.querySelector('.work-card[data-work-id=\"1001\"]')",timeout=3500)
                    page.wait_for_timeout(250)
                    assert page.locator('.gallery-viewport').evaluate('el=>el.scrollTop')<2,'A late restore moved the gallery after navigation'
                    assert page.locator('.gallery-window').get_attribute('data-start')=='0'
                    return {'scrollTop':page.locator('.gallery-viewport').evaluate('el=>el.scrollTop'),'url':page.url}
                def fresh(path='/'):
                    page.goto(origin+path,wait_until='domcontentloaded')
                    expect(page.locator('.work-card,.image-page').first).to_be_visible()
                def settled(selector):
                    page.evaluate('window.__settled=null')
                    page.wait_for_function("selector=>{const e=document.querySelector(selector),now=performance.now(),top=e.scrollTop;const s=window.__settled;if(!s||Math.abs(s.top-top)>.5){window.__settled={top,at:now};return false}return now-s.at>250}",arg=selector)
                def wheel(selector,distance=2800,settle=True):
                    box=page.locator(selector).bounding_box();assert box
                    if width>=500:
                        page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2);page.mouse.wheel(0,distance)
                    else:
                        cdp=context.new_cdp_session(page);x=box['x']+box['width']/2;y=min(820,box['y']+box['height']-40)
                        cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
                        for step in range(1,9):
                            cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y-50*step}]});page.wait_for_timeout(20)
                        cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]});cdp.detach()
                    page.wait_for_timeout(400)
                    assert page.locator(selector).evaluate('el=>el.scrollTop')>200
                    if settle:settled(selector)
                def nav(name):
                    if width<500:
                        opener=page.get_by_role('button',name='打开菜单',exact=True)
                        if opener.count():opener.click() if args.mouse_navigation else opener.tap()
                    button=page.locator('[aria-label="主导航"]').get_by_role('button',name=name,exact=True)
                    button.tap() if width<500 and not args.mouse_navigation else button.click()
                    if width<500:expect(page.locator('.menu-toggle')).to_have_attribute('aria-expanded','false')
                    if name in ('设置','画师'):expect(page.locator('.settings-page' if name=='设置' else '.artists-page')).to_be_visible()
                def logo():
                    target=page.locator('.mobile-head .brand' if width<500 else '.sidebar-head .brand')
                    target.tap() if width<500 and not args.mouse_navigation else target.click()
                if not args.baseline:
                    def failed_home():
                        fresh();wheel('.gallery-viewport')
                        before=page.locator('.gallery-viewport').evaluate('el=>el.scrollTop')
                        controls['fail']=True
                        try:
                            logo()
                            notice=page.locator('.gallery-main > .notice.error')
                            expect(notice).to_be_visible(timeout=10000)
                            assert page.locator('.work-card').count()>0
                            assert abs(page.locator('.gallery-viewport').evaluate('el=>el.scrollTop')-before)<2
                        finally:controls['fail']=False
                        notice.get_by_role('button',name='重试',exact=True).click()
                        return gallery_top()
                    scenario(f'failed-home-retains-cards-and-retry-{width}',failed_home)
                if args.failure_only:context.close();continue
                def cold():fresh();return gallery_top()
                scenario(f'old-saved-position-ignored-{width}',cold)
                def home_action(action):
                    fresh();wheel('.gallery-viewport');action();return gallery_top()
                scenario(f'logo-at-home-goes-top-{width}',lambda:home_action(logo))
                scenario(f'works-at-home-goes-top-{width}',lambda:home_action(lambda:nav('作品')))
                def reload_deep():
                    fresh();wheel('.gallery-viewport');page.reload(wait_until='domcontentloaded');return gallery_top()
                scenario(f'reload-deep-gallery-{width}',reload_deep)
                if args.baseline:context.close();continue
                def fling_home():
                    fresh();wheel('.gallery-viewport',settle=False)
                    # An actual accepted pointer click while touch inertia is active.
                    # Native touch taps during a fling may only stop scrolling, with no click.
                    page.locator('.mobile-head .brand' if width<500 else '.sidebar-head .brand').click()
                    return gallery_top()
                scenario(f'accepted-logo-during-scroll-goes-top-{width}',fling_home)
                for section in ('设置','画师'):
                    def roundtrip(section=section):
                        fresh();wheel('.gallery-viewport');nav(section)
                        expect(page.locator('.settings-page' if section=='设置' else '.artists-page')).to_be_visible()
                        nav('作品');return gallery_top()
                    scenario(f'{section}-back-home-{width}',roundtrip)
                def reader():
                    fresh('/illust/1002');expect(page.locator('.page-viewport.reader')).to_be_visible()
                    page.wait_for_function("document.querySelector('.image-page[data-page=\"0\"]') && document.querySelector('.page-viewport').scrollTop < 2")
                    page.keyboard.press('ArrowDown');page.wait_for_timeout(400)
                    before=page.locator('.page-viewport').evaluate('el=>el.scrollTop');assert before>100
                    page.locator('.image-page[data-page="1"]').get_by_role('button',name='放大此页',exact=True).click()
                    expect(page.get_by_role('dialog',name='放大查看')).to_be_visible()
                    before=page.locator('.page-viewport').evaluate('el=>el.scrollTop')
                    page.get_by_role('button',name=re.compile('关闭放大')).click();page.wait_for_timeout(100)
                    assert abs(page.locator('.page-viewport').evaluate('el=>el.scrollTop')-before)<2
                    logo();return gallery_top()
                scenario(f'detail-starts-zero-zoom-preserves-home-top-{width}',reader)
                def history_navigation():
                    fresh();wheel('.gallery-viewport')
                    page.locator('.work-card').filter(visible=True).first.locator('.card-open').click()
                    expect(page.locator('.page-viewport.reader')).to_be_visible()
                    page.go_back();gallery_top()
                    page.go_forward();expect(page.locator('.page-viewport.reader')).to_be_visible()
                    page.wait_for_function("document.querySelector('.page-viewport').scrollTop < 2")
                    logo();return gallery_top()
                scenario(f'browser-back-forward-starts-top-{width}',history_navigation)
                def feed():
                    fresh('/flow');expect(page.locator('.page-viewport.feed')).to_be_visible()
                    page.wait_for_function("document.querySelector('.image-page[data-work-id=\"1001\"]') && document.querySelector('.page-viewport').scrollTop < 2")
                    wheel('.page-viewport');nav('设置');nav('流式阅览')
                    page.wait_for_function("document.querySelector('.image-page[data-work-id=\"1001\"]') && document.querySelector('.page-viewport').scrollTop < 2")
                    logo();return gallery_top()
                scenario(f'feed-reentry-starts-first-{width}',feed)
                def transient():
                    fresh();wheel('.gallery-viewport')
                    # This checks a settled view, not a deliberate touch fling continuing naturally.
                    settled('.gallery-viewport')
                    before=page.locator('.gallery-viewport').evaluate('el=>el.scrollTop')
                    page.evaluate("window.dispatchEvent(new Event('archive-updated'))");page.wait_for_timeout(400)
                    assert abs(page.locator('.gallery-viewport').evaluate('el=>el.scrollTop')-before)<2
                    saved=page.evaluate("localStorage.getItem('pixiv-local-positions-v1')")
                    assert saved==seed,'Old position storage was modified during reading'
                    return {'scrollTop':before,'old_storage_unchanged':True}
                scenario(f'background-refresh-stays-no-position-write-{width}',transient)
                def filter_home():
                    fresh('/?sort=oldest&artist_ids=1');wheel('.gallery-viewport');logo();gallery_top()
                    query=parse_qs(urlsplit(page.url).query)
                    assert query.get('sort')==['oldest'] and query.get('artist_ids')==['1']
                    return query
                scenario(f'home-preserves-filter-and-sort-{width}',filter_home)
                def explicit_page():
                    nav('设置');page.get_by_role('combobox',name='图库浏览方式').select_option('50')
                    # No reload: explicit page navigation through the real UI.
                    nav('作品');gallery_top()
                    control=page.get_by_role('spinbutton',name='图库页码')
                    control.fill('4');control.press('Tab');expect(control).to_have_value('4')
                    expect(page.locator('.work-card[data-work-id="1151"]')).to_be_visible()
                    logo();gallery_top();expect(control).to_have_value('1')
                    return {'manual_page':4,'home_page':1}
                scenario(f'explicit-pagination-and-logo-first-page-{width}',explicit_page)
                page.screenshot(path=str(out/f'home-{width}.png'))
                context.close()
            browser.close()
    finally:
        if process:
            stop.write_text('stop',encoding='utf-8')
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=10)
        if server:server.should_exit=True
        if runtime:runtime.close()
        if log:log.close()
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'checks':len(result['checks']),'failures':len(result['failures']),'errors':len(result['errors']),'artifacts':str(out)}))
    return 0 if args.baseline or not result['failures'] and not result['errors'] else 1


if __name__=='__main__':raise SystemExit(main())
