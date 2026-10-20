"""Small real UI loop; three local synthetic works, no user data or network."""
import argparse
from collections import Counter
import json
import mimetypes
import os
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.parse import urlsplit, parse_qs

root = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument("--dist", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--pages-only", action="store_true")
parser.add_argument("--extension-only", action="store_true")
parser.add_argument("--identity-only", action="store_true")
args = parser.parse_args()
args.out.mkdir(parents=True, exist_ok=False)
os.environ["TEMP"] = os.environ["TMP"] = str(args.out.resolve())
os.environ["DATA_ROOT"] = str(args.out.resolve() / "config")
sys.path[:0] = [str(root / "app"), str(root / "app/tests")]
from stream_reader_browser import tiny_library
from src.web import create_app
from src.auth import _find_system_browser
from playwright.sync_api import sync_playwright, expect
import uvicorn

result = {"checks": [], "failures": [], "metrics": {}, "real_account": False, "real_library": False}
runtime = tiny_library(args.out / "fixture")
from src.core.models import Work, WorkTag, Asset
from sqlalchemy import select
with runtime.database.write() as session:
    work = session.get(Work, 2)
    work.caption = "<p>Full local caption.</p>" * 60
    work.source_metadata = {"tag_translations": {"試験": {"translated_name": "测试标签",
        "source": "pixiv", "language": "zh-cn", "checked_at": "2026-10-20T00:00:00Z"}}}
    session.add(WorkTag(work_id=2, tag="試験"))
    if args.extension_only:
        # 64 initial pages, then 16 more: zoom can be established before extension.
        session.get(Work, 1).page_count = 80
        source = session.scalar(select(Asset).where(Asset.work_id == 1, Asset.page_index == 0))
        for index in range(3, 80):
            session.add(Asset(work_id=1, role="image", page_index=index,
                relative_path=source.relative_path, state="verified", width=640, height=860,
                sha256=source.sha256, byte_size=source.byte_size,
                file_mtime_ns=source.file_mtime_ns, file_identity=source.file_identity))
extension_started, extension_release = threading.Event(), threading.Event()
if args.extension_only:
    actual_feed = runtime.library.cursor_feed
    def hold_extension(*positional, **keywords):
        if keywords.get("limit") == 16:
            extension_started.set()
            if not extension_release.wait(10):
                raise TimeoutError("Test did not release the controlled page-extension read")
        return actual_feed(*positional, **keywords)
    runtime.library.cursor_feed = hold_extension
with socket.socket() as port:
    port.bind(("127.0.0.1", 0))
    number = port.getsockname()[1]
origin = f"http://127.0.0.1:{number}"
server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=False),
    host="127.0.0.1", port=number, log_level="error", loop="src.networking:http_loop_factory"))
thread = threading.Thread(target=server.run, daemon=True)
thread.start()

