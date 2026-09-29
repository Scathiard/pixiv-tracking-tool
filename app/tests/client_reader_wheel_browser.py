"""Native wheel reachability on synthetic gallery, detail and flow pages."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--width", type=int, default=2560)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "fixture")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    import uvicorn
    from playwright.sync_api import sync_playwright
    from client_fixture import make_library
    runtime, _ = make_library(artifacts / "fixture", 80)
    from src.auth import _find_system_browser
    from src.web import create_app
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True),
        host="127.0.0.1", port=port, log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    results = []
    errors = []
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            context = browser.new_context(viewport={"width": args.width, "height": 900})
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            origin = f"http://127.0.0.1:{port}"
            for path, selector, target in (("/", ".gallery-viewport", ".card-picture"),
                                           ("/illust/80", ".page-viewport", ".page-picture"),
                                           ("/flow", ".page-viewport", ".page-picture")):
                page.goto(origin + path)
                page.locator(target).first.wait_for()
                if path != "/":
                    page.locator(".image-page img").first.wait_for()
                pane = page.locator(selector)
                for position in ("image", "left", "right"):
                    pane.evaluate("e => e.scrollTop = 0")
                    page.wait_for_timeout(100)
                    box = pane.bounding_box()
                    picture = page.locator(target).first.bounding_box()
                    assert box and picture
                    y = min(box["y"] + box["height"] * .55, picture["y"] + picture["height"] * .4)
                    x = (picture["x"] + picture["width"] * .5 if position == "image" else
                         box["x"] + 5 if position == "left" else box["x"] + box["width"] - 8)
                    hit = page.evaluate("([x,y]) => {const e=document.elementFromPoint(x,y); return {className:String(e?.className || ''), inViewport:!!e?.closest('.page-viewport,.gallery-viewport')}}", [x, y])
                    page.mouse.move(x, y)
                    page.mouse.wheel(0, 500)
                    page.wait_for_timeout(180)
                    scroll = pane.evaluate("e => e.scrollTop")
                    results.append({"route": path, "position": position, "scrollTop": scroll,
                                    "hit": hit, "x": round(x), "y": round(y)})
                if path != "/":
                    for band, y in (("header", box["y"] + 45), ("lower", box["y"] + box["height"] - 35)):
                        for position, x in (("left", box["x"] + 5),
                                            ("right", box["x"] + box["width"] - 8)):
                            pane.evaluate("e => e.scrollTop = 0")
                            page.wait_for_timeout(80)
                            hit = page.evaluate("([x,y]) => {const e=document.elementFromPoint(x,y); return {className:String(e?.className || ''), inViewport:!!e?.closest('.page-viewport')}}", [x, y])
                            page.mouse.move(x, y)
                            page.mouse.wheel(0, 500)
                            page.wait_for_timeout(180)
                            results.append({"route": path, "position": f"{band}-{position}",
                                            "scrollTop": pane.evaluate("e => e.scrollTop"), "hit": hit,
                                            "x": round(x), "y": round(y)})
                else:
                    shell = page.locator(".gallery-layout").bounding_box()
                    assert shell
                    for position, x in (("outer-left", shell["x"] + 8),
                                        ("outer-right", shell["x"] + shell["width"] - 8)):
                        pane.evaluate("e => e.scrollTop = 0")
                        page.wait_for_timeout(80)
                        hit = page.evaluate("([x,y]) => {const e=document.elementFromPoint(x,y); return {className:String(e?.className || ''), inViewport:!!e?.closest('.gallery-viewport')}}", [x, y])
                        page.mouse.move(x, y)
                        page.mouse.wheel(0, 500)
                        page.wait_for_timeout(180)
                        results.append({"route": path, "position": position,
                                        "scrollTop": pane.evaluate("e => e.scrollTop"), "hit": hit,
                                        "x": round(x), "y": round(y)})
                page.screenshot(path=str(artifacts / f"{path.strip('/') or 'gallery'}-wheel.png"))
            context.close()
            mobile = browser.new_context(viewport={"width": 390, "height": 844},
                device_scale_factor=2, is_mobile=True, has_touch=True)
            touch_page = mobile.new_page()
            touch_page.on("pageerror", lambda error: errors.append(str(error)))
            touch_page.goto(origin + "/flow")
            touch_page.locator(".image-page").first.wait_for()
            touch_pane = touch_page.locator(".page-viewport")
            cdp = mobile.new_cdp_session(touch_page)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [
                {"x": 320, "y": 670, "id": 1}]})
            for step in range(1, 9):
                cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [
                    {"x": 320, "y": 670 - step * 48, "id": 1}]})
                touch_page.wait_for_timeout(14)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            touch_page.wait_for_timeout(300)
            results.append({"route": "/flow-mobile", "position": "touch-right",
                            "scrollTop": touch_pane.evaluate("e => e.scrollTop"),
                            "horizontal_fit": touch_page.evaluate("document.documentElement.scrollWidth <= innerWidth")})
            touch_page.screenshot(path=str(artifacts / "flow-mobile-touch.png"))
            cdp.detach()
            mobile.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(20)
        (artifacts / "result.json").write_text(json.dumps({"results": results, "errors": errors,
            "real_library_used": False, "real_account_used": False}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))
    assert not errors, errors
    assert all(item["scrollTop"] > 0 for item in results), results
    assert results[-1]["horizontal_fit"], results[-1]


if __name__ == "__main__":
    main()
