"""Actual CDP touch events against an isolated real backend, never Pixiv."""
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
    parser.add_argument("--artifacts", type=Path, required=True)
    out = parser.parse_args().artifacts.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(out / "configuration")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from client_fixture import make_library
    from src.auth import _find_system_browser
    from src.web import create_app
    from playwright.sync_api import sync_playwright
    import uvicorn

    runtime, remote = make_library(out / "fixture", works=72)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True),
        host="127.0.0.1", port=port, log_level="error", loop="src.networking:http_loop_factory"))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    result = {"checks": [], "errors": [], "failures": [], "real_account_used": False,
              "real_phone_tested": False, "input": "Chromium CDP touch", "backend": "real isolated runtime"}
    page = None
    try:
        deadline = time.monotonic() + 25
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            context = browser.new_context(viewport={"width": 390, "height": 900},
                                          is_mobile=True, has_touch=True, device_scale_factor=1)
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: result["errors"].append(str(error)))
            cdp = context.new_cdp_session(page)

            def event(kind, points):
                cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [
                    {"x": x, "y": y, "id": index, "radiusX": 2, "radiusY": 2}
                    for index, (x, y) in enumerate(points)]})

            def tap(x, y):
                event("touchStart", [(x, y)])
                page.wait_for_timeout(40)
                event("touchEnd", [])

            def double(x, y):
                tap(x, y)
                page.wait_for_timeout(80)
                tap(x, y)

            def image_point():
                for image in page.locator(".image-page img").all():
                    box = image.bounding_box()
                    if box and box["y"] < 700 and box["y"] + box["height"] > 220:
                        return box["x"] + box["width"] / 2, min(600, max(230, box["y"] + 180))
                raise AssertionError("No visible image for touch interaction")

            page.goto(f"http://127.0.0.1:{port}/flow?types=illust")
            page.locator(".image-page img").first.wait_for()
            page.wait_for_timeout(400)
            viewport = page.locator(".page-viewport")
            if not viewport.count():
                viewport = page.locator(".reader-viewport")
            assert viewport.count() == 1
            baseline = remote.write_count
            initial_top = viewport.evaluate("e=>e.scrollTop")
            x, y = image_point()
            event("touchStart", [(x, y)])
            for step in range(1, 9):
                event("touchMove", [(x, y - step * 24)])
                page.wait_for_timeout(20)
            event("touchEnd", [])
            page.wait_for_timeout(700)
            assert viewport.evaluate("e=>e.scrollTop") > initial_top + 40
            assert remote.write_count == baseline
            assert page.get_by_role("dialog", name="放大查看").count() == 0
            result["checks"].append("swipe-scrolls-without-bookmark-or-zoom")

            x, y = image_point()
            event("touchStart", [(x - 30, y), (x + 30, y)])
            event("touchMove", [(x - 60, y), (x + 60, y)])
            event("touchEnd", [])
            page.wait_for_timeout(400)
            assert remote.write_count == baseline
            assert page.get_by_role("dialog", name="放大查看").count() == 0
            result["checks"].append("multi-touch-does-not-bookmark-or-open")

            x, y = image_point()
            event("touchStart", [(x, y)])
            page.wait_for_timeout(620)
            event("touchEnd", [])
            page.wait_for_timeout(350)
            assert remote.write_count == baseline
            assert page.get_by_role("dialog", name="放大查看").count() == 0
            result["checks"].append("long-press-does-not-bookmark-or-open")

            anchor_top = viewport.evaluate("e=>e.scrollTop")
            counter = page.locator(".reading-counter").inner_text()
            x, y = image_point()
            tap(x, y)
            page.get_by_role("dialog", name="放大查看").wait_for()
            event("touchStart", [(150, 400), (230, 400)])
            event("touchMove", [(105, 400), (275, 400)])
            event("touchEnd", [])
            page.wait_for_timeout(100)
            scale = page.locator(".zoom-toolbar button").nth(1).inner_text()
            assert int(scale.rstrip("%")) > 150, scale
            page.get_by_role("button", name="关闭放大", exact=False).tap()
            assert page.get_by_role("dialog", name="放大查看").count() == 0
            assert abs(viewport.evaluate("e=>e.scrollTop") - anchor_top) < 2
            assert page.locator(".reading-counter").inner_text() == counter
            assert remote.write_count == baseline
            result["checks"].append("single-touch-opens-pinch-scales-close-retains-anchor")

            x, y = image_point()
            double(x, y)
            page.locator(".tap-heart").wait_for()
            deadline = time.monotonic() + 8
            while remote.write_count == baseline and time.monotonic() < deadline:
                page.wait_for_timeout(50)
            assert remote.write_count == baseline + 1
            assert page.get_by_role("dialog", name="放大查看").count() == 0
            assert remote.values and all(remote.values.values())
            page.wait_for_timeout(3200)
            double(x, y)
            page.wait_for_timeout(700)
            assert remote.write_count == baseline + 1
            assert all(remote.values.values())
            result["checks"].append("double-touch-adds-once-and-never-unbookmarks")

            page.set_viewport_size({"width": 320, "height": 900})
            page.wait_for_timeout(400)
            toolbar = page.locator(".reading-toolbar").bounding_box()
            assert toolbar and toolbar["x"] >= 0 and toolbar["x"] + toolbar["width"] <= 320.5
            for button in page.locator(".reading-toolbar button").all():
                box = button.bounding_box()
                assert box and box["width"] >= 40 and box["height"] >= 40
            assert page.locator("html").evaluate("e=>e.scrollWidth") <= 320
            page.screenshot(path=str(out / "touch-320.png"))
            result["checks"].append("320px-toolbar-visible-and-touch-targets-40px")
            assert not result["errors"], result["errors"]
            context.close()
            browser.close()
    except Exception:
        result["failures"].append(traceback.format_exc())
        if page:
            try:
                page.screenshot(path=str(out / "failure.png"))
            except Exception:
                pass
        raise
    finally:
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        server.should_exit = True
        worker.join(15)
        if worker.is_alive():
            raise RuntimeError("Isolated server failed to stop")


if __name__ == "__main__":
    main()
