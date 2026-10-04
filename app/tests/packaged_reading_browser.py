"""Drive the packaged EXE against its own synthetic, account-free HTTP service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    exe = args.exe.resolve(strict=True)
    os.environ["DATA_ROOT"] = str(artifacts / "driver-root")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from playwright.sync_api import expect, sync_playwright
    from src.auth import _find_system_browser

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    stop = artifacts / "stop.signal"
    package_report = artifacts / "exe-smoke.json"
    env = os.environ.copy()
    env["TEMP"] = env["TMP"] = str(artifacts)
    env["PYTHONIOENCODING"] = "utf-8"
    checks, page_errors, animation_metrics = [], [], {}
    output = (artifacts / "exe-output.txt").open("xb")
    process = subprocess.Popen([str(exe), "--smoke-test", "--smoke-output", str(package_report),
        "--smoke-serve-port", str(port), "--smoke-stop-file", str(stop)], cwd=artifacts,
        env=env, stdout=output, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"打包程序提前退出，退出码 {process.returncode}")
            try:
                with urlopen(origin + "/api/status", timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                time.sleep(0.15)
        else:
            raise TimeoutError("打包程序未启动隔离服务")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
            def animation_frames(target_page, width):
                card = target_page.locator('.work-card[data-work-id="2"] .card-picture')
                card.scroll_into_view_if_needed()
                target_page.wait_for_function("() => {const img=document.querySelector('.work-card[data-work-id=\"2\"] img[data-media-source=\"animation\"]'); return img?.complete && img.naturalWidth > 0}")
                media = card.locator('img[data-media-source="animation"]')
                assert '_card_motion_v1.webp?v=' in media.get_attribute('src')
                assert media.evaluate('img => [img.naturalWidth, img.naturalHeight]') == [360,270]
                assert not target_page.locator('.work-card img[src*=".gif"]').count()
                checks.append(f'exe-gallery-uses-bounded-motion-cache-{width}')
                frames = []
                # Unequal intervals avoid repeatedly sampling the same phase of
                # a short loop (these fixture frames last 100 and 200 ms).
                for index, delay in enumerate((37,83,149,61,113,47,97,163)):
                    target_page.wait_for_timeout(delay)
                    frames.append(hashlib.sha256(card.screenshot(path=str(artifacts / f'exe-animation-{width}-{index}.png'))).hexdigest())
                animation_metrics[str(width)] = {"unique_frames":len(set(frames)),"sample_count":len(frames)}
                assert len(set(frames)) >= 2, f"frozen gallery animation stayed static at {width}px"
                checks.append(f'exe-gallery-animation-changes-actual-frame-{width}')
            desktop = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = desktop.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(origin, wait_until="domcontentloaded")
            page.locator('.work-card[data-work-id="1"]').wait_for()
            assert '.webp' in page.locator('.work-card[data-work-id="1"] .card-picture img').get_attribute('src').split('?')[0]
            checks.append("exe-opens-new-local-gallery-and-thumbnail")
            gallery = page.request.get(origin + "/api/library?compact=1&limit=1&q=isolated").json()
            detail = page.request.get(origin + "/api/works/1?page_limit=1").json()
            progress = page.request.get(origin + "/api/progress").json()
            expected_archive = {"state": "complete", "total": 1, "verified": 1, "missing": 0, "unknown": 0}
            assert gallery["items"][0]["archive"] == detail["archive"] == expected_archive
            assert detail["processing"]["state"] == "idle" and detail["source_update"]["pending"] is False
            assert type(progress["catalogue_revision"]) is int and progress["catalogue_revision"] > 0
            assert progress["catalogue_revision"] >= detail["catalogue_revision"]
            assert progress["worker_error"] is None and len(progress["operations"]) <= 36
            (artifacts / "exe-archive-contract.json").write_text(json.dumps({"gallery": gallery,
                "detail": detail, "progress": progress}, ensure_ascii=False, indent=2), encoding="utf-8")
            checks.append("exe-whole-work-facts-catalogue-revision-and-bounded-progress")
            selected = page.request.get(origin + "/api/works/2?page_limit=1").json()
            unavailable = page.request.get(origin + "/api/works/3?page_limit=1").json()
            assert selected['archive'] == {'state':'complete','total':2,'verified':2,'missing':0,'unknown':0}
            assert unavailable['archive']['state'] == 'partial'
            unavailable_src=unavailable['pages'][0]['src']
            assert unavailable_src and '.gif' in unavailable_src.split('?')[0]
            assert unavailable['availability']['state'] == 'unavailable'
            assert [work['id'] for work in page.request.get(origin + '/api/library/missing-originals').json()['items']] == [3]
            checks.append('exe-manual-scope-animation-originals-filled-and-unavailable-gif-preserved')
            animation_frames(page, 1440)
            page.locator('.work-card[data-work-id="2"] .card-picture').click()
            page.wait_for_function("() => {const img=document.querySelector('.image-page img'); return img?.complete && img.naturalWidth === 1280 && img.naturalHeight === 960}")
            assert page.locator('.image-page img').first.get_attribute('src')==selected['pages'][0]['src']
            checks.append('exe-animation-detail-keeps-full-resolution-gif')
            page.goto(origin, wait_until='domcontentloaded')
            page.locator('.work-card[data-work-id="1"]').wait_for()
            for width in (390,320):
                animation_phone = browser.new_context(viewport={"width":width,"height":844}, is_mobile=True, has_touch=True)
                animation_page = animation_phone.new_page()
                animation_page.on("pageerror", lambda error:page_errors.append(str(error)))
                animation_page.goto(origin, wait_until="domcontentloaded")
                animation_frames(animation_page, width)
                animation_page.goto(origin + '/settings', wait_until='domcontentloaded')
                assert animation_page.locator('.source-status-item').count()==0
                animation_page.get_by_role('button',name='查看清单',exact=True).click()
                source = animation_page.locator('.source-status-item[data-source-id="3"]')
                source.scroll_into_view_if_needed()
                assert '主站已无法访问' in source.inner_text() and '上次核对' in source.inner_text()
                assert '本地缺少' in source.inner_text() and '动图原包' in source.inner_text() and '动图播放时序' in source.inner_text()
                assert '待确认' not in source.inner_text() and '已确认原作不存在' not in source.inner_text()
                assert animation_page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                animation_page.screenshot(path=str(artifacts / f'exe-originals-unavailable-{width}.png'))
                checks.append(f'exe-paged-source-subview-unified-inaccessible-label-exact-gaps-{width}')
                animation_phone.close()
            scrolling_phone = browser.new_context(viewport={"width":390,"height":420}, is_mobile=True, has_touch=True)
            scrolling_page = scrolling_phone.new_page()
            scrolling_page.on('pageerror', lambda error:page_errors.append(str(error)))
            scrolling_page.goto(origin, wait_until='domcontentloaded')
            scrolling_page.wait_for_function("document.querySelectorAll('img[data-media-source=\"animation\"]').length > 0")
            pane = scrolling_page.locator('.gallery-viewport')
            assert pane.evaluate('e => e.scrollHeight > e.clientHeight')
            scrolling_page.evaluate("window.__initialAnimation = document.querySelector('img[data-media-source=\"animation\"]')")
            scrolling_page.mouse.move(180, 250)
            scrolling_page.mouse.wheel(0, 80)
            scrolling_page.wait_for_function("document.querySelector('.gallery-viewport').scrollTop > 0")
            paused = scrolling_page.evaluate("({animations:document.querySelectorAll('img[data-media-source=\"animation\"]').length,scrollTop:document.querySelector('.gallery-viewport').scrollTop,oldReleased:!window.__initialAnimation.isConnected || !window.__initialAnimation.hasAttribute('src')})")
            assert paused['animations'] == 0 and paused['oldReleased']
            scrolling_page.wait_for_function("document.querySelectorAll('img[data-media-source=\"animation\"]').length > 0")
            animation_metrics['scroll_pause'] = paused
            checks.append('exe-gallery-wheel-pauses-and-resumes-animation')
            scrolling_phone.close()
            page.get_by_role("button", name="收起侧栏", exact=True).click()
            page.locator(".app-shell.sidebar-collapsed").wait_for()
            page.get_by_role("button", name="展开侧栏", exact=True).click()
            page.get_by_role("button", name="检查新作品", exact=True).wait_for()
            checks.append("exe-b-sidebar-and-manual-check-entry")
            page.get_by_role('button', name='筛选', exact=True).click()
            dialog = page.get_by_role('dialog', name='筛选作品')
            dialog.get_by_role('button', name='画师', exact=False).first.click()
            dialog.locator('.filter-candidate[data-value="1"]').wait_for()
            assert dialog.locator('.filter-candidate[data-value="1"] .candidate-label').inner_text() == 'smoke'
            dialog.get_by_role('button', name='标签', exact=False).first.click()
            dialog.locator('.filter-candidate[data-value="本地动图"]').wait_for()
            dialog.locator('.filter-candidate[data-value="本地单图"]').click()
            dialog.get_by_role('button', name='应用筛选', exact=True).click()
            dialog.wait_for(state='hidden')
            page.locator('.work-card[data-work-id="1"]').wait_for()
            page.wait_for_function("document.querySelectorAll('.work-card').length === 1")
            page.get_by_role('button', name='筛选', exact=True).click()
            dialog.get_by_role('button', name='标签', exact=False).first.click()
            assert dialog.locator('.filter-candidate[data-value="本地单图"]').get_attribute('aria-pressed') == 'true'
            dialog.get_by_role('button', name='重置', exact=True).click()
            dialog.get_by_role('button', name='应用筛选', exact=True).click()
            page.wait_for_function("document.querySelectorAll('.work-card').length === 3")
            checks.append('exe-full-local-filter-candidates-selection-reopen-reset')
            page.locator('.work-card[data-work-id="1"] .card-picture').click()
            page.locator('.image-page img').first.wait_for()
            page.wait_for_function("() => { const img = document.querySelector('.image-page img'); return img.complete && img.naturalWidth > 0; }")
            assert page.locator('.image-page img').first.evaluate("img => [img.naturalWidth, img.naturalHeight]") == [32, 48]
            assert page.locator('.image-page img').first.get_attribute('src')==detail['pages'][0]['src']
            page.screenshot(path=str(artifacts / "exe-ordinary.png"))
            checks.append("exe-ordinary-reader-loads-original")
            tag_detail = page.request.get(origin + "/api/works/1?page_limit=1").json()["tag_details"][0]
            assert tag_detail["name"] == "本地单图" and tag_detail["translated_name"] == "测试单图"
            assert tag_detail["source"] == "pixiv" and tag_detail["language_basis"] == "requested_locale"
            assert page.locator('.tag-translation[data-source="pixiv"]').inner_text() == "测试单图"
            checks.append("exe-tag-translation-renders-from-persisted-local-metadata")
            page.locator('.image-page img').first.dblclick()
            page.get_by_role("dialog", name="放大查看").wait_for()
            page.get_by_role("button", name="关闭放大", exact=False).click()
            page.get_by_role("navigation", name="主导航").get_by_role("button", name="流式", exact=False).click()
            page.locator('.page-viewport.feed').wait_for()
            page.wait_for_function("!document.querySelector('.reader-notice')")
            page.screenshot(path=str(artifacts / "exe-feed.png"))
            checks.append("exe-zoom-and-feed-share-synthetic-library")
            page.goto(origin + "/settings", wait_until="domcontentloaded")
            page.locator(".settings-page").wait_for()
            page.wait_for_function("document.querySelector('.sidebar-scopes button[title=全部作品] small')?.innerText==='3'")
            checks.append('exe-direct-settings-sidebar-library-count-three')
            assert page.locator('.source-status-item').count()==0
            page.get_by_role('button',name='查看清单',exact=True).click()
            source = page.locator('.source-status-item[data-source-id="3"]')
            assert '主站已无法访问' in source.inner_text() and '动图原包' in source.inner_text()
            page.get_by_role('button',name='返回设置',exact=False).click()
            assert page.get_by_role('button', name='检查并补齐图库', exact=True).count() == 1
            for old_label in ('补下载', '刷新作品信息', '同步收藏状态', '全量核验本地文件'):
                assert page.get_by_role('button', name=old_label, exact=True).count() == 0
            checks.append('exe-unified-maintenance-entry-and-unavailable-archive-retained')
            page.get_by_text("离线，本地图库仍可阅读", exact=True).wait_for()
            checks.append("exe-real-settings-remain-usable-offline")
            from src.version import __version__, __build__
            settings = page.request.get(origin + "/api/settings").json()
            assert settings["version"] == __version__ and settings["build"] == __build__
            assert set(('registered_originals','verified_originals','missing_originals','unverified_originals')).issubset(settings['stats'])
            paused=settings['automatic_paused']
            page.get_by_label('检查周期',exact=True).fill('2');page.get_by_label('检查周期单位',exact=True).select_option('hours')
            page.get_by_role('button',name='保存周期',exact=True).click()
            page.wait_for_function("document.querySelector('.settings-page').innerText.includes('每 120 分钟')")
            after=page.request.get(origin+'/api/settings').json()
            assert after['check_interval_minutes']==120 and after['automatic_paused']==paused and after['next_check_at']
            assert page.locator("select[aria-label='图库浏览方式'] option[value='500']").count()==1
            checks.append('exe-daily-hour-interval-save-pause-preservation-next-time-current-original-stats-500-option')
            page.get_by_text(f"版本 {__version__} · 内部构建 {__build__}", exact=True).wait_for()
            page.mouse.move(1100, 600)
            page.mouse.wheel(0, 10000)
            page.wait_for_function("""() => {const pane=document.querySelector('.settings-page');
                const bottom=document.querySelector('.settings-version').getBoundingClientRect();
                return pane.scrollTop > 0 && bottom.bottom <= innerHeight && bottom.top > 0;}""")
            checks.append("exe-settings-bottom-reachable-by-wheel")
            page.get_by_role("button", name="查看日志", exact=True).click()
            page.locator(".settings-log pre").wait_for()
            assert page.request.get(origin + "/api/logs").ok
            checks.append("exe-public-version-build-and-read-only-log")
            for label in ("浅色", "深色", "浅色", "深色"):
                page.get_by_role("button", name=label, exact=True).click()
                assert page.locator("body").get_attribute("data-theme") == ("light" if label == "浅色" else "dark")
            page.reload()
            page.locator(".settings-page").wait_for()
            assert page.locator("body").get_attribute("data-theme") == "dark"
            checks.append("exe-theme-rapid-clicks-and-reload")
            page.set_viewport_size({'width':2560,'height':900})
            for collapsed in (False, True):
                if bool(page.locator('.app-shell.sidebar-collapsed').count()) != collapsed:
                    page.get_by_role('button', name='收起侧栏' if collapsed else '展开侧栏', exact=True).click()
                sidebar = page.locator('.app-header').bounding_box()
                assert sidebar
                for position, x in (('left', sidebar['x'] + sidebar['width'] + 8), ('right', 2548)):
                    page.locator('.settings-page').evaluate('e => e.scrollTop = 0')
                    page.mouse.move(x, 500)
                    page.mouse.wheel(0, 450)
                    page.wait_for_function("document.querySelector('.settings-page').scrollTop > 0")
            page.screenshot(path=str(artifacts / 'exe-settings-gutter-wheel.png'))
            checks.append('exe-wide-settings-both-gutters-and-sidebar-wheel')
            page.set_viewport_size({'width':1440,'height':900})
            page.goto(origin+'/artists');page.locator('.artist-profile').first.wait_for()
            page.wait_for_function("document.querySelector('.sidebar-scopes button[title=全部作品] small')?.innerText==='3'")
            page.goto(origin + "/artist/1")
            page.locator(".artist-profile").wait_for()
            page.wait_for_function("document.querySelector('.sidebar-scopes button[title=全部作品] small')?.innerText==='3'")
            checks.append('exe-direct-artists-and-artist-sidebar-library-count-three')
            link = page.get_by_role('link', name='在 Pixiv 查看 smoke', exact=True)
            assert link.is_visible() and link.get_attribute('href') == 'https://www.pixiv.net/users/1'
            assert link.get_attribute('target') == '_blank'
            checks.append('exe-artist-main-site-profile-link')
            checks.append("exe-direct-artist-route-and-local-profile")
            grid=page.locator('.artist-profile').first.evaluate("e=>[...e.querySelectorAll('.artist-work')].map(x=>x.getBoundingClientRect().width)")
            assert len(grid)==3
            page.get_by_role('button',name='列表',exact=True).click()
            row=page.locator('.artist-profile').first.evaluate("e=>[...e.querySelectorAll('.artist-work')].map(x=>x.getBoundingClientRect().width)")
            assert len(row)==3 and max(abs(left-right) for left,right in zip(grid,row))<.1
            assert page.get_by_role('button',name='检查这位画师新作',exact=True).is_visible()
            checks.append('exe-artist-three-grid-and-equal-list-thumbnails-per-artist-check-entry')
            phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            mobile = phone.new_page()
            mobile.on("pageerror", lambda error: page_errors.append(str(error)))
            mobile.goto(origin + "/illust/1", wait_until="domcontentloaded")
            mobile.locator('.image-page img').first.wait_for()
            mobile.wait_for_function("() => { const img = document.querySelector('.image-page img'); return img.complete && img.naturalWidth > 0; }")
            assert mobile.locator('.image-page img').first.evaluate("img => [img.naturalWidth, img.naturalHeight]") == [32, 48]
            assert mobile.evaluate("document.documentElement.scrollWidth <= innerWidth")
            mobile.screenshot(path=str(artifacts / "exe-mobile.png"))
            checks.append("exe-mobile-viewport-fits-reader")
            mobile.get_by_role("button", name="打开菜单", exact=True).tap()
            mobile.get_by_role("navigation", name="主导航").get_by_role("button", name="设置", exact=True).tap()
            mobile.locator(".settings-page").wait_for()
            assert mobile.get_by_role("button", name="打开菜单", exact=True).is_visible()
            mobile.wait_for_function("""() => !document.querySelector('.app-header').classList.contains('is-open')
                && document.querySelector('.sidebar-body').getBoundingClientRect().right <= 0""")
            mobile.mouse.move(270, 650)
            mobile.mouse.wheel(0, 10000)
            mobile.wait_for_function("""() => {const pane=document.querySelector('.settings-page');
                const bottom=document.querySelector('.settings-version')?.getBoundingClientRect();
                return pane.scrollTop > 0 && bottom && bottom.bottom <= innerHeight && bottom.top > 50;}""")
            mobile.screenshot(path=str(artifacts / "exe-mobile-settings-bottom.png"))
            checks.append("exe-mobile-settings-scroll-container-reaches-bottom")
            checks.append("exe-mobile-drawer-navigation")
            mobile.set_viewport_size({'width':320,'height':844});mobile.goto(origin+'/flow')
            mobile.locator('.page-viewport.feed').wait_for();mobile.locator('.reading-counter small').wait_for()
            counter=mobile.locator('.reading-counter small').evaluate("e=>{const range=document.createRange();range.selectNodeContents(e);return {text:e.innerText,lines:range.getClientRects().length,width:e.clientWidth,content:e.scrollWidth}}")
            assert '页' in counter['text'] and counter['lines']==1 and counter['content']<=counter['width']+1,counter
            for button in mobile.locator('.reading-toolbar button').all():
                box=button.bounding_box();assert box and box['width']>=40 and box['height']>=40
            mobile.screenshot(path=str(artifacts/'exe-flow-toolbar-320.png'))
            checks.append('exe-flow-320-controls-readable-counter-single-line-no-isolated-page-unit')
            # Replay a stopped worker, including a paused persisted task. The
            # packaged UI must not imply that its Continue button can restart
            # dead workers; local images must remain readable during the error.
            fault = {"round": None, "worker_error": "TimeoutError", "automatic_paused": False,
                "catalogue_revision": progress["catalogue_revision"], "active_operation_count": 1,
                "operations": [{"id": "synthetic-stopped-worker", "label": "检查并补齐图库",
                    "state": "paused", "source": "manual", "pause_requested": True,
                    "control": {"can_pause": False, "can_resume": True}, "result": {},
                    "phase": {"name": "verifying", "done": 12, "total": 50, "unit": "assets", "detail": ""}}]}
            for width in (1440, 390, 320):
                fault_context = browser.new_context(viewport={"width": width, "height": 844})
                fault_page = fault_context.new_page()
                fault_page.on("pageerror", lambda error: page_errors.append(str(error)))
                fault_page.route("**/api/progress", lambda route: route.fulfill(json=fault))
                fault_page.goto(origin + "/illust/1", wait_until="domcontentloaded")
                expect(fault_page.locator(".task-phase")).to_have_text("后台已中断，请重启程序")
                expect(fault_page.locator(".task-control")).to_have_count(0)
                expect(fault_page.locator(".task-track")).to_have_count(0)
                fault_page.wait_for_function("() => {const img=document.querySelector('.image-page img'); return img?.complete && img.naturalWidth > 0}")
                assert fault_page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                fault_page.screenshot(path=str(artifacts / f"exe-worker-stopped-{width}.png"))
                checks.append(f"exe-worker-error-truthful-action-and-local-reading-{width}")
                fault_context.close()
            if page_errors:
                raise AssertionError(page_errors)
            phone.close(); desktop.close(); browser.close()
    finally:
        stop.write_text("stop", encoding="utf-8")
        try:
            code = process.wait(timeout=35)
        except subprocess.TimeoutExpired:
            process.terminate()
            code = process.wait(timeout=10)
        output.close()
        (artifacts / "result.json").write_text(json.dumps({"checks": checks,
            "page_errors": page_errors, "animation_metrics": animation_metrics, "exe_exit_code": code, "package_report": str(package_report),
            "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
            "real_account_used": False, "real_library_used": False, "real_phone_tested": False},
            ensure_ascii=False, indent=2), encoding="utf-8")
    if code != 0 or not package_report.is_file():
        raise RuntimeError(f"打包程序隔离服务退出异常，退出码 {code}")
    print(json.dumps({"checks": checks, "artifacts": str(artifacts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
