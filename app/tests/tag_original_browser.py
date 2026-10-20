"""Focused compiled-UI check with three local synthetic works; no real account."""
import argparse
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

parser = argparse.ArgumentParser()
parser.add_argument("--dist", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
out = args.out.resolve()
out.mkdir(parents=True, exist_ok=False)
os.environ.update(TEMP=str(out), TMP=str(out), DATA_ROOT=str(out / "configuration"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stream_reader_browser import tiny_library
from src.core.models import Work, WorkTag
from src.web import create_app
from src.auth import _find_system_browser
from playwright.sync_api import sync_playwright, expect
import uvicorn

runtime = tiny_library(out / "fixture")
with runtime.database.write() as session:
    for identity in (1, 2, 3):
        session.add(WorkTag(work_id=identity, tag="zzz"))
    session.add(WorkTag(work_id=1, tag="aaa"))
    session.get(Work, 1).source_metadata = {"tag_translations": {"zzz": {
        "translated_name": "官方测试译名", "source": "pixiv", "language": "zh-cn",
        "checked_at": "2026-10-21T00:00:00Z"}}}
with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
origin = f"http://127.0.0.1:{port}"
server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False,
    schedule=False, reading_ui=False), host="127.0.0.1", port=port,
    loop="src.networking:http_loop_factory", log_level="error"))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()
result = {"checks": [], "errors": [], "real_library": False, "real_account": False,
          "real_phone": False, "metrics": {}}
held = []
control = {"hold": None}
requests = []
browser = None

def route_request(route):
    path = urlsplit(route.request.url).path
    if path.startswith("/reader/assets/"):
        asset = args.dist / path.removeprefix("/reader/")
        route.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(asset)[0] or "application/octet-stream")
    elif route.request.is_navigation_request():
        route.fulfill(body=(args.dist / "index.html").read_bytes(), content_type="text/html")
    elif path == control["hold"]:
        held.append(route)
    else:
        route.continue_()

def context_page(browser, **options):
    context = browser.new_context(**options)
    page = context.new_page()
    page.set_default_timeout(10000)
    page.route("**/*", route_request)
    page.on("pageerror", lambda error: result["errors"].append(str(error)))
    page.on("request", lambda request: requests.append(urlsplit(request.url).path))
    return context, page

def open_tags(page):
    page.get_by_role("button", name="筛选", exact=True).click()
    page.get_by_role("button", name=re.compile("^标签 ")).click()
    expect(page.locator(".filter-candidate")).to_have_count(2)
    return page.locator(".filter-candidate").evaluate_all("els=>els.map(e=>e.dataset.value)")