def scenario(name, fn):
    try:
        result["metrics"][name] = fn()
        result["checks"].append(name)
        print("PASS", name, flush=True)
    except Exception as error:
        result["failures"].append({"name": name, "error": str(error)})
        if args.pages_only or args.extension_only or args.identity_only:
            result["failure_state"] = page.evaluate("""()=>({active:{tag:document.activeElement?.tagName,classes:document.activeElement?.className},
                detail:document.querySelector('.reading-page-count')?.textContent,
                stream:document.querySelector('.stream-counter')?.textContent,
                activity:document.querySelector('.reading-activity')?.textContent,
                busy:document.querySelector('.stream-viewport')?.getAttribute('aria-busy'),
                window:document.querySelector('.stream-window')?.dataset.windowSize,
                zoom:[...document.querySelectorAll('.inline-zoom-media')].map(e=>({
                    page:e.closest('[data-page]')?.dataset.page,scale:e.dataset.inlineZoomScale}))})""")
            result["feed_requests"] = [url for url in urls if "/api/library/cursor/feed?" in url]
            page.screenshot(path=str(args.out / f"{name}-failure.png"))
        print("FAIL", name, str(error), flush=True)

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.set_default_timeout(8000)
        counts = Counter()
        urls = []
        completed_feeds = []
        page.on("pageerror", lambda error: result.setdefault("errors", []).append(str(error)))
        page.on("response", lambda response: completed_feeds.append(response.url)
            if response.status == 200 and urlsplit(response.url).path == "/api/library/cursor/feed" else None)
        def request(route):
            path = urlsplit(route.request.url).path
            if path.startswith("/api/"):
                counts[path] += 1
                urls.append(route.request.url)
                route.continue_()
            elif path.startswith("/reader/assets/"):
                asset = args.dist / path.removeprefix("/reader/")
                route.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(asset)[0] or "application/octet-stream")
            elif route.request.is_navigation_request():
                route.fulfill(body=(args.dist / "index.html").read_bytes(), content_type="text/html")
            else:
                route.continue_()
        page.route("**/*", request)
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        page.goto(origin)
        expect(page.locator(".work-card")).to_have_count(3)
        if args.identity_only:
            def identity_previous():
                page.locator('.work-card[data-work-id="2"] .card-open').click()
                expect(page.locator(".reading-page-count")).to_contain_text("1 / 1")
                expect(page.locator(".page-window")).to_have_attribute("data-window-size", "1")
                expect(page.locator(".reading-activity")).to_have_count(0)
                page.get_by_title("流式阅览", exact=True).click()
                expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "2")
                expect(page.locator(".stream-viewport")).to_have_attribute("aria-busy", "false")
                def prepared(url):
                    query = parse_qs(urlsplit(url).query)
                    return query.get("anchor_work_id") == ["3"] and query.get("index") == ["0"] and query.get("limit") == ["4"]
                deadline = time.monotonic() + 5
                while not any(prepared(url) for url in completed_feeds) and time.monotonic() < deadline:
                    page.wait_for_timeout(25)
                assert any(prepared(url) for url in completed_feeds), "The rebased current work never prepared its predecessor identity"
                page.wait_for_timeout(250)  # Let the already local image decode and install the single opening.
                before = sum(prepared(url) for url in urls)
                foreground_before = sum(parse_qs(urlsplit(url).query).get("limit") == ["64"] for url in completed_feeds)
                neighbors_before = counts["/api/works/2/cursor-neighbors"]
                page.locator(".stream-viewport").press("ArrowLeft")
                expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "3")
                expect(page.locator(".stream-viewport")).to_have_attribute("aria-busy", "false")
                assert sum(prepared(url) for url in urls) == before, "Previous-work navigation re-read the prepared predecessor"
                assert counts["/api/works/2/cursor-neighbors"] == neighbors_before, "Previous navigation repeated foreground identity discovery"
                assert sum(parse_qs(urlsplit(url).query).get("limit") == ["64"] for url in completed_feeds) == foreground_before, "The new identity session re-read a foreground 64-page window"
                return {"prepared_previous": 3, "opened_previous": 3, "foreground_64_reads": 0, "repeated_identity_reads": 0}
            scenario("rebased-predecessor-opening-reused-with-new-session", identity_previous)
            browser.close()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            sys.exit(1 if result["failures"] else 0)
        if args.extension_only:
            def extension_zoom():
                page.locator('.work-card[data-work-id="1"] .card-open').click()
                expect(page.locator(".reading-page-count")).to_contain_text("1 / 80")
                expect(page.locator(".page-window")).to_have_attribute("data-window-size", "64")
                expect(page.locator(".reading-activity")).to_have_count(0)
                page.get_by_title("流式阅览", exact=True).click()
                expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "1")
                viewport = page.locator(".stream-viewport")
                expect(page.locator(".stream-window")).to_have_attribute("data-window-size", "64")
                expect(viewport).to_have_attribute("aria-busy", "false")
                step = page.locator('.stream-page[data-page="0"]').evaluate("e=>e.getBoundingClientRect().height+16")
                viewport.evaluate("(e,step)=>e.scrollTop=step*50", step)
                expect(page.locator(".stream-counter")).to_contain_text("51 / 80")
                zoom = page.locator('.stream-page[data-page="50"] .inline-zoom-media')
                box = viewport.bounding_box()
                page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + 80)
                expect(zoom).to_have_attribute("data-inline-zoom-scale", "2.25")
                zoom.evaluate("e=>window.__extensionZoomNode=e")
                viewport.evaluate("(e,step)=>e.scrollTop=step*51", step)
                # Event.wait blocks Playwright's route callbacks; pump its event loop instead.
                deadline = time.monotonic() + 5
                while not extension_started.is_set() and time.monotonic() < deadline:
                    page.wait_for_timeout(25)
                assert extension_started.is_set(), "Expected limit=16 extension after entering page 52 of the initial 64"
                expect(viewport).to_have_attribute("aria-busy", "true")
                result["metrics"]["extension_setup"] = {"initial_window": 64, "total_pages": 80,
                    "trigger_page": 52, "zoom_page": 51, "held_limit": 16,
                    "scale_during_extension": zoom.get_attribute("data-inline-zoom-scale")}
                try:
                    assert zoom.evaluate("e=>e===window.__extensionZoomNode"), "Virtualization replaced the zoom target"
                    expect(zoom).to_have_attribute("data-inline-zoom-scale", "2.25")
                finally:
                    extension_release.set()
                expect(viewport).to_have_attribute("aria-busy", "false")
                expect(zoom).to_have_attribute("data-inline-zoom-scale", "2.25")
            scenario("background-page-extension-preserves-live-zoom", extension_zoom)
            browser.close()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            sys.exit(1 if result["failures"] else 0)
        if args.pages_only:
            def page_origin():
                page.locator('.work-card[data-work-id="1"] .card-open').click()
                expect(page.locator(".reading-page-count")).to_contain_text("1 / 3")
                # The toolbar renders before the reader has loaded its page count and restored.
                expect(page.locator(".page-window")).to_have_attribute("data-window-size", "3")
                expect(page.locator(".reading-activity")).to_have_count(0)
                page.locator(".page-viewport").press("ArrowDown")
                expect(page.locator(".reading-page-count")).to_contain_text("2 / 3")
                page.get_by_title("流式阅览", exact=True).click()
                expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "1")
                expect(page.locator(".stream-counter")).to_contain_text("2 / 3")
                page.wait_for_timeout(1600)
                for order in ("random", "sequential", "random"):
                    page.get_by_label("流式阅览顺序", exact=True).select_option(order)
                    expect(page.get_by_label("流式阅览顺序", exact=True)).to_have_value(order)
                    expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "1")
                    expect(page.locator(".stream-counter")).to_contain_text("2 / 3")
                assert counts["/api/reading-sessions"] == 1, dict(counts)
            scenario("detail-page-two-and-repeated-random-toggle-retain-page", page_origin)
            browser.close()
            print(json.dumps(result, ensure_ascii=False), flush=True)
            sys.exit(1 if result["failures"] else 0)
        def entry():
            page.locator('.work-card[data-work-id="2"] .card-open').click()
            expect(page).to_have_url(__import__("re").compile(r".*/illust/2.*"))
            page.get_by_title("流式阅览", exact=True).click()
            expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "2")
            return {"opened": 2, "stream": 2}
        scenario("detail-enters-current-work", entry)
        def random_ready():
            page.wait_for_timeout(1800)
            assert counts["/api/reading-sessions"] > 0, "No random session prepared before choosing random"
            return {"created_before_click": counts["/api/reading-sessions"]}
        scenario("random-prepared-before-click", random_ready)
        def nearby():
            foreground = lambda: sum(urlsplit(url).path == "/api/library/cursor/feed" and
                parse_qs(urlsplit(url).query).get("limit") == ["64"] for url in urls)
            before = foreground()
            page.keyboard.press("ArrowRight")
            expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "1")
            assert foreground() == before, "Next-work switch made another foreground 64-page read"
            page.keyboard.press("ArrowLeft")
            expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "2")
            return {"foreground_reads_during_next": foreground() - before}
        scenario("neighbor-opening-reused", nearby)
        def order():
            before = counts["/api/reading-sessions"]
            started = time.monotonic()
            page.get_by_label("流式阅览顺序", exact=True).select_option("random")
            expect(page.get_by_label("流式阅览顺序", exact=True)).to_have_value("random")
            expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "2")
            page.wait_for_timeout(200)
            assert counts["/api/reading-sessions"] == before, "Choosing random created a new visit"
            page.get_by_label("流式阅览顺序", exact=True).select_option("sequential")
            expect(page.get_by_label("流式阅览顺序", exact=True)).to_have_value("sequential")
            return {"synthetic_switch_ms": round((time.monotonic() - started) * 1000)}
        scenario("random-toggle-reuses-prepared-visit-and-current-work", order)
        def no_buttons():
            assert page.locator(".stream-navigation button").count() == 0, "Stream still has previous/next buttons"
        scenario("stream-no-navigation-buttons", no_buttons)
        def zoom():
            image = page.locator(".stream-viewport img").first
            expect(image).to_be_visible()
            image.dblclick()
            scale = float(page.locator(".inline-zoom-media").first.get_attribute("data-inline-zoom-scale"))
            assert 1 <= scale < 2.25, f"Zoom jumped to final scale: {scale}"
            page.wait_for_timeout(230)
            assert float(page.locator(".inline-zoom-media").first.get_attribute("data-inline-zoom-scale")) == 2.25
            image.dblclick()
            page.wait_for_timeout(230)
            expect(page.locator(".inline-zoom-media").first).to_have_attribute("data-inline-zoom-scale", "1")
            return {"immediate_scale": scale, "final_scale": 2.25}
        scenario("double-click-has-visible-interpolation", zoom)
        def drag():
            box = page.locator(".stream-caption-trigger").bounding_box()
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x, y - 160, steps=8)
            expect(page.locator("#stream-caption-sheet")).to_be_visible()
            partial = page.locator("#stream-caption-sheet").evaluate("(e)=>getComputedStyle(e).transform")
            assert partial != "matrix(1, 0, 0, 1, 0, 0)", partial
            page.mouse.up()
            page.wait_for_timeout(230)
            expect(page.locator(".stream-caption-trigger")).to_have_attribute("aria-expanded", "true")
            content = page.locator(".stream-details")
            content.hover()
            page.mouse.wheel(0, 220)
            page.wait_for_timeout(200)
            assert content.evaluate("(e)=>e.scrollTop") > 0
            handle = page.locator(".stream-sheet-handle").bounding_box()
            x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
            page.mouse.move(x, y); page.mouse.down(); page.mouse.move(x, y + 150, steps=8); page.mouse.up()
            expect(page.locator("#stream-caption-sheet")).to_have_count(0)
            return {"while_held_transform": partial}
        scenario("mouse-sheet-follows-drag-and-content-scrolls", drag)
        counts.clear()
        page.goto(origin)
        expect(page.locator(".work-card")).to_have_count(3)
        def filters():
            for _ in range(2):
                page.get_by_role("button", name="筛选", exact=True).click()
                expect(page.get_by_role("dialog", name="筛选作品")).to_be_visible()
                page.get_by_role("button", name=__import__("re").compile("^标签 ")).click()
                expect(page.locator(".filter-candidate").filter(has_text="测试标签")).to_be_visible()
                page.get_by_role("button", name="关闭筛选").click()
            total = counts["/api/library/tags"]
            assert total == 1, f"Opened twice: tags requests={total}, expected one reusable result"
            return {"tag_requests_for_two_opens": total}
        scenario("filter-reopen-reuses-result", filters)
        page.locator('.work-card[data-work-id="2"] .card-open').click()
        expect(page.locator(".detail-head")).to_be_visible()
        assert page.locator(".stream-shell").count() == 0
        result["checks"].append("ordinary-detail-still-separate")
        mobile = browser.new_context(viewport={"width": 390, "height": 844},
            is_mobile=True, has_touch=True, device_scale_factor=1)
        page = mobile.new_page()
        page.set_default_timeout(8000)
        page.route("**/*", request)
        page.goto(origin + "/flow")
        expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "3")
        cdp = mobile.new_cdp_session(page)
        def swipe(x, y, delta):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 1}]})
            for step in range(1, 9):
                cdp.send("Input.dispatchTouchEvent", {"type": "touchMove",
                    "touchPoints": [{"x": x, "y": y + delta * step / 8, "id": 1}]})
                page.wait_for_timeout(18)
            cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        def threshold():
            page.wait_for_timeout(700)
            viewport = page.locator(".stream-viewport").bounding_box()
            x, y = viewport["x"] + 9, viewport["y"] + viewport["height"] * .7
            swipe(x, y, -85)
            page.wait_for_timeout(300)
            expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "3")
            swipe(x, y, -160)
            expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "2")
        scenario("touch-short-pull-stays-long-pull-switches", threshold)
        def touch_sheet():
            box = page.locator(".stream-caption-trigger").bounding_box()
            swipe(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, -160)
            expect(page.locator(".stream-caption-trigger")).to_have_attribute("aria-expanded", "true")
            page.wait_for_timeout(220)
            box = page.locator(".stream-sheet-handle").bounding_box()
            swipe(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, 160)
            expect(page.locator("#stream-caption-sheet")).to_have_count(0)
        scenario("touch-sheet-opens-and-closes", touch_sheet)
        for width in (390, 320):
            page.set_viewport_size({"width": width, "height": 844})
            page.wait_for_timeout(100)
            geometry = page.evaluate("""()=>({overflow:document.documentElement.scrollWidth>innerWidth,
                dockBottom:document.querySelector('.stream-dock').getBoundingClientRect().bottom,height:innerHeight})""")
            assert not geometry["overflow"] and geometry["dockBottom"] <= geometry["height"] + 1, geometry
            page.screenshot(path=str(args.out / f"stream-{width}.png"))
            result["checks"].append(f"stream-{width}-geometry")
        browser.close()
finally:
    extension_release.set()
    server.should_exit = True
    thread.join(timeout=8)
    runtime.database.engine.dispose()
    (args.out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False), flush=True)
sys.exit(1 if result["failures"] else 0)
