"""Tiny real-component navigation replay against the Vite fixture, without a backend."""
import argparse
import json
import os
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--artifacts', required=True, type=Path)
parser.add_argument('--url', help='Already-running isolated EXE/source origin, to verify its actual bundle with the same synthetic replay')
args = parser.parse_args()
args.artifacts.mkdir(parents=True, exist_ok=False)
args.artifacts = args.artifacts.resolve()
os.environ['TEMP'] = os.environ['TMP'] = str(args.artifacts)
result = {'checks': [], 'errors': [], 'real_library_used': False, 'real_account_used': False}

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe')
    try:
        for width in (1440, 390):
            context = browser.new_context(viewport={'width': width, 'height': 900})
            page = context.new_page()
            page.set_default_timeout(5000)
            page.on('pageerror', lambda error: result['errors'].append(str(error)))
            if args.url:
                synthetic = re.search(r'<script id="synthetic-api">(.*?)</script>', Path(__file__).with_name('gallery-entry.html').read_text(encoding='utf-8'), re.S)[1]
                def inject(route):
                    original = route.fetch()
                    html = original.text().replace('<head>', '<head><script>' + synthetic + '</script>', 1)
                    route.fulfill(response=original, body=html)
                page.route(args.url.rstrip('/') + '/', inject)
                page.goto(args.url.rstrip('/') + '/')
            else:
                page.goto('http://127.0.0.1:5198/reader/tests/gallery-entry.html')
            card = page.locator('.work-card strong').first
            expect(card).to_have_text('合成作品 1')
            assert page.evaluate("galleryEntryFixture.requests.filter(x=>x.startsWith('/api/library?')).length") == 1
            assert page.evaluate("galleryEntryFixture.pending.has('counts')")
            result['checks'].append(f'{width}: cards appear with counts still pending; no repeated first batch')

            # A failed search retains the cards, summary, input and remembered home scope.
            page.evaluate('galleryEntryFixture.fail = true')
            search = page.locator('.side-search input')
            def type_search(text):
                if width < 500:
                    page.get_by_role('button', name='打开菜单', exact=True).click()
                search.fill(text)
                if width < 500:
                    page.locator('.mobile-close').click()
            type_search('failed')
            expect(page.get_by_role('alert')).to_contain_text('合成读取超时')
            expect(card).to_have_text('合成作品 1')
            expect(search).to_have_value('')
            assert page.evaluate("localStorage.getItem('pixiv-local-home-scope') || ''") == ''
            assert page.evaluate("galleryEntryFixture.cancelled.includes('counts')")
            result['checks'].append(f'{width}: failed navigation preserves cards and filter controls; old counts cancelled')
            page.evaluate('galleryEntryFixture.fail = false')
            page.get_by_role('alert').get_by_text('重试', exact=True).click()
            expect(card).to_have_text('failed 1')
            expect(search).to_have_value('failed')

            # Superseding a slow intent releases its read and never commits late metadata.
            type_search('slow')
            page.wait_for_function("galleryEntryFixture.pending.has('slow')")
            type_search('fast')
            expect(card).to_have_text('fast 1')
            assert page.evaluate("galleryEntryFixture.cancelled.includes('slow')")
            result['checks'].append(f'{width}: latest search wins; superseded slow read is cancelled')

            page.evaluate("history.pushState(null, '', '/?page=2'); dispatchEvent(new PopStateEvent('popstate'))")
            expect(card).to_have_text('合成作品 101')
            expect(page.get_by_role('spinbutton', name='图库页码')).to_have_value('2')
            result['checks'].append(f'{width}: explicit page two uses its seed and displays the correct page')
            reads_before = page.evaluate("galleryEntryFixture.requests.filter(x=>x.startsWith('/api/library?')).length")
            # Browser route APIs deliberately drive direct address and feed entries.
            page.evaluate("history.pushState(null, '', '/illust/1'); dispatchEvent(new PopStateEvent('popstate'))")
            expect(page.locator('.detail-title-row h1')).to_have_text('合成作品 1')
            page.evaluate("history.pushState(null, '', '/flow'); dispatchEvent(new PopStateEvent('popstate'))")
            expect(page.locator('.stream-head h1')).to_have_text('流式阅览')
            page.get_by_text('返回图库', exact=True).first.click()
            expect(card).to_have_text('合成作品 1')
            assert page.evaluate("galleryEntryFixture.requests.filter(x=>x.startsWith('/api/library?')).length") == reads_before + 3
            result['checks'].append(f'{width}: direct detail, feed and return to gallery remain usable')
            # A remount in continuous mode also consumes its first response exactly once.
            page.evaluate("localStorage.setItem('pixiv-local-page-size', 'scroll')")
            page.goto('http://127.0.0.1:5198/reader/tests/gallery-entry.html' if not args.url else args.url.rstrip('/') + '/')
            expect(card).to_have_text('fast 1')
            assert page.locator('.pagination').count() == 0
            assert page.evaluate("galleryEntryFixture.requests.filter(x=>x.startsWith('/api/library?')).length") == 1
            page.locator('.gallery-viewport').hover()
            page.mouse.wheel(0, 1400)
            page.wait_for_function("document.querySelector('.gallery-viewport').scrollTop > 500")
            if width < 500:
                page.get_by_role('button', name='返回我的图库', exact=True).first.click()
            else:
                page.locator('.sidebar-head .brand').click()
            page.wait_for_function("document.querySelector('.gallery-viewport').scrollTop < 4")
            result['checks'].append(f'{width}: continuous entry reuses one batch and Logo returns to the beginning')
            context.close()
        assert not result['errors'], result['errors']
    except Exception as exc:
        result['failure'] = str(exc)
        page.screenshot(path=str(args.artifacts / 'failure.png'))
        raise
    finally:
        (args.artifacts / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        browser.close()
print(json.dumps(result, ensure_ascii=False, indent=2))
