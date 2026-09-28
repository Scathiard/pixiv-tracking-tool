"""Isolated Chromium check for the v0.2.5 feed layout."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

from PIL import Image
from playwright.sync_api import sync_playwright
import uvicorn


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".worktrees/v0.2.5/app"))
sys.path.insert(0, str(ROOT / ".worktrees/v0.2.5/app/tests"))


def png(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), "#8095b3").save(output, format="PNG")
    return output.getvalue()


def main() -> None:
    artifacts = ROOT / f".devtools/test-runs/v025-flow-layout-browser-{int(time.time())}"
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "fixture")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    os.environ["PORT"] = str(port)
    from client_fixture import make_library
    runtime, _ = make_library(artifacts / "fixture", 10)
    from src.auth import _find_system_browser
    from src.web import create_app
    server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True),
        host="127.0.0.1", port=port, log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
            for width in (1440, 390, 320):
                context = browser.new_context(viewport={"width": width, "height": 900})
                page = context.new_page()
                errors = []
                mutated = [0]
                page.on("pageerror", lambda error: errors.append(str(error)))
                landscape, long = png(1200, 400), png(200, 2000)

                def replace_image(route):
                    url = route.request.url
                    if "100010_p0.png" in url:
                        route.fulfill(body=landscape, content_type="image/png")
                    elif "100010_p1.png" in url:
                        route.fulfill(body=long, content_type="image/png")
                    else:
                        route.continue_()

                def unknown_dimensions(route):
                    response = route.fetch()
                    payload = response.json()
                    for item in payload.get("items", []):
                        if item["work_id"] == 10 and item["page"] == 0:
                            item["width"] = item["height"] = None
                            mutated[0] += 1
                    route.fulfill(response=response, body=json.dumps(payload))

                page.route("**/*_p*.png", replace_image)
                page.route("**/api/library/feed?*", unknown_dimensions)
                page.goto(f"http://127.0.0.1:{port}/illust/10")
                page.locator(".image-page img").first.wait_for()
                page.get_by_role("navigation", name="主导航").get_by_role("button", name="流式", exact=False).click()
                page.locator(".page-viewport.feed").wait_for()
                page.locator(".page-viewport.reader").wait_for(state="detached")
                first = page.locator(".feed .image-page[data-work-id='10'][data-page='0']")
                first.wait_for()
                page.wait_for_function("""() => {
                    const f = document.querySelector('.feed .image-page[data-work-id="10"][data-page="0"]');
                    const i = f?.querySelector('img');
                    return i?.complete && i.naturalWidth === 1200 &&
                        Math.abs(f.querySelector('.page-picture').clientHeight -
                            f.querySelector('.page-picture').clientWidth / 3) < 3;
                }""")
                assert first.locator(".page-title").count() == 1
                assert mutated[0] > 0
                assert first.locator(".restriction-badge").count() <= 1
                second = page.locator(".feed .image-page[data-work-id='10'][data-page='1']")
                second.wait_for()
                page.wait_for_function("""() => {
                    const f = document.querySelector('.feed .image-page[data-work-id="10"][data-page="1"]');
                    const i = f?.querySelector('img');
                    return i?.complete && i.naturalWidth === 200 &&
                        Math.abs(f.querySelector('.page-picture').clientHeight -
                            f.querySelector('.page-picture').clientWidth * 10) < 3;
                }""")
                assert second.locator(".page-title, .restriction-badge").count() == 0
                assert "2 / 2000" in second.locator("figcaption").inner_text()
                assert second.get_by_role("button", name="放大此页").count() == 1
                page.screenshot(path=str(artifacts / f"feed-{width}.png"))
                if width == 1440:
                    for resized in (761, 760, 759, 733, 732, 1440):
                        page.set_viewport_size({"width": resized, "height": 900})
                        page.wait_for_function("""() => {
                            const f = document.querySelector('.feed .image-page[data-page="0"]');
                            const p = f?.querySelector('.page-picture');
                            const i = f?.querySelector('img');
                            return i?.naturalWidth === 1200 &&
                                Math.abs(p.clientHeight - p.clientWidth * i.naturalHeight / i.naturalWidth) < 3;
                        }""")
                    print("responsive: 761/760/759/733/732px preserved decoded image ratio")
                    viewport = page.locator(".page-viewport.feed")
                    for _ in range(65):
                        viewport.evaluate("e => e.scrollBy(0, 3200)")
                        page.wait_for_timeout(80)
                    page.wait_for_timeout(500)
                    window = int(page.locator(".feed .page-window").get_attribute("data-window-size"))
                    first_index = int(page.locator(".feed .image-page").first.get_attribute("data-global-index"))
                    assert first_index > 40, first_index
                    assert window <= 120, window
                    assert page.locator(".feed .image-page").count() < 16
                    assert page.locator(".feed .page-title").count() == 0
                    page.screenshot(path=str(artifacts / "feed-long-work-window.png"))
                    print(f"long work: visible page {first_index}, metadata window {window}, no repeated title")
                    page.reload()
                    page.locator(".page-viewport.feed").wait_for()
                    page.wait_for_function("""() => {
                        const f = document.querySelector('.feed .image-page');
                        return f && Number(f.dataset.globalIndex) > 40;
                    }""")
                    assert int(page.locator(".feed .page-window").get_attribute("data-window-size")) <= 120
                    print("reload: long work resumed beyond page 40 with bounded metadata")
                assert not errors, errors
                context.close()
                print(f"{width}px: proportional landscape and long images, shared title, page controls")
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
