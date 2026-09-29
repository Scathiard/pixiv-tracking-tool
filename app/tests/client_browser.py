"""Production reader regression with real Chromium input and generated local media."""
from __future__ import annotations

import argparse
import hashlib
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
    parser.add_argument("--works", type=int, default=5000)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    fixture = artifacts / "fixture"
    os.environ["DATA_ROOT"] = str(fixture)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    os.environ["PORT"] = str(port)
    from playwright.sync_api import sync_playwright
    import uvicorn
    from client_fixture import make_library
    started = time.monotonic()
    runtime, remote = make_library(fixture, args.works)
    from src.auth import _find_system_browser
    from src.web import create_app
    app = create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
        log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{port}"
    checks, errors, metrics = [], [], {"works": args.works, "long_work_pages": 2000}
    metrics["frontend_index_sha256"] = hashlib.sha256(
        (Path(__file__).resolve().parents[1] / "frontend/dist/index.html").read_bytes()).hexdigest()
    browser = None
    playwright = None
    page = None

    def passed(label):
        checks.append(label)
        print(label, flush=True)

    def fit(target):
        assert target.evaluate("document.documentElement.scrollWidth <= innerWidth"), "horizontal page overflow"

    def reading(target):
        target.locator(".image-page img").first.wait_for()
        target.wait_for_function("[...document.querySelectorAll('.image-page img')].some(i=>i.complete&&i.naturalWidth>0)")

    try:
        deadline = time.monotonic() + 25
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("isolated server did not start")
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
        desktop = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = desktop.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        requests = []
        page.on("request", lambda request: requests.append(request.url) if "/images/" in request.url else None)
        page.goto(origin)
        page.locator(".work-card").first.wait_for()
        fit(page)
        assert page.locator(".work-card").count() < 80
        assert page.get_by_role("navigation", name="主导航").get_by_role("button", name="阅览", exact=True).count() == 0
        assert not page.locator(".task-track").is_visible()
        page.wait_for_function("[...document.querySelectorAll('.work-card img')].slice(0,5).every(i=>i.complete&&i.naturalWidth>0)")
        page.screenshot(path=str(artifacts / "desktop-gallery.png"))
        passed("desktop-gallery-b-idle-progress-hidden")

        page.get_by_role("navigation", name="主导航").get_by_role("button", name="设置", exact=True).click()
        page.get_by_label("图库浏览方式").select_option("scroll")
        page.get_by_role("button", name="返回我的图库", exact=True).click()
        page.locator(".work-card").first.wait_for()
        cdp = desktop.new_cdp_session(page)
        cdp.send("Performance.enable")
        samples = []
        page.locator(".gallery-viewport").hover()
        for step in range(36):
            page.mouse.wheel(0, 1000)
            page.wait_for_timeout(90)
            if step % 6 == 0:
                window = int(page.locator(".gallery-window").get_attribute("data-window-size"))
                assert window <= 480
                assert page.locator(".work-card").count() < 90
                samples.append({"step": step, "window": window, "dom": page.locator("*").count(),
                    "heap": next(item["value"] for item in cdp.send("Performance.getMetrics")["metrics"] if item["name"] == "JSHeapUsedSize"),
                    "image_requests": len(requests)})
        metrics["gallery_samples"] = samples
        passed("continuous-gallery-bounded-window-and-dom")

        page.goto(origin + f"/illust/{args.works}")
        reading(page)
        page.evaluate("window.documentMarker='preserved'")
        assert int(page.locator(".page-window").get_attribute("data-window-size")) <= 120
        assert page.locator(".image-page").count() < 14
        page.screenshot(path=str(artifacts / "desktop-detail.png"))
        page.locator(".page-viewport").hover()
        for step in range(40):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(70)
            assert int(page.locator(".page-window").get_attribute("data-window-size")) <= 120
            assert page.locator(".image-page").count() < 16
        metrics["reader_last_page"] = page.locator(".image-page").last.get_attribute("data-page")
        passed("two-thousand-page-work-virtualized-by-page")

        page.keyboard.press("ArrowRight")
        page.wait_for_url(f"**/illust/{args.works - 1}*")
        reading(page)
        assert page.evaluate("window.documentMarker") == "preserved"
        page.get_by_role("button", name="返回我的图库", exact=True).click()
        page.locator(f".work-card[data-work-id='{args.works - 1}']").wait_for()
        assert page.evaluate("window.documentMarker") == "preserved"
        passed("keyboard-navigation-and-logo-position-without-refresh")

        page.get_by_role("button", name="筛选", exact=True).click()
        dialog = page.get_by_role("dialog", name="筛选作品")
        dialog.wait_for()
        page.screenshot(path=str(artifacts / "desktop-filter.png"))
        page.keyboard.press("Escape")
        dialog.wait_for(state="hidden")
        search = page.get_by_label("搜索作品、画师或标签")
        search.fill("本地作品 00035")
        page.locator(".work-card[data-work-id='35']").wait_for()
        assert page.locator(".work-card").count() == 1
        passed("filter-drawer-and-shared-text-search")

        page.goto(origin + f"/illust/{args.works}")
        reading(page)
        page.get_by_role("navigation", name="主导航").get_by_role("button", name="流式", exact=False).click()
        page.locator(".page-viewport.feed").wait_for()
        reading(page)
        heart = page.locator(".reading-toolbar .bookmark-button")
        skip = page.get_by_role("button", name="跳过作品", exact=False)
        assert heart.bounding_box()["x"] < skip.bounding_box()["x"]
        assert int(page.locator(".image-page").first.get_attribute("data-work-id")) == args.works
        skip.click()
        page.wait_for_function("id => document.querySelector('.reading-counter strong')?.textContent.includes(String(id).padStart(5,'0'))", arg=args.works - 1)
        assert page.evaluate("window.documentMarker") is None
        passed("feed-heart-before-skip-and-entire-work-skipping")

        page.goto(origin + f"/illust/{args.works - 1}")
        reading(page)
        heart = page.locator(".detail-heading .bookmark-button, .detail-header .bookmark-button").first
        if heart.count() == 0:
            heart = page.locator(".bookmark-button").first
        page.wait_for_timeout(1200)
        remote.entered.clear()
        remote.release.clear()
        before_writes = remote.write_count
        heart.click()
        assert remote.entered.wait(3)
        assert heart.get_attribute("aria-pressed") != "true"
        assert heart.is_disabled()
        page.keyboard.press("ArrowRight")
        page.wait_for_url(f"**/illust/{args.works - 2}*")
        remote.release.set()
        page.wait_for_timeout(1700)
        assert remote.write_count == before_writes + 1
        assert page.locator(".bookmark-button").first.get_attribute("aria-pressed") != "true"
        passed("bookmark-waits-for-fact-and-response-stays-with-original-work")

        page.get_by_role("navigation", name="主导航").get_by_role("button", name="画师", exact=True).click()
        page.locator(".artist-profile").first.wait_for()
        fit(page)
        assert page.locator("img.artist-avatar").count() > 0
        page.get_by_role("button", name="列表", exact=True).click()
        page.screenshot(path=str(artifacts / "desktop-artists-row.png"))
        strip = page.locator(".artist-recent-strip").first
        if strip.count():
            box = strip.bounding_box()
            page.mouse.move(box["x"] + box["width"] * .8, box["y"] + 50)
            page.mouse.down()
            page.mouse.move(box["x"] + 50, box["y"] + 55, steps=12)
            page.mouse.up()
            assert strip.evaluate("e=>e.scrollLeft") > 0
        passed("local-artist-profiles-and-draggable-recent-works")

        page.get_by_role("navigation", name="主导航").get_by_role("button", name="设置", exact=True).click()
        page.locator(".settings-page").wait_for()
        light, dark = page.get_by_role("button", name="浅色", exact=True), page.get_by_role("button", name="深色", exact=True)
        light.scroll_into_view_if_needed()
        for index in range(8):
            button = light if index % 2 == 0 else dark
            box = button.bounding_box()
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            assert page.locator("body").get_attribute("data-theme") == ("light" if index % 2 == 0 else "dark")
            page.wait_for_timeout(60)
        page.wait_for_timeout(350)
        assert page.locator(".theme-snapshot").count() == 0
        page.screenshot(path=str(artifacts / "desktop-settings.png"))
        page.reload()
        page.locator(".settings-page").wait_for()
        assert page.locator("body").get_attribute("data-theme") == "dark"
        passed("trusted-rapid-theme-clicks-and-reload-persistence")

        for width in (390, 320):
            phone = browser.new_context(viewport={"width": width, "height": 844}, is_mobile=True, has_touch=True)
            mobile = phone.new_page()
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            for path, label in (("/", "gallery"), (f"/illust/{args.works - 1}", "detail"), ("/artists", "artists"), ("/settings", "settings")):
                mobile.goto(origin + path)
                mobile.locator({"gallery": ".work-card", "detail": ".image-page", "artists": ".artist-profile", "settings": ".settings-card"}[label]).first.wait_for()
                fit(mobile)
                if label == "gallery":
                    mobile.wait_for_function("[...document.querySelectorAll('.work-card img')].slice(0,4).every(i=>i.complete&&i.naturalWidth>0)")
                mobile.screenshot(path=str(artifacts / f"phone-{width}-{label}.png"))
            light = mobile.get_by_role("button", name="浅色", exact=True)
            dark = mobile.get_by_role("button", name="深色", exact=True)
            light.scroll_into_view_if_needed()
            for index in range(6):
                box = (light if index % 2 == 0 else dark).bounding_box()
                mobile.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                assert mobile.locator("body").get_attribute("data-theme") == ("light" if index % 2 == 0 else "dark")
                mobile.wait_for_timeout(55)
            phone.close()
            passed(f"mobile-{width}-layout-and-rapid-theme-taps")
        if errors:
            raise AssertionError(errors)
        desktop.close()
        browser.close()
        browser = None
    except Exception:
        errors.append(traceback.format_exc())
        if page is not None:
            try:
                page.screenshot(path=str(artifacts / "failure.png"))
                (artifacts / "failure.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
        raise
    finally:
        remote.release.set()
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            playwright.stop()
        server.should_exit = True
        thread.join(20)
        metrics["elapsed_seconds"] = round(time.monotonic() - started, 2)
        (artifacts / "result.json").write_text(json.dumps({"checks": checks, "errors": errors, "metrics": metrics,
            "real_account_used": False, "real_library_used": False, "real_phone_tested": False}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
