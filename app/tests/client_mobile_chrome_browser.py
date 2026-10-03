"""Mobile reader controls, compact task panel and Chrome viewport signal replay.

Only new synthetic fixtures/EXE smoke databases are used. Native safe-area
emulation and real touch input are separate from explicitly synthetic
VisualViewport getters/events; neither is an Android-device acceptance claim.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler


VIEWPORT_REPLAY = """(() => {
  const vv = window.visualViewport;
  const native = {};
  for (const key of ['height','width','offsetTop','offsetLeft','pageTop','pageLeft','scale']) {
    native[key] = Object.getOwnPropertyDescriptor(VisualViewport.prototype,key).get;
    Object.defineProperty(vv,key,{configurable:true,get(){
      return window.__mobileViewportReplay?.[key] ?? native[key].call(vv);
    }});
  }
  window.__replayVisualViewport = state => {
    window.__mobileViewportReplay = state;
    document.getElementById('synthetic-browser-occlusion')?.remove();
    if (state && state.scale===1 && state.offsetTop+state.height<innerHeight) {
      const cover=document.createElement('div');cover.id='synthetic-browser-occlusion';
      cover.textContent='模拟浏览器 / 键盘遮挡区';
      Object.assign(cover.style,{position:'fixed',left:'0',right:'0',
        top:`${state.offsetTop+state.height}px`,bottom:'0',zIndex:'2147483647',
        background:'#0e121bcc',color:'#fff',fontSize:'12px',padding:'8px',pointerEvents:'auto'});
      document.body.append(cover);
    }
    vv.dispatchEvent(new Event('resize')); vv.dispatchEvent(new Event('scroll'));
  };
})();"""

GEOMETRY = """safe => {
  const rect = e => {const r=e.getBoundingClientRect();return {x:r.x,y:r.y,
    width:r.width,height:r.height,top:r.top,bottom:r.bottom,left:r.left,right:r.right}};
  // Insets originate at the layout edge: clipping the visual viewport can
  // already exclude that same region, so intersect the two safe rectangles.
  const vv=visualViewport, top=vv.offsetTop, bottom=Math.min(top+vv.height,innerHeight-safe);
  const toolbar=document.querySelector('.reading-toolbar');
  const task=document.querySelector('.task-status');
  const control=document.querySelector('.task-control');
  const hit = e => {
    const r=rect(e), visibleTop=Math.max(top,r.top),visibleBottom=Math.min(bottom,r.bottom);
    const points=[[r.left+2,r.top+2],[r.right-2,r.top+2],
      [r.left+2,r.bottom-2],[r.right-2,r.bottom-2],[(r.left+r.right)/2,(r.top+r.bottom)/2]];
    return {...r, name:e.getAttribute('aria-label') || e.textContent.trim(),
      disabled:e.disabled,visibleHeight:Math.max(0,visibleBottom-visibleTop),
      fullHit:points.every(([x,y])=>y>=top&&y<=bottom&&e.contains(document.elementFromPoint(x,y)))};
  };
  const overlap=(a,b)=>Math.max(0,Math.min(a.right,b.right)-Math.max(a.left,b.left))*
    Math.max(0,Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top));
  const taskRect=task&&rect(task);
  const pictures=[...document.querySelectorAll('.page-picture,.gallery-card-image')].map(rect);
  const imageOverlap=taskRect?pictures.reduce((sum,r)=>sum+overlap(taskRect,r),0):0;
  const phase=task?.querySelector('.task-phase');
  const range=phase&&document.createRange();if(range)range.selectNodeContents(phase);
  const phaseTextRect=range&&rect({getBoundingClientRect:()=>range.getBoundingClientRect()});
  return {layout:{width:innerWidth,height:innerHeight},visual:{width:vv.width,height:vv.height,
    offsetTop:top,scale:vv.scale},safeBottom:safe,visibleBottom:bottom,
    shell:rect(document.querySelector('.app-shell')),toolbar:toolbar&&rect(toolbar),
    toolbarSafeGap:toolbar?bottom-rect(toolbar).bottom:null,
    buttons:toolbar?[...toolbar.querySelectorAll('button')].map(hit):[],
    task:taskRect,taskArea:taskRect?taskRect.width*taskRect.height:0,imageOverlap,
    taskControl:control&&{...hit(control),background:getComputedStyle(control).backgroundColor},
    phase:phase&&{text:phase.textContent,rect:rect(phase),textRect:phaseTextRect},
    secondary:task?.querySelector('.task-secondary')?.textContent,
    progress:task?.querySelector('[role=progressbar]')?.getAttribute('aria-valuenow'),
    horizontalOverflow:document.documentElement.scrollWidth>innerWidth};
}"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--exe', type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ['DATA_ROOT'] = str(artifacts / 'fixture')
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    result = {'checks': [], 'failures': [], 'errors': [], 'metrics': {},
        'mode': 'exe' if args.exe else 'source', 'real_account_used': False,
        'real_library_used': False, 'real_phone_tested': False,
        'viewport_replay': 'Synthetic VisualViewport getters and resize/scroll events; native layout unchanged',
        'references': ['https://developer.chrome.com/docs/css-ui/edge-to-edge',
            'https://developer.chrome.com/blog/viewport-resize-behavior'],
        'scope': 'Native Chromium safe-area emulation; CDP touch; synthetic task/bookmark responses'}
    result['isolated_origin']=origin
    server = thread = process = output = browser = None
    stop = artifacts / 'stop.signal'
    started = time.monotonic()
    local_http=build_opener(ProxyHandler({}))

    def fetch(path):
        with local_http.open(origin + path, timeout=5) as response:
            return response.read()

    def check(label, condition, detail):
        (result['checks'] if condition else result['failures']).append(
            label if condition else {'name': label, 'detail': detail})
        if not condition:
            print('FAIL ' + label, flush=True)

    try:
        if args.exe:
            exe = args.exe.resolve(strict=True)
            result.update(exe=str(exe), exe_sha256=hashlib.sha256(exe.read_bytes()).hexdigest())
            output = (artifacts / 'exe-output.txt').open('xb')
            env = os.environ.copy()
            env['TEMP'] = env['TMP'] = str(artifacts)
            process = subprocess.Popen([str(exe), '--smoke-test', '--smoke-output',
                str(artifacts / 'exe-smoke.json'), '--smoke-serve-port', str(port),
                '--smoke-stop-file', str(stop)], cwd=artifacts, env=env,
                stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result['actual_database_works'] = 3
        else:
            from client_fixture import make_library
            runtime, remote = make_library(artifacts / 'fixture', works=20)
            from src.web import create_app
            import uvicorn
            server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime,
                authenticate=False, schedule=False, reading_ui=True), host='127.0.0.1',
                port=port, log_level='error', loop='src.networking:http_loop_factory'))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            result['actual_database_works'] = 20
        deadline = time.monotonic() + 300
        last_probe_error=None
        next_probe_report=time.monotonic()+10
        while time.monotonic() < deadline:
            if process and process.poll() is not None:
                raise RuntimeError(f'isolated EXE exited: {process.returncode}')
            try:
                json.loads(fetch('/api/status'))
                result['service_ready_elapsed_seconds']=time.monotonic()-started
                break
            except OSError as error:
                last_probe_error=str(error)
                if time.monotonic()>=next_probe_report:
                    elapsed=round(time.monotonic()-started,1)
                    result.setdefault('startup_probes',[]).append({'elapsed_seconds':elapsed,
                        'error':last_probe_error})
                    print(f'WAIT isolated EXE/service preparation {elapsed}s: {last_probe_error}',flush=True)
                    next_probe_report=time.monotonic()+10
                time.sleep(.15)
        else:
            result['startup_probe_error']=last_probe_error
            raise TimeoutError('isolated service did not start')
        html = fetch('/')
        original_bookmark=json.loads(fetch('/api/works/1?page_limit=1'))['bookmark']
        mock_epoch=original_bookmark['account_epoch']+1
        mock_account='synthetic-mobile-account'
        result['bookmark_response_replay']={'account_id':mock_account,'account_epoch':mock_epoch,
            'scope':'Browser responses only; no real Pixiv connection or write'}
        assets = re.findall(r'/reader/assets/[^"\']+', html.decode('utf-8'))
        result['served_frontend_sha256'] = {'index.html': hashlib.sha256(html).hexdigest(),
            **{asset: hashlib.sha256(fetch(asset)).hexdigest() for asset in assets}}
        assert assets, 'production frontend assets missing'
        from src.auth import _find_system_browser
        from playwright.sync_api import expect, sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            result['browser_version'] = browser.version
            for width, theme in ((390,'dark'),(360,'dark'),(320,'dark'),(390,'light')):
                context = browser.new_context(viewport={'width':width,'height':844},
                    is_mobile=True, has_touch=True)
                page = context.new_page()
                page.set_default_timeout(9000)
                page.add_init_script(VIEWPORT_REPLAY)
                page.add_init_script("localStorage.setItem('pixiv-local-theme',"+json.dumps(theme)+")")
                page.on('pageerror', lambda error: result['errors'].append(str(error)))
                cdp = context.new_cdp_session(page)
                state = {'paused': False, 'idle': False, 'parallel':False,
                         'unknown':False,'stale':False,'posts': []}

                def snapshot():
                    if state['idle']:
                        return {'round':None,'operations':[],'automatic_paused':False}
                    op = {'id':'synthetic-mobile-task','label':'检查并补齐图库','kind':'reconcile',
                        'state':'paused' if state['paused'] else 'running','source':'manual',
                        'pause_requested':state['paused'],'control':{'can_pause':not state['paused'],
                        'can_resume':state['paused']},'phase':{'name':'verifying','done':8720,
                        'total':None if state['unknown'] else 193215,'unit':'files','detail':'synthetic'},'result':{}}
                    others = [
                        {**deepcopy(op),'id':'synthetic-other','label':'检查新作品',
                         'state':'queued','control':{},'pause_requested':False}] if state['parallel'] else []
                    return {'round':{'id':'synthetic-round','state':'running'},'operations':[op,*others],
                         'automatic_paused':False,'active_operation_count':len(others)+1}

                def route_api(route):
                    path = urlsplit(route.request.url).path
                    if path == '/api/progress':
                        if state['stale']:
                            route.abort('failed')
                        else:
                            route.fulfill(json=snapshot())
                    elif path.startswith('/api/tasks/synthetic-mobile-task/'):
                        state['posts'].append(path)
                        state['paused'] = path.endswith('/pause')
                        route.fulfill(json={'operation_id':'synthetic-mobile-task','ok':True})
                    elif re.fullmatch(r'/api/works/\d+/bookmark(?:/refresh)?',path):
                        state['posts'].append(path+':'+route.request.method)
                        route.fulfill(json=bookmark_fact(int(path.split('/')[3])))
                    elif re.fullmatch(r'/api/works/\d+',path):
                        response=route.fetch()
                        data=response.json()
                        data['bookmark']=bookmark_fact(int(path.split('/')[3]))
                        route.fulfill(response=response,json=data)
                    elif re.fullmatch(r'/api/reading-sessions/[^/]+/feed',path):
                        response=route.fetch()
                        data=response.json()
                        for item in data['items']:
                            item['bookmark']=bookmark_fact(item['work_id'])
                        route.fulfill(response=response,json=data)
                    else:
                        route.continue_()

                def bookmark_fact(work_id):
                    return {'work_id':work_id,'status':'confirmed','value':False,'version':100,
                        'account_epoch':mock_epoch,'verified':True,'current_account_id':mock_account,
                        'fact_account_id':mock_account,'error':None,'cached':False,'operation_id':None,
                        'desired':None,'confirmed_at':'2026-10-03T00:00:00Z'}
                page.route('**/api/**',route_api)

                def tap(locator):
                    box=locator.bounding_box()
                    assert box
                    point={'x':box['x']+box['width']/2,'y':box['y']+box['height']/2}
                    cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[point]})
                    cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]})
                    page.wait_for_timeout(200)

                def measure(label,safe=0,compact=True):
                    page.wait_for_timeout(180)
                    metric=page.evaluate(GEOMETRY,safe)
                    result['metrics'][label]=metric
                    page.screenshot(path=str(artifacts/(label+'.png')))
                    if compact and metric['task']:
                        check(label+'-compact-task',metric['task']['height']<=52,metric['task'])
                        control=metric['taskControl']
                        check(label+'-pause-touch-target',bool(control and control['width']>=44 and
                            control['height']>=44 and control['fullHit']),control)
                        if not state['paused'] and not state['unknown'] and not state['stale']:
                            phase=metric['phase']
                            check(label+'-real-count-visible',bool(phase and '8720' in phase['text'] and
                                '193215' in phase['text'] and phase['textRect']['width']<=phase['rect']['width']+.5),phase)
                    if metric['toolbar']:
                        check(label+'-toolbar-visible',metric['toolbarSafeGap']>=-.5,metric['toolbarSafeGap'])
                        check(label+'-toolbar-no-double-inset',metric['toolbarSafeGap']<=18,metric['toolbarSafeGap'])
                        for button in metric['buttons']:
                            # Fractional typography makes the secondary info button 43.9375px.
                            minimum=43.9 if button['name']=='作品信息与整理' else 44
                            check(label+'-target-'+button['name'],button['width']>=minimum and
                                button['height']>=minimum and button['fullHit'],button)
                    check(label+'-no-horizontal-overflow',not metric['horizontalOverflow'],metric)
                    (artifacts/'checkpoint.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                    print('MEASURE '+label,flush=True)

                label=f'{width}-{theme}'
                page.goto(origin+'/flow',wait_until='domcontentloaded')
                page.locator('.image-page img').first.wait_for()
                page.wait_for_function("document.querySelector('.image-page img')?.naturalWidth>0")
                page.locator('.task-control').wait_for()
                measure(label+'-flow')
                page.locator('.page-viewport').evaluate('element=>element.scrollTop=130')
                measure(label+'-image-overlap')
                state['parallel']=True
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_function("document.querySelector('.task-other-count')?.textContent?.includes('1') || document.querySelector('.task-secondary')?.textContent?.includes('1')")
                measure(label+'-parallel')
                tap(page.locator('.task-control'))
                expect(page.get_by_role('button',name='继续',exact=True)).to_have_count(1)
                check(label+'-actual-pause-post','/api/tasks/synthetic-mobile-task/pause' in state['posts'],state['posts'])
                measure(label+'-paused')
                tap(page.locator('.task-control'))
                expect(page.get_by_role('button',name='暂停',exact=True)).to_have_count(1)
                check(label+'-actual-resume-post','/api/tasks/synthetic-mobile-task/resume' in state['posts'],state['posts'])
                tap(page.get_by_role('button',name='下一件作品',exact=True))
                expect(page.get_by_role('button',name='上一件作品',exact=True)).to_be_enabled()
                measure(label+'-after-next')
                print('TOUCH '+label+' previous',flush=True)
                tap(page.get_by_role('button',name='上一件作品',exact=True))
                expect(page.get_by_role('button',name='上一件作品',exact=True)).to_be_disabled()
                heart=page.locator('.reading-toolbar .bookmark-button')
                print('TOUCH '+label+' bookmark',flush=True)
                tap(heart)
                expect(heart).to_have_class(re.compile(r'\backnowledging\b'))
                check(label+'-actual-heart-post',any(p.endswith('/bookmark:POST') for p in state['posts']),state['posts'])
                for safe in (24,48,0):
                    cdp.send('Emulation.setSafeAreaInsetsOverride',{'insets':{'top':0,'left':0,'right':0,'bottom':safe}})
                    measure(label+f'-native-safe-{safe}',safe)
                for name,height,offset in (('address-shown',780,0),('address-hidden',844,0),
                                           ('keyboard',430,20),('restored',844,0)):
                    page.evaluate('state=>window.__replayVisualViewport(state)',
                        {'width':width,'height':height,'offsetTop':offset,'offsetLeft':0,
                         'pageTop':offset,'pageLeft':0,'scale':1})
                    measure(label+'-replay-'+name)
                for safe in (24,48):
                    cdp.send('Emulation.setSafeAreaInsetsOverride',{'insets':{'top':0,'left':0,'right':0,'bottom':safe}})
                    page.evaluate('state=>window.__replayVisualViewport(state)',
                        {'width':width,'height':780,'offsetTop':0,'offsetLeft':0,
                         'pageTop':0,'pageLeft':0,'scale':1})
                    measure(label+f'-replay-address-safe-{safe}',safe)
                cdp.send('Emulation.setSafeAreaInsetsOverride',{'insets':{'top':0,'left':0,'right':0,'bottom':0}})
                page.evaluate('window.__replayVisualViewport(null)')
                page.wait_for_function("Math.abs(document.querySelector('.app-shell').getBoundingClientRect().height-innerHeight)<.5")
                shell_before=page.locator('.app-shell').bounding_box()
                page.evaluate('state=>window.__replayVisualViewport(state)',
                    {'width':width/2,'height':422,'offsetTop':80,'offsetLeft':0,
                     'pageTop':80,'pageLeft':0,'scale':2})
                page.wait_for_timeout(180)
                shell_zoom=page.locator('.app-shell').bounding_box()
                result['metrics'][label+'-pinch-scale-2']={'before':shell_before,'after':shell_zoom}
                check(label+'-pinch-keeps-layout',abs(shell_before['height']-shell_zoom['height'])<1,
                    {'before':shell_before,'after':shell_zoom})
                page.evaluate('window.__replayVisualViewport(null)')
                page.set_viewport_size({'width':740,'height':360})
                measure(label+'-landscape')
                page.set_viewport_size({'width':width,'height':844})
                measure(label+'-portrait-return')
                page.goto(origin+'/illust/1',wait_until='domcontentloaded')
                page.locator('.page-viewport.reader .image-page').first.wait_for()
                measure(label+'-detail')
                if (width,theme)==(390,'dark'):
                    state['unknown']=True
                    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                    expect(page.locator('.task-phase')).to_contain_text('已处理 8720')
                    expect(page.locator('.task-track')).to_have_count(0)
                    measure(label+'-unknown-total')
                    check(label+'-unknown-no-false-percent',not page.locator('.task-status').get_by_role('progressbar').count(),page.locator('.task-status').inner_text())
                    state['stale']=True
                    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                    expect(page.locator('.task-status')).to_have_class(re.compile(r'\bstale\b'))
                    expect(page.locator('.task-track')).to_have_count(0)
                    expect(page.locator('.task-control')).to_be_disabled()
                    measure(label+'-disconnected',compact=False)
                    check(label+'-disconnected-no-false-track',not page.locator('.task-status').get_by_role('progressbar').count(),page.locator('.task-status').inner_text())
                    state.update(stale=False,unknown=False,idle=True)
                    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                    expect(page.locator('.task-status')).to_have_count(0)
                    measure(label+'-idle')
                    check(label+'-idle-panel-hidden',not page.locator('.task-status').count(),None)
                context.close()
            context=browser.new_context(viewport={'width':1440,'height':900})
            page=context.new_page()
            page.set_default_timeout(9000)
            page.on('pageerror',lambda error:result['errors'].append(str(error)))
            page.add_init_script("localStorage.setItem('pixiv-local-theme','dark')")
            state={'paused':False,'idle':False,'parallel':True,'unknown':False,'stale':False,'posts':[]}
            page.route('**/api/**',route_api)
            for route_name,path in (('flow','/flow'),('detail','/illust/1')):
                state['idle']=False
                page.goto(origin+path,wait_until='domcontentloaded')
                page.locator('.image-page').first.wait_for()
                page.locator('.task-status').wait_for()
                measure('1440-dark-'+route_name+'-task',compact=False)
                state['idle']=True
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                expect(page.locator('.task-status')).to_have_count(0)
                measure('1440-dark-'+route_name+'-idle',compact=False)
            context.close()
            browser.close()
            browser=None
    except Exception as error:
        result['failures'].append({'name':'harness','error':str(error),'traceback':traceback.format_exc()})
        print('HARNESS FAILURE '+str(error),flush=True)
    finally:
        if browser:
            try:
                browser.close()
            except Exception as error:
                # Exiting sync_playwright already closes its browsers after an error.
                result['browser_cleanup_note']=str(error)
        if process:
            stop.write_text('stop',encoding='utf-8')
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate();process.wait(timeout=10)
            result['exe_exit_code']=process.returncode
            if output:
                output.close()
        if server:
            server.should_exit=True
            thread.join(timeout=20)
        result['elapsed_seconds']=time.monotonic()-started
        (artifacts/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':len(result['checks']),'failed':len(result['failures']),
        'artifacts':str(artifacts)},ensure_ascii=False),flush=True)
    assert not result['failures'] and not result['errors'], 'See preserved result.json for mobile regression failures'


if __name__ == '__main__':
    main()
