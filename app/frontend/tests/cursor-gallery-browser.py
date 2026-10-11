"""Real source UI over one isolated synthetic Runtime and installed Chromium."""
import argparse
import json
import os
import shutil
from pathlib import Path
import threading
import time

parser = argparse.ArgumentParser()
parser.add_argument('--artifacts', required=True, type=Path)
parser.add_argument('--serve-only', action='store_true')
parser.add_argument('--chromium', default=shutil.which('chromium') or 'C:/Program Files/Google/Chrome/Application/chrome.exe')
parser.add_argument('--port', type=int, default=19273)
args = parser.parse_args()
args.artifacts.mkdir(parents=True, exist_ok=False)
args.artifacts = args.artifacts.resolve()
os.environ['DATA_ROOT'] = str(args.artifacts / 'default-isolation')
# Backend imports must use the app directory; never touch default user data.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from playwright.sync_api import expect, sync_playwright
import uvicorn
from src.core.models import Artist, Asset, Derivative, Work
from src.core.runtime import Runtime
from src.routes.archive_api import create_router

runtime = Runtime(args.artifacts / 'synthetic.db', args.artifacts / 'images', create=True)
Image.new('RGB', (160, 120), '#247d9d').save(runtime.images / 'synthetic.png')
with runtime.database.write() as session:
    session.add(Artist(id=1, pixiv_user_id='synthetic', name='Synthetic artist'))
    session.flush()
    for identity in range(1, 62):
        count = 200 if identity == 61 else 1
        work = Work(id=identity, pixiv_illust_id=str(identity), artist_id=1,
            title=f'Synthetic {identity}', page_count=count,
            posted_at=datetime(2026, 1, 1) + timedelta(days=identity))
        work.assets = [Asset(role='image', page_index=page, relative_path='synthetic.png',
            state='verified', width=160, height=120) for page in range(count)]
        session.add(Derivative(work_id=identity, kind='thumbnail', page_index=0, spec_key='v1',
            relative_path='synthetic.png', state='verified', width=160, height=120))
        session.add(work)

app = FastAPI()
@app.get('/api/startup')
def startup(): return {'state': 'ready'}
@app.get('/api/status')
def status(): return {'page_size': 50, 'pagination_mode': 'pages'}
@app.post('/__synthetic/mutate')
def mutate():
    with runtime.database.write() as session:
        session.get(Work, 59).title = 'Recovered synthetic 59'
    return {'ok': True}
app.include_router(create_router(lambda: runtime))
dist = Path(__file__).resolve().parents[1] / 'dist'
app.mount('/reader', StaticFiles(directory=dist), name='reader')
@app.get('/{path:path}')
def root(path: str): return FileResponse(dist / 'index.html')
server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, log_level='error'))
thread = threading.Thread(target=server.run, daemon=True); thread.start()
for _ in range(100):
    if server.started: break
    time.sleep(.05)
assert server.started
origin = f'http://127.0.0.1:{args.port}'
if args.serve_only:
    print(origin, flush=True)
    thread.join()
    assert runtime.close()
    raise SystemExit(0)
