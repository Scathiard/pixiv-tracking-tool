"""Check packaged settings with delayed/failed statistics on synthetic data."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[5]
EVIDENCE = ROOT / 'docs/审查/证据/2026-10-17-读取与调度修订'
URL = 'http://127.0.0.1:19217'

async def main():
    results, errors = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for width, height in [(1440,1000),(390,844),(320,768)]:
                context = await browser.new_context(viewport={'width':width,'height':height},
                    has_touch=width<500, is_mobile=width<500)
                page = await context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                gate = asyncio.Event()
                calls = 0
                async def stats(route):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        await gate.wait()
                        await route.fulfill(status=500, content_type='application/json',
                            body=json.dumps({'detail':'synthetic statistics unavailable'}))
                    else:
                        await route.continue_()
                await page.route('**/api/settings/stats*', stats)
                await page.goto(URL + '/settings', wait_until='domcontentloaded')
                interval = page.get_by_role('spinbutton', name='检查周期', exact=True)
                await expect(interval).to_be_visible()
                await interval.fill('90')
                async with page.expect_response(lambda response: response.request.method=='PATCH' and response.url.endswith('/api/settings')) as pending:
                    await page.get_by_role('button', name='保存周期', exact=True).click()
                saved = await (await pending.value).json()
                assert saved['ok'] and saved['check_interval_minutes'] == 90 and 'stats' not in saved
                assert not gate.is_set()
                gate.set()
                failure = page.get_by_text('统计读取失败：', exact=False)
                await expect(failure).to_be_visible()
                await page.get_by_role('button', name='刷新统计', exact=True).click()
                await expect(page.get_by_text('统计更新于', exact=False)).to_be_visible()
                await page.get_by_role('heading', name='图库位置', exact=True).scroll_into_view_if_needed()
                overflow = await page.evaluate('document.documentElement.scrollWidth > window.innerWidth + 1')
                assert not overflow
                await page.screenshot(path=str(EVIDENCE / f'package-settings-{width}.png'))
                results.append({'viewport': [width,height], 'saved_while_stats_pending':True,
                    'failure_isolated':True, 'refresh_recovered':True, 'horizontal_overflow':overflow})
                await context.close()
        finally:
            await browser.close()
    assert not errors, errors
    value = {'scope':'final EXE on synthetic library, actual pointer/keyboard input; only stats failure intercepted',
        'results':results, 'page_errors':errors, 'real_phone_test':False}
    (EVIDENCE / 'package-browser.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(value,ensure_ascii=False))

asyncio.run(main())