try:
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(.05)
    assert server.started
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
        context, page = context_page(browser, viewport={"width": 1440, "height": 900})
        page.goto(origin)
        expect(page.locator(".work-card")).to_have_count(3)
        assert open_tags(page) == ["zzz", "aaa"]
        expect(page.locator(".filter-candidate small[data-source=pixiv]")).to_have_text("官方测试译名")
        before = requests.count("/api/library/tags")
        page.get_by_role("button", name="关闭筛选").click()
        page.get_by_title("设置", exact=True).click()
        page.get_by_label("筛选标签排序", exact=True).select_option("name")
        page.get_by_title("作品", exact=True).click()
        expect(page.locator(".work-card")).to_have_count(3)
        assert open_tags(page) == ["aaa", "zzz"]
        assert requests.count("/api/library/tags") == before, "Changing order must reuse the catalogue"
        page.get_by_role("button", name="关闭筛选").click()
        page.get_by_title("设置", exact=True).click()
        page.reload()
        expect(page.get_by_label("筛选标签排序", exact=True)).to_have_value("name")
        result["checks"].append("default-count-order-settings-switch-cache-reuse-and-reload")
        result["metrics"]["tag_counts"] = context.request.get(origin + "/api/library/tags?show_hidden=1").json()["tag_counts"]
        assert result["metrics"]["tag_counts"] == {"aaa": 1, "zzz": 3}, "Multi-page work counts once"
        result["checks"].append("real-synthetic-tag-count-and-official-translation")
        details = {identity: context.request.get(origin + f"/api/works/{identity}?page_limit=10").json()
                   for identity in (1, 2, 3)}
        originals = {identity: urlsplit(detail["pages"][0]["src"]).path for identity, detail in details.items()}
        previews = {urlsplit(image["preview"]).path for detail in details.values()
                    for image in detail["pages"] if image.get("preview")}
        context.close()
        requests.clear()
        context, page = context_page(browser, viewport={"width": 1440, "height": 900})
        control["hold"] = originals[2]
        page.goto(origin + "/illust/3")
        expect(page.locator(".detail-head")).to_contain_text("R074 synthetic work 3")
        expect(page.locator(".reading-activity")).to_have_count(0)
        image = page.locator(".page-window .media-image img").first
        expect(image).to_have_attribute("src", details[3]["pages"][0]["src"])
        page.get_by_role("button", name="下一件作品", exact=True).click()
        expect(page.locator(".reading-activity")).to_contain_text("正在准备图片")
        expect(image).to_have_attribute("src", details[3]["pages"][0]["src"])
        assert not previews.intersection(requests), "Reader requested a thumbnail before the held original"
        assert held, "The target original was not held"
        control["hold"] = None
        for route in held:
            route.continue_()
        held.clear()
        expect(page.locator(".detail-head").filter(has_text="R074 synthetic work 2")).to_be_visible()
        expect(page.locator(".detail-head")).to_have_count(1)
        expect(page.locator(".page-window .media-image img").first).to_have_attribute("src", details[2]["pages"][0]["src"])
        result["checks"].append("ordinary-navigation-retains-current-until-original-no-thumbnail")
        page.get_by_title("流式阅览", exact=True).click()
        expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "2")
        expect(page.locator(".stream-viewport")).to_have_attribute("aria-busy", "false")
        page.locator(".stream-viewport").press("ArrowRight")
        expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "1")
        expect(page.locator(".stream-viewport")).to_have_attribute("aria-busy", "false")
        sources = page.locator(".stream-window .media-image img").evaluate_all("els=>els.map(e=>e.getAttribute('src'))")
        assert sources and all(urlsplit(src).path == originals[1] for src in sources)
        assert not previews.intersection(requests), "Stream or adjacent preload requested a thumbnail"
        result["checks"].append("stream-entry-navigation-and-adjacent-preload-original-only")
        context.close()
        context, page = context_page(browser, viewport={"width": 390, "height": 844},
                                    is_mobile=True, has_touch=True, device_scale_factor=1)
        page.goto(origin + "/flow")
        expect(page.locator(".stream-viewport")).to_have_attribute("aria-busy", "false")
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            assert not page.evaluate("document.documentElement.scrollWidth>innerWidth")
            page.screenshot(path=str(out / f"stream-{width}.png"))
        page.goto(origin + "/settings")
        expect(page.get_by_label("筛选标签排序", exact=True)).to_have_value("count")
        page.get_by_label("筛选标签排序", exact=True).scroll_into_view_if_needed()
        assert not page.evaluate("document.documentElement.scrollWidth>innerWidth")
        page.screenshot(path=str(out / "settings-320.png"))
        result["checks"].append("mobile-390-320-reader-and-settings-geometry")
        context.close()
        browser.close()
        browser = None
except Exception as error:
    result["errors"].append(str(error))
    if browser:
        try:
            page.screenshot(path=str(out / "failure.png"))
        except Exception:
            pass
finally:
    for route in held:
        try:
            route.abort()
        except Exception:
            pass
    if browser:
        try:
            browser.close()
        except Exception:
            pass
    server.should_exit = True
    thread.join(timeout=8)
    runtime.database.engine.dispose()
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False), flush=True)
sys.exit(1 if result["errors"] else 0)