results = {'checks': [], 'errors': [], 'real_library_used': False, 'real_account_used': False, 'browser': args.chromium}
def expect_detail_title(page, title):
    # PageReader has a 150 ms leave transition. Wait for its old component to
    # leave the DOM before a strict assertion; preserve the expected new title.
    page.wait_for_function("expected => {const nodes = document.querySelectorAll('.detail-title-row h1'); return nodes.length === 1 && nodes[0].textContent === expected}", arg=title)
    expect(page.locator('.detail-title-row h1')).to_have_text(title)

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=args.chromium, args=['--no-sandbox'])
        try:
            for width in (1440, 390, 320):
                context = browser.new_context(viewport={'width': width, 'height': 900})
                page = context.new_page(); page.set_default_timeout(12000)
                page.on('pageerror', lambda error: results['errors'].append(str(error)))
                requests = []
                page.on('request', lambda request: requests.append(request.url))
                page.goto(origin)
                first = page.locator('.work-card strong').first
                expect(first).to_have_text('Synthetic 61')
                assert not any('/api/library/counts' in path or '/api/reading-sessions' in path for path in requests)
                results['checks'].append(f'{width}: cursor-first gallery without exact count or frozen manifest')
                page.locator('.card-open').first.click()
                expect_detail_title(page, 'Synthetic 61')
                page.get_by_role('button', name='下一件作品', exact=True).click()
                expect_detail_title(page, 'Synthetic 60')
                page.get_by_role('button', name='上一件作品', exact=True).click()
                expect_detail_title(page, 'Synthetic 61')
                page.get_by_role('button', name='下一件作品', exact=True).click()
                page.get_by_role('button', name='下一件作品', exact=True).click()
                expect_detail_title(page, 'Synthetic 59')
                results['checks'].append(f'{width}: adjacent reading both directions and repeated clicks')
                with runtime.database.write() as session:
                    session.get(Work, 59).title = f'Recovered {width}'
                page.get_by_role('button', name='下一件作品', exact=True).click()
                expect(page.locator('.reader-layout > .notice')).to_contain_text('图库已变化')
                page.get_by_role('button', name='重新定位后继续', exact=True).click()
                expect_detail_title(page, f'Recovered {width}')
                page.get_by_role('button', name='下一件作品', exact=True).click()
                expect_detail_title(page, 'Synthetic 58')
                results['checks'].append(f'{width}: expiry retains view and explicit identity recovery resumes')
                page.get_by_role('button', name='返回图库', exact=True).click()
                expect(first).to_have_text('Synthetic 61')
                number = page.get_by_role('spinbutton', name='图库页码')
                number.fill('2'); number.press('Enter'); number.blur()
                expect(first).to_have_text('Synthetic 11')
                assert any('/api/library?' in path and 'offset=50' in path for path in requests)
                results['checks'].append(f'{width}: explicit numeric page two remains available')
                page.goto(origin + '/flow')
                expect(page.locator('.stream-head h1')).to_have_text('流式阅览')
                page.wait_for_function("document.querySelector('.image-page')?.dataset.workId === '61'")
                before = len([path for path in requests if '/api/reading-sessions' in path])
                page.get_by_role('button', name='下一件作品', exact=True).click()
                page.wait_for_function("document.querySelector('.reading-work-info strong')?.textContent === 'Synthetic 60'")
                page.get_by_role('button', name='上一件作品', exact=True).click()
                page.wait_for_function("document.querySelector('.reading-work-info strong')?.textContent === 'Synthetic 61'")
                assert len([path for path in requests if '/api/reading-sessions' in path]) == before
                assert int(page.locator('.page-window').get_attribute('data-window-size')) <= 120
                results['checks'].append(f'{width}: sequential flow skips a 200-page work and returns without manifest; bounded page window')
                # Explicit random selection preserves the frozen read and round behavior.
                page.get_by_role('combobox', name='流式阅览顺序').select_option('random')
                page.wait_for_function("document.querySelector('.reading-order select')?.value === 'random'")
                assert len([path for path in requests if '/api/reading-sessions' in path]) > before
                results['checks'].append(f'{width}: explicit random mode creates its frozen scope')
                page.screenshot(path=str(args.artifacts / f'ui-{width}.png'))
                context.close()
                with runtime.database.write() as session:
                    session.get(Work, 59).title = 'Synthetic 59'
            assert not results['errors'], results['errors']
        finally: browser.close()
except Exception as exc:
    results['failure'] = str(exc)
    try: page.screenshot(path=str(args.artifacts / 'failure.png'))
    except Exception: pass
    raise
finally:
    (args.artifacts / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    server.should_exit = True; thread.join(10)
    assert runtime.close()
print(json.dumps(results, ensure_ascii=False, indent=2))
