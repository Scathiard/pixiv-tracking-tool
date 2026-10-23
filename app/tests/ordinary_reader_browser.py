"""Small ordinary-reader UI check; three synthetic works, no real account."""
import argparse
from collections import Counter
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

parser = argparse.ArgumentParser()
parser.add_argument('--dist', type=Path, required=True)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
out = args.out.resolve()
out.mkdir(parents=True, exist_ok=False)
os.environ.update(TEMP=str(out), TMP=str(out), DATA_ROOT=str(out / 'configuration'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stream_reader_browser import tiny_library
from src.core.models import Asset
from src.web import create_app
from src.auth import _find_system_browser
from sqlalchemy import select
from PIL import Image
from playwright.sync_api import sync_playwright, expect
import uvicorn

runtime = tiny_library(out / 'fixture')
with runtime.database.write() as session:
    asset = session.scalar(select(Asset).where(Asset.work_id == 2))
    picture = runtime.images / asset.relative_path
    with Image.new('RGB', (1280, 720), (65, 105, 135)) as image:
        image.save(picture)
    stat = picture.stat()
    asset.width, asset.height = 1280, 720
    asset.sha256 = hashlib.sha256(picture.read_bytes()).hexdigest()
    asset.byte_size, asset.file_mtime_ns = stat.st_size, stat.st_mtime_ns
with socket.socket() as probe:
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
origin = f'http://127.0.0.1:{port}'
server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False,
    schedule=False, reading_ui=False), host='127.0.0.1', port=port, log_level='error',
    loop='src.networking:http_loop_factory'))
worker = threading.Thread(target=server.run, daemon=True)
worker.start()
result = {'checks': [], 'metrics': {}, 'errors': [], 'real_library': False,
          'real_account': False, 'real_phone': False}


def route_request(route):
    path = urlsplit(route.request.url).path
    if path.startswith('/reader/assets/'):
        source = args.dist / path.removeprefix('/reader/')
        route.fulfill(body=source.read_bytes(), content_type=mimetypes.guess_type(source)[0] or 'application/octet-stream')
    elif route.request.is_navigation_request():
        route.fulfill(body=(args.dist / 'index.html').read_bytes(), content_type='text/html')
    else:
        route.continue_()


def geometry(page, work):
    image = page.locator(f'.reader .image-page[data-work-id="{work}"] img').first
    expect(image).to_be_attached()
    page.wait_for_function('(id) => {const i=document.querySelector(`.reader .image-page[data-work-id="${id}"] img`);return i?.complete && i.naturalWidth>0}', arg=work)
    page.wait_for_function('!document.querySelector(".work-enter-active,.work-leave-active")')
    metrics = image.evaluate('''i => {
        const b=i.closest('.page-picture').getBoundingClientRect(), r=i.getBoundingClientRect();
        return {width:r.width,height:r.height,frameWidth:b.width,frameHeight:b.height,
            naturalWidth:i.naturalWidth,naturalHeight:i.naturalHeight,
            scrollHeight:document.querySelector('.page-viewport').scrollHeight,
            viewportHeight:document.querySelector('.page-viewport').clientHeight};
    }''')
    assert abs(metrics['width'] - metrics['frameWidth']) < 2, metrics
    assert abs(metrics['height'] / metrics['width'] - metrics['naturalHeight'] / metrics['naturalWidth']) < .01, metrics
    assert abs(metrics['height'] - metrics['frameHeight']) < 2, metrics
    return metrics


try:
    deadline = time.monotonic() + 25
    while not server.started and time.monotonic() < deadline:
        time.sleep(.05)
    assert server.started, 'Synthetic server failed to start'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
        try:
            for width in (1440, 390, 320):
                context = browser.new_context(viewport={'width': width, 'height': 900},
                    is_mobile=width < 500, has_touch=width < 500, device_scale_factor=1)
                try:
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.route('**/*', route_request)
                    requests, completed = [], []
                    page.on('request', lambda request: requests.append(request.url))
                    page.on('requestfinished', lambda request: completed.append(request.url))
                    page.on('pageerror', lambda error: result['errors'].append(str(error)))
                    page.goto(origin + '/illust/2')
                    landscape = geometry(page, 2)
                    # Pump browser events until the two immediate neighbors are ready.
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        paths = Counter(urlsplit(url).path for url in completed)
                        if all(paths[f'/api/works/{identity}/cursor-neighbors'] and paths[f'/api/works/{identity}']
                               for identity in (1, 3)):
                            break
                        page.wait_for_timeout(50)
                    else:
                        raise AssertionError('Direct detail did not prepare both neighbors')
                    before = len(requests)
                    page.get_by_role('button', name='下一件作品', exact=True).click()
                    portrait = geometry(page, 1)
                    during = [url for url in requests[before:] if urlsplit(url).path in
                              ('/api/works/1', '/api/works/1/cursor-neighbors')]
                    assert not during, f'Cached multi-page opening reread on navigation: {during}'
                    assert portrait['scrollHeight'] > portrait['viewportHeight']
                    if width == 1440:
                        assert portrait['height'] > portrait['viewportHeight'], portrait
                        page.keyboard.press('ArrowDown')
                        page.wait_for_function('''() => {
                            const p=document.querySelector('.image-page[data-page="1"]');
                            const v=document.querySelector('.page-viewport');
                            return p && Math.abs(p.getBoundingClientRect().top-v.getBoundingClientRect().top)<3;
                        }''')
                        page.keyboard.press('ArrowUp')
                        page.wait_for_function('''() => {
                            const p=document.querySelector('.image-page[data-page="0"]');
                            const v=document.querySelector('.page-viewport');
                            return p && Math.abs(p.getBoundingClientRect().top-v.getBoundingClientRect().top)<3;
                        }''')
                    page.screenshot(path=str(out / f'portrait-{width}.png'))
                    page.get_by_role('button', name='上一件作品', exact=True).click()
                    geometry(page, 2)
                    assert not any(urlsplit(url).path == '/api/diagnostics/events' for url in requests)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate('({documentWidth:document.documentElement.scrollWidth,viewport:innerWidth})')
                    result['metrics'][str(width)] = {'landscape': landscape, 'portrait': portrait,
                        'cached_target_reads_on_switch': len(during)}
                    result['checks'].append(f'{width}: ratio, warm-navigation, back, diagnostic-off')
                    print('PASS', result['checks'][-1], flush=True)
                finally:
                    context.close()
        finally:
            browser.close()
    assert not result['errors'], result['errors']
except Exception as error:
    result['failure'] = str(error)
    raise
finally:
    server.should_exit = True
    worker.join(15)
    result['server_stopped'] = not worker.is_alive()
    (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
