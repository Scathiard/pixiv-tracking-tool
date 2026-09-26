"""Actual Chromium reading checks, synthetic media only; keep every run's artifacts."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--works', type=int, default=5000)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    fixture = artifacts / 'fixture'
    os.environ['DATA_ROOT'] = str(fixture)
    os.environ['TEMP'] = os.environ['TMP'] = str(artifacts)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    os.environ['PORT'] = str(port)
    import uvicorn
    from PIL import Image, ImageDraw, ImageOps
    from playwright.sync_api import sync_playwright
    from src.auth import _find_system_browser
    from src.core.models import ArchiveIntent, Artist, Asset, Derivative, TrackingPolicy, Work, WorkTag
    from src.core.runtime import Runtime
    from src.web import create_app

    palette = []
    for number in range(24):
        with Image.new('RGB', (640, 860), (38 + number * 7 % 170, 43 + number * 11 % 130, 75 + number * 5 % 140)) as picture:
            draw = ImageDraw.Draw(picture)
            draw.ellipse((120, 130, 650, 680), fill=(160 + number % 60, 148, 165))
            draw.polygon(((0, 700), (350, 300), (640, 760)), fill=(30, 42 + number * 4, 75))
            draw.line((50, 70, 570, 70, 570, 780, 50, 780, 50, 70), fill=(205, 215, 230), width=2)
            draw.text((75, 100), f'LOCAL ARCHIVE / {number + 1:02}', fill='white', font_size=23)
            original, thumb = io.BytesIO(), io.BytesIO()
            picture.save(original, format='PNG')
            with ImageOps.fit(picture, (360, 360)) as small:
                small.save(thumb, format='WEBP', quality=80)
            palette.append((original.getvalue(), thumb.getvalue()))

    class Download:
        fast = False

        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()

        def download(self, url, destination, report=None):
            payload = palette[0][0]
            chunk = max(1, len(payload) // 12)
            with destination.open('xb') as output:
                done = 0
                for offset in range(0, len(payload), chunk):
                    block = payload[offset:offset + chunk]
                    output.write(block); done += len(block)
                    if report:
                        report(done, len(payload))
                    if not self.fast and not self.started.is_set():
                        self.started.set()
                        if not self.release.wait(120):
                            raise TimeoutError('isolated load test did not release its download')
                    if not self.fast:
                        time.sleep(0.12)

    downloader = Download()
    runtime = Runtime(fixture / 'data/pixiv_tracker.db', fixture / 'images', create=True, downloader=downloader)
    (runtime.images / '.thumbnails').mkdir()
    count = max(args.works, 60)
    with runtime.database.write() as session:
        session.execute(Artist.__table__.insert(), [{'id': 1, 'pixiv_user_id': '1', 'name': '测试画师 · 山与光'}, {'id': 2, 'pixiv_user_id': '2', 'name': '测试画师 · 海与夜'}])
        session.execute(TrackingPolicy.__table__.insert(), [{'artist_id': 1, 'mode': 'paused'}, {'artist_id': 2, 'mode': 'stopped'}])
        records = [{'id': index, 'pixiv_illust_id': str(100000 + index), 'artist_id': 1 if index % 2 else 2,
            'title': f'本地作品 {index:05}', 'type': 'manga' if index == count or index % 5 == 0 else 'illust',
            'page_count': 2000 if index == count else 3 if index % 5 == 0 else 1,
            'caption': '<p>此图由测试程序生成，只用于隔离验证。</p>', 'caption_state': 'available',
            'posted_at': datetime(2026, 1, 1) + timedelta(minutes=index)} for index in range(1, count + 1)]
        session.execute(Work.__table__.insert(), records)
        session.execute(ArchiveIntent.__table__.insert(), [{'pixiv_illust_id': row['pixiv_illust_id']} for row in records])
        session.execute(WorkTag.__table__.insert(), [{'work_id': row['id'], 'tag': 'alpha' if row['id'] % 2 else 'beta'} for row in records])
        assets, derivatives = [], []
        for row in records:
            original, thumb = palette[row['id'] % 24]
            for page in range(row['page_count']):
                relative = f"{row['pixiv_illust_id']}_p{page}.png"
                path = runtime.images / relative
                missing = row['id'] <= 20 and page == 0
                if not missing:
                    path.write_bytes(original)
                stat = path.stat() if not missing else None
                assets.append({'work_id': row['id'], 'role': 'image', 'page_index': page, 'relative_path': relative,
                    'width': 640, 'height': 860, 'state': 'missing' if missing else 'verified',
                    'source_url': 'https://i.pximg.net/isolated-original.png', 'sha256': hashlib.sha256(original).hexdigest(),
                    'byte_size': len(original), 'file_mtime_ns': stat.st_mtime_ns if stat else None,
                    'file_identity': stat.st_ino if stat else None})
            relative = f".thumbnails/{row['pixiv_illust_id']}.webp"
            (runtime.images / relative).write_bytes(thumb)
            derivatives.append({'work_id': row['id'], 'kind': 'thumbnail', 'relative_path': relative,
                                'width': 360, 'height': 360, 'state': 'verified'})
        for offset in range(0, len(assets), 500):
            session.execute(Asset.__table__.insert(), assets[offset:offset + 500])
        session.execute(Derivative.__table__.insert(), derivatives)
    del records, assets, derivatives
    app = create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error',
        loop='src.networking:http_loop_factory', timeout_graceful_shutdown=10, limit_concurrency=128))
    thread = threading.Thread(target=server.run, daemon=False)
    thread.start()
    origin = f'http://localhost:{port}'
    checks, failures, metrics = [], [], {}
    try:
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError('isolated server did not start')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
            desktop = browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = desktop.new_page()
            page.on('pageerror', lambda error: failures.append(str(error)))
            page.goto(origin, wait_until='domcontentloaded')
            page.locator('.work-card').first.wait_for()
            page.evaluate("window.readingDocumentMarker = 'keep'")
            page.get_by_label('图库浏览方式').select_option('500')
            page.wait_for_function("document.querySelector('.pagination').textContent.includes('件作品')")
            assert page.locator('.work-card').count() < 60
            assert all('.webp' in src for src in page.locator('.card-picture img').evaluate_all('(images) => images.map(image => image.src)'))
            page.wait_for_function("[...document.querySelectorAll('.card-picture img')].every(image => image.complete)")
            page.screenshot(path=str(artifacts / 'desktop-gallery.png'))
            checks.append('virtual-grid-uses-thumbnails-with-500-items-per-page')
            page.locator(f'.work-card[data-work-id="{count}"]').click()
            page.locator('.image-page').first.wait_for()
            page.wait_for_function("!document.querySelector('.reader-notice')")
            assert page.locator('.image-page').count() < 12
            assert page.locator('.page-window').get_attribute('data-window-size') == '64'
            page.screenshot(path=str(artifacts / 'desktop-reader.png'))
            checks.append('ordinary-reader-virtualizes-2000-page-work')
            old_url = page.url
            original_query = runtime.library.query
            old_request_started = threading.Event()

            def delayed_query(*args, **kwargs):
                if kwargs.get('offset') == 1:
                    old_request_started.set()
                    time.sleep(0.8)
                return original_query(*args, **kwargs)

            runtime.library.query = delayed_query
            page.keyboard.press('ArrowRight')
            assert old_request_started.wait(5)
            page.keyboard.press('ArrowRight')
            page.wait_for_url(f'**/illust/{count - 2}?**')
            page.wait_for_timeout(1000)
            assert f'/illust/{count - 2}?' in page.url
            runtime.library.query = original_query
            checks.append('delayed-old-navigation-cannot-overwrite-newer-intent')
            page.get_by_role('button', name='← 上一件', exact=True).click()
            page.wait_for_url(f'**/illust/{count - 1}?**')
            page.get_by_role('button', name='下一件 →', exact=True).click()
            page.wait_for_url(f'**/illust/{count - 2}?**')
            assert page.evaluate('window.readingDocumentMarker') == 'keep' and page.url != old_url
            checks.append('buttons-and-keys-share-soft-navigation')
            # Reject one target image: the old document and work must remain.
            blocked = f'**/images/{100000 + count - 3}_p0.png'
            page.route(blocked, lambda route: route.abort())
            page.get_by_role('button', name='下一件 →', exact=True).click()
            page.get_by_role('alert').get_by_text('目标图片未能载入，当前画面已保留', exact=False).wait_for()
            assert f'/illust/{count - 2}?' in page.url
            page.unroute(blocked)
            page.get_by_role('alert').get_by_role('button', name='重试', exact=True).click()
            page.wait_for_url(f'**/illust/{count - 3}?**')
            page.keyboard.press('ArrowLeft')
            page.wait_for_url(f'**/illust/{count - 2}?**')
            checks.append('failed-image-keeps-old-work-and-retry-target')
            page.get_by_role('button', name='放大此页', exact=True).first.click()
            page.get_by_role('dialog', name='放大查看').wait_for()
            page.get_by_role('button', name='放大', exact=True).click()
            page.keyboard.press('ArrowRight')
            assert f'/illust/{count - 2}?' in page.url
            page.get_by_role('button', name='关闭放大', exact=False).click()
            assert page.get_by_role('dialog').count() == 0
            checks.append('zoom-layer-blocks-navigation-and-returns-in-place')
            page.get_by_role('button', name='返回图库', exact=True).click()
            page.locator(f'.work-card[data-work-id="{count - 2}"]').wait_for()
            checks.append('return-to-gallery-locates-last-read-work')
            page.get_by_label('图库浏览方式').select_option('scroll')
            page.wait_for_function("document.querySelector('.gallery-viewport').getAttribute('aria-busy') === 'false'")
            grid_windows = []
            for _ in range(min(12, max(1, count // 200 - 1))):
                before_end = page.locator('.gallery-window').evaluate('(element) => Number(element.dataset.start) + Number(element.dataset.windowSize)')
                if before_end >= count:
                    break
                page.locator('.gallery-viewport').evaluate('(element) => element.scrollTop = element.scrollHeight - element.clientHeight - 2')
                page.wait_for_function('(previous) => Number(document.querySelector(".gallery-window").dataset.start) + Number(document.querySelector(".gallery-window").dataset.windowSize) > previous', arg=before_end)
                page.wait_for_function("document.querySelector('.gallery-viewport').getAttribute('aria-busy') === 'false'")
                grid_windows.append(page.locator('.gallery-window').get_attribute('data-window-size'))
                assert page.locator('.work-card').count() < 60
                assert int(grid_windows[-1]) <= 480
            metrics['continuous_grid_window_sizes'] = grid_windows
            page.get_by_role('button', name='回到开头', exact=True).click()
            page.locator(f'.work-card[data-work-id="{count}"]').wait_for()
            checks.append('continuous-grid-slides-a-bounded-window')
            alpha_started = threading.Event()

            def delayed_filter(*args, **kwargs):
                if args and args[0].tags == ('alpha',):
                    alpha_started.set()
                    time.sleep(0.8)
                return original_query(*args, **kwargs)

            runtime.library.query = delayed_filter
            page.get_by_role('button', name='筛选与排序', exact=True).click()
            page.get_by_placeholder('多个标签用逗号分开').fill('alpha')
            page.get_by_role('button', name='应用筛选', exact=True).click()
            assert alpha_started.wait(5)
            page.get_by_placeholder('多个标签用逗号分开').fill('beta')
            page.get_by_role('button', name='应用筛选', exact=True).click()
            page.wait_for_url('**tags=beta**')
            page.wait_for_timeout(1000)
            assert all(int(value) % 2 == 0 for value in page.locator('.work-card').evaluate_all('(cards) => cards.map(card => card.dataset.workId)'))
            assert 'tags=beta' in page.url
            runtime.library.query = original_query
            page.get_by_role('button', name='筛选与排序', exact=True).click()
            page.get_by_role('button', name='重置', exact=True).click()
            page.wait_for_function("!location.search.includes('tags=') && document.querySelector('.work-card')")
            checks.append('delayed-old-filter-cannot-overwrite-new-scope')
            page.get_by_role('button', name='流式阅览', exact=True).click()
            page.locator('.page-viewport.feed').wait_for()
            page.wait_for_function("!document.querySelector('.reader-notice')")
            flow_url = page.url
            page.keyboard.press('ArrowRight')
            assert page.url == flow_url
            before_counter = page.locator('.reading-counter b').inner_text().strip().split()[0]
            page.get_by_role('button', name='跳过当前作品 ↓', exact=True).click()
            page.wait_for_function("!document.querySelector('.reader-notice')")
            page.wait_for_function('(previous) => Number(document.querySelector(".reading-counter b").textContent.trim().split(/\\s+/)[0]) === previous + 1', arg=int(before_counter))
            page.screenshot(path=str(artifacts / 'desktop-feed.png'))
            checks.append('independent-feed-expands-pages-and-skips-work')
            page.get_by_role('button', name='普通阅览', exact=True).click()
            page.wait_for_url(f'**/illust/{count - 2}?**')
            checks.append('ordinary-and-feed-positions-are-independent')
            phone_context = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
            phone = phone_context.new_page()
            phone.on('pageerror', lambda error: failures.append(str(error)))
            phone.goto(f'{origin}/illust/{count}', wait_until='domcontentloaded')
            phone.locator('.image-page').first.wait_for()
            phone.wait_for_function("!document.querySelector('.reader-notice')")
            assert phone.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            phone.screenshot(path=str(artifacts / 'mobile-reader.png'))
            touch = phone_context.new_cdp_session(phone)

            def swipe(points):
                touch.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': points[0][0], 'y': points[0][1]}]})
                for x, y in points[1:]:
                    touch.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': x, 'y': y}]})
                touch.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})

            swipe([(300, 420), (294, 380), (190, 360), (100, 350)])
            phone.wait_for_timeout(300)
            assert f'/illust/{count}?' in phone.url
            swipe([(310, 470), (265, 471), (205, 470), (130, 472)])
            phone.wait_for_url(f'**/illust/{count - 1}?**')
            checks.append('mobile-real-touch-direction-lock-and-horizontal-navigation')
            phone.wait_for_function("!document.querySelector('.reader-notice')")
            phone.get_by_role('button', name='放大此页', exact=True).first.click()
            phone.get_by_role('dialog', name='放大查看').wait_for()
            touch.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': 130, 'y': 400}, {'x': 230, 'y': 400}]})
            touch.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': 85, 'y': 400}, {'x': 275, 'y': 400}]})
            touch.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
            assert phone.locator('.zoom-canvas').evaluate('(element) => new DOMMatrix(getComputedStyle(element).transform).a') > 1.5
            assert f'/illust/{count - 1}?' in phone.url
            phone.get_by_role('button', name='关闭放大', exact=False).click()
            checks.append('mobile-pinch-zooms-without-changing-work')
            phone.get_by_role('button', name='流式阅览', exact=True).click()
            phone.locator('.page-viewport.feed').wait_for()
            phone.screenshot(path=str(artifacts / 'mobile-feed.png'))
            before = phone.url
            swipe([(310, 470), (265, 471), (130, 472)])
            assert phone.url == before
            checks.append('mobile-feed-does-not-use-horizontal-work-switching')
            # A fresh browser context exercises a long stream while its JS heap,
            # rendered images and metadata window are sampled after collection.
            perf_context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            stream = perf_context.new_page()
            stream.on('pageerror', lambda error: failures.append(str(error)))
            stream.goto(origin + '/flow', wait_until='domcontentloaded')
            stream.locator('.page-viewport.feed').wait_for()
            stream.wait_for_function("!document.querySelector('.reader-notice')")
            cdp = perf_context.new_cdp_session(stream)
            samples = []
            metrics['stream_samples'] = samples
            for step in range(30):
                if step == 15:
                    runtime.queue.submit('repair', {'work_ids': list(range(1, 21))}, '隔离下载负载')
                    assert downloader.started.wait(30), 'download phase was not reached'
                previous = stream.locator('.image-page').evaluate_all('(pages) => Math.max(...pages.map(page => Number(page.dataset.globalIndex)))')
                stream.locator('.page-viewport').evaluate('(element) => element.scrollTop = element.scrollHeight - element.clientHeight - 2')
                stream.wait_for_function('(previous) => Math.max(...[...document.querySelectorAll(".image-page")].map(page => Number(page.dataset.globalIndex))) > previous', arg=previous)
                stream.wait_for_function("!document.querySelector('.reader-notice')")
                if step % 5 == 4:
                    cdp.send('HeapProfiler.collectGarbage')
                    heap = cdp.send('Runtime.getHeapUsage')
                    dom = cdp.send('Memory.getDOMCounters')
                    samples.append({'step': step + 1, 'heap_bytes': heap['usedSize'], 'nodes': dom['nodes'],
                        'images': stream.locator('.image-page img').count(), 'window_pages': int(stream.locator('.page-window').get_attribute('data-window-size')),
                        'downloading': any(operation['phase']['name'] == 'downloading' and operation['state'] == 'running' for operation in runtime.queue.snapshot()['operations'])})
                    assert samples[-1]['images'] <= 12 and samples[-1]['window_pages'] <= 120
                    assert samples[-1]['nodes'] < 2000
            assert samples[-1]['heap_bytes'] < samples[0]['heap_bytes'] * 1.8 + 8 * 1024**2
            assert any(sample['downloading'] for sample in samples)
            metrics['stream_samples'] = samples
            checks.append('long-stream-media-dom-and-heap-stay-bounded-idle-and-downloading')
            stream.screenshot(path=str(artifacts / 'long-stream.png'))
            perf_context.close()
            if failures:
                raise AssertionError(failures)
            phone_context.close(); desktop.close(); browser.close()
    finally:
        downloader.fast = True
        downloader.release.set()
        server.should_exit = True
        thread.join(25)
        (artifacts / 'result.json').write_text(json.dumps({'checks': checks, 'page_errors': failures,
            'metrics': metrics, 'works': count, 'server_stopped': not thread.is_alive(), 'real_phone_tested': False}, ensure_ascii=False, indent=2), encoding='utf-8')
        if thread.is_alive():
            raise RuntimeError('isolated server did not stop')
    print(json.dumps({'passed': checks, 'artifacts': str(artifacts)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
