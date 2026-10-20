"""Focused real-component reading replay, with synthetic metadata/media only."""
import argparse
import json
import os
import re
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--artifacts', required=True, type=Path)
parser.add_argument('--url', default='http://127.0.0.1:5198/reader/tests/reading-interactions.html')
parser.add_argument('--bundle', action='store_true', help='Inject the synthetic API into the actual app HTML at --url')
args = parser.parse_args()
args.artifacts.mkdir(parents=True, exist_ok=False)
args.artifacts = args.artifacts.resolve()
os.environ['TEMP'] = os.environ['TMP'] = str(args.artifacts)
result = {'checks': [], 'errors': [], 'real_library_used': False, 'real_account_used': False}
synthetic = re.search(r'<script id="synthetic-api">(.*?)</script>', Path(__file__).with_name('reading-interactions.html').read_text(encoding='utf-8'), re.S)[1]

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe')
    try:
        for width in (1440, 390, 320):
            context = browser.new_context(viewport={'width': width, 'height': 900})
            page = context.new_page()
            page.set_default_timeout(6000)
            page.on('pageerror', lambda error: result['errors'].append(str(error)))
            page.route('**/synthetic-reading/*.svg', lambda route: route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="320"><rect width="240" height="320" fill="teal"/></svg>'))
            if args.bundle:
                def inject(route):
                    original = route.fetch()
                    route.fulfill(response=original, body=original.text().replace('<head>', '<head><script>' + synthetic + '</script>', 1))
                page.route(args.url.rstrip('/') + '/', inject)
            page.goto(args.url.rstrip('/') + '/' if args.bundle else args.url)
            expect(page.locator('.work-card strong').first).to_have_text('合成作品 1')
            page.locator('.work-card strong').first.click()
            title = page.locator('.detail-title-row h1')
            expect(title).to_have_text('合成作品 1')
            dock = page.locator('.reading-toolbar')
            next_work = dock.get_by_role('button', name='下一件作品', exact=True)
            previous_work = dock.get_by_role('button', name='上一件作品', exact=True)
            reads_before = page.evaluate("readingFixture.requests.filter(x=>x.startsWith('/api/library/cursor?')).length")

            heart = dock.locator('.bookmark-button')
            expect(heart).to_have_attribute('aria-busy', 'true')
            expect(heart).to_be_enabled()
            heart.click()
            expect(heart).to_have_class(re.compile(r'\backnowledging\b'))
            page.wait_for_function('readingFixture.bookmarkWrites.length === 1')
            assert page.evaluate('readingFixture.bookmarkWrites[0].desired') is True
            assert page.evaluate('readingFixture.bookmarkWrites[0].expected_account_id') == 'synthetic'
            expect(heart).not_to_have_class(re.compile(r'\backnowledging\b'), timeout=4500)
            expect(heart).to_have_attribute('aria-pressed', 'true')
            result['checks'].append(f'{width}: background refresh leaves heart clickable; immediate acknowledgment; captured intent; confirmed fact after three seconds')

            # Preview is display-ready while the original remains pending.
            page.evaluate("readingFixture.generation++; readingFixture.held.add('/synthetic-reading/2.svg'); readingFixture.held.add('/synthetic-reading/3.svg')")
            next_work.click()
            expect(title).to_have_text('合成作品 2')
            page.wait_for_function("readingFixture.pending.has('/synthetic-reading/2.svg')")
            expect(page.locator('.image-page img').first).to_be_visible()
            assert page.locator('.image-page img').first.get_attribute('src').startswith('data:image/svg+xml,')
            assert page.evaluate("readingFixture.requests.filter(x=>x.startsWith('/api/library/cursor?')).length") == reads_before
            result['checks'].append(f'{width}: validated generation continuation; single step uses neighbor identity; preview displays before original; no decode barrier')

            # A genuine expired key gets one identity rebase, then the original
            # next intent waits visibly without losing the currently shown work.
            page.evaluate('readingFixture.generation++; readingFixture.expire = 1')
            next_work.click()
            expect(dock.locator('.reading-activity')).to_contain_text('正在准备图片')
            expect(title).to_have_text('合成作品 2')
            page.wait_for_function("readingFixture.pending.has('/synthetic-reading/3.svg')")
            count = page.evaluate("readingFixture.images.filter(x=>x==='/synthetic-reading/3.svg').length")
            next_work.click()
            assert page.evaluate("readingFixture.images.filter(x=>x==='/synthetic-reading/3.svg').length") == count
            assert any('/api/works/2/cursor-neighbors?' in request and 'cursor=' not in request for request in page.evaluate('readingFixture.requests'))
            dock.get_by_role('button', name='取消', exact=True).click()
            expect(dock.locator('.reading-activity')).to_have_count(0)
            expect(title).to_have_text('合成作品 2')
            result['checks'].append(f'{width}: expired key rebases and resumes next; old picture retained; repeated target does not restart; cancel exits wait')

            # A different direction supersedes the held resource; late media
            # completion cannot restore the abandoned target.
            next_work.click()
            expect(dock.locator('.reading-activity')).to_contain_text('正在准备图片')
            previous_work.click()
            expect(title).to_have_text('合成作品 1')
            page.evaluate("readingFixture.pending.get('/synthetic-reading/3.svg')?.()")
            expect(title).to_have_text('合成作品 1')
            page.evaluate('readingFixture.generation++')
            next_work.click()
            expect(title).to_have_text('合成作品 2')
            result['checks'].append(f'{width}: opposite intent wins and later generation changes remain navigable')

            # Resource error leaves a retry in the same dock. The retry remains
            # the original next action, rather than merely re-opening work 2.
            page.evaluate("readingFixture.held.delete('/synthetic-reading/3.svg'); readingFixture.failures.add('/synthetic-reading/3.svg')")
            next_work.click()
            page.evaluate("readingFixture.pendingErrors.get('/synthetic-reading/3.svg')?.()")
            expect(dock.locator('.reading-activity')).to_contain_text('目标图片未能载入')
            expect(title).to_have_text('合成作品 2')
            page.evaluate("readingFixture.failures.delete('/synthetic-reading/3.svg')")
            dock.get_by_role('button', name='重试', exact=True).click()
            expect(title).to_have_text('合成作品 3')
            assert dock.bounding_box()['width'] <= width
            page.screenshot(path=str(args.artifacts / f'reader-{width}.png'))
            result['checks'].append(f'{width}: image failure and retry resume original next action; dock stays within viewport')
            context.close()
    finally:
        browser.close()
        (args.artifacts / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
assert not result['errors'], result['errors']
print(json.dumps(result, ensure_ascii=False))
