from pathlib import Path
import hashlib,json,os,socket,subprocess,time
from urllib.request import urlopen
from playwright.sync_api import sync_playwright,expect
base=Path(__file__).resolve().parent
out=base/'packaged-browser';out.mkdir(exist_ok=False)
exe=Path(r'D:\claude_sandbox\pixiv-tracking-tool\release\v0.2.0\build-20261020.3\PixivTracker_v0.2.0_build20261020.3.exe')
with socket.socket() as sock: sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
origin=f'http://127.0.0.1:{port}';stop=out/'stop.signal'
os.environ.update(TEMP=str(out),TMP=str(out))
result={'checks':[],'errors':[],'real_account_used':False,'real_library_used':False,'real_phone_tested':False,'api_replay':'First feed response changes only x_restrict to verify retained badge; all other fields and requests use actual packaged backend','exe_sha256':hashlib.sha256(exe.read_bytes()).hexdigest()}
with (out/'output.txt').open('wb') as output:
    process=subprocess.Popen([str(exe),'--smoke-test','--smoke-output',str(out/'smoke.json'),'--smoke-serve-port',str(port),'--smoke-stop-file',str(stop)],cwd=out,stdout=output,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        for _ in range(180):
            if process.poll() is not None: raise RuntimeError('EXE exited before ready')
            try:
                with urlopen(origin+'/api/status',timeout=1) as response: assert response.status==200
                break
            except OSError: time.sleep(.5)
        else: raise TimeoutError('EXE not ready')
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe')
            context=browser.new_context(viewport={'width':1440,'height':900})
            page=context.new_page();page.set_default_timeout(10000)
            page.on('pageerror',lambda error:result['errors'].append(str(error)))
            page.goto(origin,wait_until='domcontentloaded');page.locator('.work-card').first.wait_for();result['checks'].append('packaged-gallery-actual-api')
            def rating_route(route):
                response=route.fetch(); data=response.json()
                for item in data['items']: item['x_restrict']=1
                route.fulfill(response=response,json=data)
            page.route('**/api/library/cursor/feed*',rating_route)
            page.goto(origin+'/flow?sort=oldest',wait_until='domcontentloaded')
            page.wait_for_function("document.querySelector('.stream-viewport img')?.naturalWidth > 0")
            expect(page.locator('.stream-rating')).to_have_text('R-18')
            result['checks'].append('packaged-retains-restriction-badge')
            page.unroute('**/api/library/cursor/feed*',rating_route)
            first=page.locator('.stream-reader').get_attribute('data-stream-work')
            page.get_by_role('button',name='下一件作品',exact=True).click()
            page.wait_for_function("old=>document.querySelector('.stream-reader')?.dataset.streamWork !== old",arg=first)
            result['checks'].append('packaged-stream-actual-cursor-next')
            page.locator('.stream-viewport img').first.dblclick()
            expect(page.locator('.inline-zoom-media').first).to_have_attribute('data-inline-zoom-active','true')
            assert page.locator('.zoom-overlay').count()==0
            result['checks'].append('packaged-inline-zoom')
            page.get_by_role('button',name='展开作品简介',exact=True).click()
            expect(page.get_by_role('dialog',name='作品简介',exact=True)).to_be_visible()
            page.get_by_role('button',name='关闭作品简介',exact=True).click()
            result['checks'].append('packaged-local-caption')
            page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(300)
            page.screenshot(path=str(out/'packaged-stream-390.png'))
            page.set_viewport_size({'width':320,'height':844});page.wait_for_timeout(250)
            geometry=page.evaluate("""()=>({width:innerWidth,height:innerHeight,overflow:document.documentElement.scrollWidth>innerWidth,dock:(()=>{const r=document.querySelector('.stream-dock').getBoundingClientRect();return{x:r.x,right:r.right,bottom:r.bottom}})()})""")
            assert not geometry['overflow'] and geometry['dock']['bottom']<=geometry['height']+1 and geometry['dock']['right']<=geometry['width']+1,geometry
            page.screenshot(path=str(out/'packaged-stream-320.png'))
            result['checks'].append('packaged-320-geometry');result['geometry']=geometry
            browser.close()
    finally:
        stop.write_text('stop',encoding='utf-8')
        process.wait(timeout=25)
        result['exit_code']=process.returncode
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
assert process.returncode==0 and not result['errors'],result
assert json.loads((out/'smoke.json').read_text(encoding='utf-8'))['status']=='SMOKE_OK'
print(json.dumps(result,ensure_ascii=False))
