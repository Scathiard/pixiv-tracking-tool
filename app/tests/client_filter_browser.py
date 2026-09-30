"""Filter catalogue Chromium regression, with a generated local library only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    fixture = artifacts / "fixture"
    os.environ["DATA_ROOT"] = str(fixture)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from playwright.sync_api import sync_playwright
    import uvicorn
    from client_fixture import make_library
    from src.core.models import Artist, Work, WorkTag, LocalPreference, TrackingPolicy
    runtime, remote = make_library(fixture, works=302)
    with runtime.database.write() as session:
        session.add_all([Artist(id=i, pixiv_user_id=str(i), name=f"候选画师 {i:03}") for i in range(13, 138)])
        session.add(TrackingPolicy(artist_id=137, mode="stopped"))
        session.get(Artist, 137).name = "终末画师137"
        session.add_all([WorkTag(work_id=i, tag=f"中文标签 {i:03}") for i in range(1, 303)])
        session.add_all([WorkTag(work_id=1, tag=f"大图库标签 {i:05}") for i in range(10000)])
        session.add(WorkTag(work_id=301, tag="逗号,标签"))
        session.add(LocalPreference(work_id=302, is_hidden=True))
        session.get(Work, 302).artist_id = 137
    from src.auth import _find_system_browser
    from src.web import create_app
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    os.environ["PORT"] = str(port)
    server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True),
        host="127.0.0.1", port=port, log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    checks, errors, metrics = [], [], {"artists": 137, "tags": 10305}
    origin = f"http://127.0.0.1:{port}"
    page = None
    try:
        deadline = time.monotonic() + 25
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin)
            page.locator(".work-card").first.wait_for()
            page.get_by_role("button", name="筛选", exact=True).click()
            dialog = page.get_by_role("dialog", name="筛选作品")
            dialog.get_by_role("button", name="画师", exact=False).first.click()
            dialog.get_by_label("搜索筛选画师").wait_for()
            dialog.locator(".filter-candidate").first.wait_for()
            assert dialog.locator(".candidate-viewport").get_attribute("data-candidate-total") == "137"
            assert dialog.locator(".filter-candidate").count() < 20
            checks.append("default-artists-visible")
            page.screenshot(path=str(artifacts / "desktop-artists.png"))

            def group(name):
                button = dialog.locator(".filter-section-title").filter(has_text=name).first
                if button.get_attribute("aria-expanded") != "true":
                    button.click()
                return button.locator("..").locator(".filter-section-body")

            def apply():
                dialog.get_by_role("button", name="应用筛选", exact=True).click()
                dialog.wait_for(state="hidden")

            def reopen():
                page.get_by_role("button", name="筛选", exact=True).click()
                dialog.wait_for()

            def reset():
                dialog.get_by_role("button", name="重置", exact=True).click()

            artists = group("画师")
            artists.locator(".candidate-viewport").evaluate("e => e.scrollTop = e.scrollHeight")
            artists.locator(".filter-candidate[data-value='137']").wait_for()
            artists.locator(".filter-candidate[data-value='137']").click()
            assert artists.get_by_text("已选：终末画师137", exact=True).is_visible()
            checks.append("last-stopped-artist-browsable-and-selectable")
            artists.get_by_role("button", name="清空选择", exact=True).click()
            artists.get_by_label("搜索筛选画师").fill("候选画师 136")
            artists.get_by_role("button", name="候选画师 136 已停止追踪", exact=True).click()
            apply()
            page.wait_for_function("document.querySelectorAll('.work-card').length === 0")
            reopen()
            artists = group("画师")
            artists.get_by_role("button", name="反选", exact=True).click()
            apply()
            page.locator(".work-card").first.wait_for()
            assert "artist_ids_exclude=136" in page.url
            assert "画师已筛选" in page.locator(".active-filters").inner_text()
            inverse_artist_url = page.url
            page.get_by_role("navigation", name="主导航").get_by_role("button", name="画师", exact=True).click()
            page.get_by_label("搜索画师", exact=True).fill("本地画师 02")
            profile = page.locator(".artist-profile").filter(has=page.get_by_role("heading", name="本地画师 02", exact=True))
            profile.get_by_role("button", name="查看全部作品", exact=False).click()
            page.locator(".work-card").first.wait_for()
            assert "artist_ids=2" in page.url and "artist_ids_exclude=" not in page.url
            assert page.request.get(origin + "/api/library?" + page.url.split("?", 1)[1]).status == 200
            page.goto(inverse_artist_url)
            page.locator(".work-card").first.wait_for()
            checks.append("artist-inverse-summary-and-view-artist-works-without-conflict")
            reopen()
            artists = group("画师")
            artists.get_by_role("button", name="清空选择", exact=True).click()
            apply()
            page.wait_for_function("document.querySelectorAll('.work-card').length === 0")
            reopen()
            artists = group("画师")
            assert artists.get_by_text("未选择画师，应用后无作品", exact=False).is_visible()
            artists.get_by_role("button", name="全选", exact=True).click()
            checks.append("undownloaded-artist-empty-selection-inverse-and-all-recovery")
            reset()
            tags = group("标签")
            tags.locator(".filter-candidate").first.wait_for()
            assert tags.locator(".candidate-viewport").get_attribute("data-candidate-total") == "10305"
            assert tags.locator(".filter-candidate").count() < 20
            tags.locator(".candidate-viewport").evaluate("e => e.scrollTop = e.scrollHeight - e.clientHeight - 150")
            tags.locator(".candidate-viewport").hover()
            page.mouse.wheel(0, 600)
            tags.get_by_role("button", name="风景", exact=True).wait_for()
            tags.get_by_label("搜索筛选标签").fill("大图库标签 09999")
            tags.get_by_role("button", name="大图库标签 09999", exact=True).click()
            tags.get_by_role("button", name="反选", exact=True).click()
            apply()
            page.wait_for_function("location.search.includes('tags_exclude=')")
            metrics["inverse_query_length"] = len(page.url.split("?", 1)[1])
            assert metrics["inverse_query_length"] < 180
            page.locator(".work-card").first.wait_for()
            inverse = page.request.get(origin + "/api/library?" + page.url.split("?", 1)[1] + "&limit=500").json()
            assert inverse["total"] == 301 and any(item["id"] == 1 for item in inverse["items"])
            assert "标签已筛选" in page.locator(".active-filters").inner_text()
            inverse_tag_url = page.url
            page.locator(".work-card[data-work-id='301']").click()
            page.locator(".detail-tags").get_by_role("button", name="#中文标签 301", exact=True).click()
            page.locator(".work-card[data-work-id='301']").wait_for()
            assert "tags_exclude=" not in page.url
            assert page.request.get(origin + "/api/library?" + page.url.split("?", 1)[1]).status == 200
            page.goto(inverse_tag_url)
            page.locator(".work-card").first.wait_for()
            checks.append("tag-inverse-summary-and-detail-tag-without-conflict")
            checks.append("ten-thousand-tags-bounded-dom-scroll-search-and-short-inverse-query")
            reopen()
            tags = group("标签")
            tags.locator(".filter-candidate").first.wait_for()
            assert tags.locator(".candidate-viewport").get_attribute("data-candidate-total") == "10305"
            tags.get_by_label("搜索筛选标签").fill("大图库标签 09999")
            assert tags.get_by_role("button", name="大图库标签 09999", exact=True).get_attribute("aria-pressed") == "false"
            tags.get_by_role("button", name="反选", exact=True).click()
            apply()
            page.wait_for_function("location.search.includes('tags=') && !location.search.includes('tags_exclude=')")
            page.locator(".work-card[data-work-id='1']").wait_for()
            assert page.locator(".work-card").count() == 1
            checks.append("reopen-inverse-preserved-and-any-tag-match")
            reopen()
            tags = group("标签")
            tags.get_by_role("button", name="清空选择", exact=True).click()
            apply()
            page.wait_for_function("location.search.includes('tags=')")
            page.wait_for_function("document.querySelectorAll('.work-card').length === 0")
            reopen()
            tags = group("标签")
            assert tags.get_by_text("未选择标签，应用后无作品", exact=False).is_visible()
            tags.get_by_role("button", name="全选", exact=True).click()
            apply()
            page.wait_for_function("!location.search.includes('tags=') && !location.search.includes('tags_exclude=')")
            page.locator(".work-card").first.wait_for()
            checks.append("explicit-empty-tag-selection-and-all-recovery")

            reopen()
            reset()
            tags = group("标签")
            tags.get_by_label("搜索筛选标签").fill("中文标签 302")
            tags.get_by_role("button", name="中文标签 302", exact=True).click()
            artists = group("画师")
            artists.get_by_label("搜索筛选画师").fill("终末画师137")
            artists.get_by_role("button", name="终末画师137 已停止追踪", exact=True).click()
            dialog.get_by_label("显示已隐藏作品").check()
            types = group("作品类型")
            types.get_by_role("button", name="清空选择", exact=True).click()
            types.get_by_role("button", name="插画", exact=True).click()
            apply()
            page.locator(".work-card[data-work-id='302']").wait_for()
            assert page.locator(".work-card").count() == 1
            reopen()
            tags = group("标签")
            assert tags.locator(".candidate-viewport").get_attribute("data-candidate-total") == "10305"
            reset()
            types = group("作品类型")
            types.get_by_role("button", name="反选", exact=True).click()
            assert types.get_by_text("未选择作品类型，应用后无作品", exact=True).is_visible()
            types.get_by_role("button", name="全选", exact=True).click()
            apply()
            page.locator(".work-card").first.wait_for()
            checks.append("hidden-last-tag-artist-type-combination-stable-candidates-reset-and-type-shortcuts")

            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 844})
                reopen()
                tags = group("标签")
                tags.locator(".filter-candidate").first.wait_for()
                viewport = tags.locator(".candidate-viewport")
                viewport.evaluate("e => e.scrollTop = e.scrollHeight - e.clientHeight - 150")
                box = viewport.bounding_box()
                cdp = context.new_cdp_session(page)
                x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] - 20
                cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 1}]})
                for delta in (40, 80, 140, 190):
                    cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y - delta, "id": 1}]})
                    page.wait_for_timeout(40)
                cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                cdp.detach()
                tags.get_by_role("button", name="风景", exact=True).wait_for()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert dialog.evaluate("e => e.scrollWidth <= e.clientWidth")
                page.screenshot(path=str(artifacts / f"mobile-{width}-tags.png"))
                tags.get_by_label("搜索筛选标签").fill("中文标签 301")
                tags.get_by_role("button", name="中文标签 301", exact=True).click()
                apply()
                page.locator(".work-card[data-work-id='301']").wait_for()
                reopen()
                reset()
                apply()
                checks.append(f"mobile-{width}-browse-search-select-apply-reset-no-overflow")

            page.set_viewport_size({"width": 1440, "height": 1000})
            reopen()
            reset()
            tags = group("标签")
            tags.get_by_label("搜索筛选标签").fill("逗号,标签")
            tags.get_by_role("button", name="逗号,标签", exact=True).click()
            apply()
            page.locator(".work-card[data-work-id='301']").wait_for()
            assert page.locator(".work-card").count() == 1
            assert page.evaluate("new URLSearchParams(location.search).getAll('tags').length") == 2
            page.reload()
            page.locator(".work-card[data-work-id='301']").wait_for()
            reopen()
            tags = group("标签")
            tags.get_by_label("搜索筛选标签").fill("逗号,标签")
            assert tags.get_by_role("button", name="逗号,标签", exact=True).get_attribute("aria-pressed") == "true"
            page.keyboard.press("Escape")
            dialog.wait_for(state="hidden")
            page.locator(".work-card[data-work-id='301']").click()
            page.locator(".detail-tags").get_by_role("button", name="#逗号,标签", exact=True).click()
            page.locator(".work-card[data-work-id='301']").wait_for()
            assert page.evaluate("new URLSearchParams(location.search).getAll('tags').length") == 2
            assert page.request.get(origin + "/api/library?" + page.url.split("?", 1)[1]).json()["total"] == 1
            checks.append("literal-comma-tag-apply-reload-reopen-and-detail-link")
            page.goto(origin + "/?tags=中文标签%20301,中文标签%20302&show_hidden=1")
            page.locator(".work-card[data-work-id='301']").wait_for()
            page.locator(".work-card[data-work-id='302']").wait_for()
            assert page.locator(".work-card").count() == 2
            checks.append("legacy-single-tags-csv-still-matches-both")
            reopen()
            reset()
            apply()
            page.locator(".work-card").first.wait_for()
            deferred = []
            page.route("**/api/library/tags?show_hidden=1", lambda route: deferred.append(route))
            reopen()
            tags = group("标签")
            tags.get_by_text("正在载入全部标签…", exact=True).wait_for()
            assert deferred
            deferred[0].continue_()
            tags.locator(".filter-candidate").first.wait_for()
            page.unroute("**/api/library/tags?show_hidden=1")
            page.keyboard.press("Escape")
            dialog.wait_for(state="hidden")
            checks.append("catalogue-loading-state-visible")

            page.route("**/api/library/tags?show_hidden=1", lambda route: route.fulfill(status=503, json={"detail": "synthetic catalogue failure"}))
            page.route("**/api/artists?**", lambda route: route.fulfill(status=503, json={"detail": "synthetic artist failure"}))
            reopen()
            tags = group("标签")
            tags.get_by_role("button", name="重试", exact=True).wait_for()
            page.screenshot(path=str(artifacts / "desktop-tag-error.png"))
            page.unroute("**/api/library/tags?show_hidden=1")
            tags.get_by_role("button", name="重试", exact=True).click()
            tags.locator(".filter-candidate").first.wait_for()
            checks.append("tag-network-failure-distinct-from-empty-and-retry")
            page.screenshot(path=str(artifacts / "desktop-tags.png"))
            artists = group("画师")
            artists.get_by_role("button", name="重试", exact=True).wait_for()
            page.unroute("**/api/artists?**")
            artists.get_by_role("button", name="重试", exact=True).click()
            artists.locator(".filter-candidate").first.wait_for()
            assert artists.locator(".candidate-viewport").get_attribute("data-candidate-total") == "137"
            checks.append("artist-network-failure-and-complete-retry")
            page.keyboard.press("Escape")
            dialog.wait_for(state="hidden")
            page.route("**/api/library/tags?show_hidden=1", lambda route: route.fulfill(json={"tags": []}))
            page.route("**/api/artists?**", lambda route: route.fulfill(json={"items": [], "total": 0}))
            reopen()
            tags = group("标签")
            tags.get_by_text("本地图库暂无标签", exact=True).wait_for()
            artists = group("画师")
            artists.get_by_text("本地图库暂无画师", exact=True).wait_for()
            checks.append("empty-catalogues-distinct-from-network-failure")
            page.keyboard.press("Escape")
            dialog.wait_for(state="hidden")
            assert not errors, errors
            browser.close()
    except Exception:
        if page:
            try:
                page.screenshot(path=str(artifacts / "failure.png"))
            except Exception:
                pass
        (artifacts / "failure.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        (artifacts / "results.json").write_text(json.dumps({"checks": checks, "errors": errors, "metrics": metrics}, ensure_ascii=False, indent=2), encoding="utf-8")
        server.should_exit = True
        thread.join(10)
        runtime.close()


if __name__ == "__main__":
    main()
