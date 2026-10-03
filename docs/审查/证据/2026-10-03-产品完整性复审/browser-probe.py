"""Read-only production-EXE UX probes with synthetic browser responses only."""
from pathlib import Path
import copy, hashlib, json, os, socket, subprocess, sys, time
from urllib.request import urlopen
from urllib.parse import parse_qs, urlsplit

ROOT = Path(r'D:\claude_sandbox\pixiv-tracking-tool')
OUT = Path(__file__).parent / time.strftime('%Y%m%d-%H%M%S')
OUT.mkdir()
os.environ['DATA_ROOT'] = str(OUT / 'fixture')
sys.path.insert(0, str(ROOT / 'app'))
from src.auth import _find_system_browser
from playwright.sync_api import sync_playwright, expect

EXE = ROOT / 'release/v0.2.0/build-20261003.2/PixivTracker_v0.2.0_build20261003.2.exe'
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
origin = f'http://127.0.0.1:{port}'
stop = OUT / 'stop.signal'
result = {'build': '20261003.2', 'sha256': hashlib.sha256(EXE.read_bytes()).hexdigest(),
          'real_library_used': False, 'real_account_used': False,
          'layer': 'Packaged production UI with synthetic GET responses',
          'sizes': [], 'settings': [], 'errors': [], 'failures': []}
def read(path):
    with urlopen(origin + path, timeout=10) as response:
        return json.loads(response.read())
def check(condition, message, data=None):
    if not condition:
        result['failures'].append({'message': message, 'data': data})
env = os.environ.copy()
env['TEMP'] = env['TMP'] = str(OUT)
log = (OUT / 'process.txt').open('wb')
process = subprocess.Popen([str(EXE), '--smoke-test', '--smoke-output', str(OUT/'smoke.json'),
    '--smoke-serve-port', str(port), '--smoke-stop-file', str(stop)], cwd=OUT, env=env,
    stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
try:
    for _ in range(320):
        if process.poll() is not None:
            raise RuntimeError('Owned EXE exited before startup')
        try:
            read('/api/status')
            break
        except OSError:
            time.sleep(.2)
    else:
        raise TimeoutError('Owned smoke EXE startup timeout')
    work = read('/api/library?limit=1&compact=1')['items'][0]
    artist = read('/api/artists?limit=1')['items'][0]
    artist = copy.deepcopy(artist)
    artist.update(name='隔离画师', biography='用于比较布局的本地资料。', work_count=120)
    artist['recent_works'] = [{**copy.deepcopy(work), 'id': i+1000, 'title': f'近期作品 {i+1}'} for i in range(12)]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        page = context.new_page()
        page.set_default_timeout(8000)
        page.on('pageerror', lambda error: result['errors'].append(str(error)))
        page.add_init_script("localStorage.setItem('pixiv-local-artist-layout','grid');localStorage.setItem('pixiv-local-sidebar-collapsed','0')")
        def artists(route):
            if route.request.method != 'GET':
                route.abort(); return
            if urlsplit(route.request.url).path == '/api/artists':
                data = {'items': [artist, {**artist, 'id': artist['id']+1, 'name': '第二位隔离画师'}], 'total':2, 'offset':0,'limit':50}
            else:
                data = artist
            route.fulfill(json=data)
        page.route('**/api/artists**', artists)
        for width in (320,390,680,768,800,900,1024,1100,1280,1366,1440,1536,1920):
            page.set_viewport_size({'width':width,'height':900})
            page.goto(origin+'/artists', wait_until='domcontentloaded')
            for collapsed in ((False,True) if width>768 else (False,)):
                if collapsed:
                    toggle = page.get_by_role('button', name='收起侧栏', exact=True)
                    if toggle.is_visible():
                        toggle.click()
                sizes = {}
                for mode, label in (('grid','网格'),('row','列表')):
                    page.locator('.artist-layout-switch').get_by_role('button',name=label,exact=True).click()
                    expect(page.locator('.artists-list.'+mode)).to_be_visible()
                    page.wait_for_timeout(90)
                    sizes[mode] = page.locator('.artist-profile').first.evaluate('''el => {
                      const rect = selector => {const r=el.querySelector(selector).getBoundingClientRect();return {width:r.width,height:r.height}};
                      return {card:el.getBoundingClientRect().width, image:rect('.artist-work-image'),
                        media:rect('.media-image'), img:el.querySelector('img:not(.artist-avatar)')?rect('img:not(.artist-avatar)'):null,
                        gap:parseFloat(getComputedStyle(el.querySelector('.artist-recent-strip')).columnGap),
                        strip:rect('.artist-recent-strip')};
                    }''')
                    if width in (390,1024,1440) and not collapsed:
                        page.screenshot(path=str(OUT/f'artists-{width}-{mode}.png'))
                result['sizes'].append({'width':width,'collapsed':collapsed,**sizes})
                equal = all(abs(sizes['grid']['image'][key]-sizes['row']['image'][key])<1 for key in ('width','height'))
                check(equal,'Recent-work preview changes size between modes',result['sizes'][-1])
                for mode in ('grid','row'):
                    sizes[mode]['fully_visible'] = int((sizes[mode]['strip']['width'] + sizes[mode]['gap'] + .5) // (sizes[mode]['image']['width'] + sizes[mode]['gap']))
                check(sizes['grid']['fully_visible'] >= 3,'Grid no longer fits three complete recent works',result['sizes'][-1])
        source_count = [0]
        def sources(route):
            q=parse_qs(urlsplit(route.request.url).query)
            offset,limit=int(q.get('offset',['0'])[0]),int(q.get('limit',['50'])[0])
            rows=[{'kind':'work','id':i+1,'pixiv_id':str(i+1000),'title':f'隔离来源作品 {i+1}',
                   'artist_name':'隔离画师','availability':{'state':'unavailable','confirmed':False,'confirmation':'unconfirmed',
                   'reason':'主站暂时无法获取，尚不能确认删除；已有存档保留','checked_at':'2026-10-03T09:00:00Z'},
                   'local':{'state':'complete','missing':[]}} for i in range(source_count[0])]
            route.fulfill(json={'items':rows[offset:offset+limit],'total':len(rows),'offset':offset,'limit':limit,
                 'counts':{'works':{'deleted':0,'unavailable':len(rows)},'artists':{'deleted':0,'unavailable':0}}})
        page.route('**/api/library/source-status**',sources)
        for width in (1440,390):
            page.set_viewport_size({'width':width,'height':900})
            for count in (0,50):
                source_count[0]=count
                page.goto(origin+'/settings',wait_until='domcontentloaded')
                expect(page.locator('.settings-page h3').filter(has_text='自动检查')).to_be_visible()
                expect(page.locator('.source-status-item')).to_have_count(count)
                metrics=page.locator('.settings-page').evaluate('''el=>({
                  height:el.clientHeight,scrollHeight:el.scrollHeight,
                  portY:[...el.querySelectorAll('h3')].find(x=>x.textContent==='访问端口').getBoundingClientRect().top,
                  fields:[...el.querySelectorAll('input,select')].map(x=>({label:x.getAttribute('aria-label'),type:x.type})),
                  overviewHeight:el.querySelector('.source-overview').getBoundingClientRect().height
                })''')
                metrics.update(width=width,source_count=count)
                result['settings'].append(metrics)
                interval_fields=[x for x in metrics['fields'] if any(word in (x['label'] or '') for word in ('间隔','频率','分钟','小时'))]
                check(bool(interval_fields),'Automatic interval has no editable control',{'width':width})
                if count:
                    baseline=result['settings'][-2]
                    check(metrics['portY']-baseline['portY']<metrics['height'],'Exception list pushes normal settings down by multiple screens',metrics)
                page.screenshot(path=str(OUT/f'settings-{width}-{count}.png'))
        browser.close()
finally:
    stop.write_text('stop',encoding='utf-8')
    try: process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.terminate();process.wait(timeout=10)
    log.close()
    (OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(OUT),'sizes_checked':len(result['sizes']),'settings':result['settings'],
                      'failures':result['failures'],'errors':result['errors']},ensure_ascii=False),flush=True)
