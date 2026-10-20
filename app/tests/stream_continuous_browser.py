"""One focused, real-input R-074 check; 3 synthetic works / 5 original pages.

Run from app/: python tests/stream_continuous_browser.py --dist BUILT_DIST
    --out ../work/tmp/continuous-stream/browser-UNIQUE
No real account, real library, package, large fixture, or frontend build is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--remaining-only", action="store_true", help="Resume after the already evidenced continuous-pull scenario")
    parser.add_argument("--finish-only", action="store_true", help="Only order-position, narrow geometry and desktop inputs; earlier groups already evidenced")
    args = parser.parse_args()
    # Windows sandbox may permit file reads while denying directory realpath handles.
    dist, out = Path(os.path.abspath(args.dist)), Path(os.path.abspath(args.out))
    assert dist.is_dir() and (dist / "index.html").is_file(), dist
    temporary_root = root / "work/tmp/continuous-stream"
    assert out.parent == temporary_root and out.name.startswith("browser-"), out
    out.mkdir(parents=True, exist_ok=False)
    evidence = root / "docs/审查/证据/2026-10-21-随机等待与连续切换诊断"
    evidence.mkdir(parents=True, exist_ok=True)
    prefix = out.name
    os.environ["TEMP"] = os.environ["TMP"] = str(out)
    os.environ["DATA_ROOT"] = str(out / "configuration")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.path[:0] = [str(root / "app"), str(root / "app/tests")]
    from stream_reader_browser import tiny_library
    from src.auth import _find_system_browser
    from src.core.models import Work
    from src.web import create_app
    from playwright.sync_api import sync_playwright, expect
    import uvicorn

    result = {"checks": [], "metrics": {}, "requests": [], "request_failures": [], "errors": [],
        "failures": [], "temporary_path": str(out), "temporary_cleaned": False,
        "fixture": "3 synthetic works / 5 pages; original media and APIs from real local runtime",
        "real_library": False, "real_account": False, "physical_phone": False,
        "input": "Chromium CDP touch, real mouse wheel/double-click and keyboard",
        "remaining_only": args.remaining_only,
        "finish_only": args.finish_only,
        "baseline": "Prior diagnosis/source: one current viewport, no adjacent rendered track; no old-browser red run"}
    result["frontend_sha256"] = {str(file.relative_to(dist)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in dist.rglob("*") if file.is_file()}
    runtime = tiny_library(out / "fixture")
    with runtime.database.write() as session:
        session.get(Work, 2).caption = "<p>Full synthetic caption.</p>" * 40
    create_started, create_release = threading.Event(), threading.Event()
    original_create = runtime.reading.create
    create_calls = []

    def delayed_create(*positional, **keywords):
        create_calls.append({"started": time.monotonic()})
        create_started.set()
        # Runs in the API worker, never in Playwright's synchronous route callback.
        if not create_release.wait(150):
            raise TimeoutError("Browser did not release synthetic random preparation")
        value = original_create(*positional, **keywords)
        create_calls[-1]["completed"] = time.monotonic()
        return value

    runtime.reading.create = delayed_create
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        number = port.getsockname()[1]
    origin = f"http://127.0.0.1:{number}"
    server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False,
        schedule=False, reading_ui=False), host="127.0.0.1", port=number,
        log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    page = browser = pw = None
    active = "startup"

    def route(request):
        path = urlsplit(request.request.url).path
        if path.startswith("/api/"):
            result["requests"].append({"url": request.request.url, "method": request.request.method,
                "phase": active, "at": time.monotonic()})
            request.continue_()
        elif path.startswith("/reader/assets/"):
            asset = dist / path.removeprefix("/reader/")
            assert asset.is_relative_to(dist) and ".." not in asset.parts
            request.fulfill(path=str(asset), content_type=mimetypes.guess_type(asset)[0] or "application/octet-stream")
        elif request.request.is_navigation_request():
            request.fulfill(path=str(dist / "index.html"), content_type="text/html")
        else:
            request.continue_()

    def setup(context):
        current_page = context.new_page()
        current_page.set_default_timeout(7000)
        current_page.route("**/*", route)
        current_page.on("pageerror", lambda error: result["errors"].append(str(error)))
        current_page.on("requestfailed", lambda request: result["request_failures"].append({
            "url": request.url, "error": request.failure, "phase": active}))
        return current_page

    def current():
        return page.locator('.stream-reader [data-stream-slot="current"]')

    def viewport():
        return current().locator(".stream-viewport")

    def identity(value):
        expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", str(value))
        expect(current()).to_have_attribute("data-work-id", str(value))

    def settled():
        expect(current()).to_have_attribute("data-panel-ready", "true")
        expect(page.locator(".stream-reader")).to_have_attribute("data-track-settling", "false")
        expect(viewport()).to_have_attribute("aria-busy", "false")
        page.wait_for_timeout(300)

    def pump_until(predicate, message, seconds=7):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            page.wait_for_timeout(25)
        assert predicate(), message

    def screenshot(name):
        page.screenshot(path=str(evidence / f"{prefix}-{name}.png"))

    def position():
        return current().evaluate("""slot=>{
            const v=slot.querySelector('.stream-viewport'), vr=v.getBoundingClientRect();
            const rows=[...v.querySelectorAll('.stream-page')].map(e=>{const r=e.getBoundingClientRect();
                return {page:Number(e.dataset.page),globalIndex:Number(e.dataset.globalIndex),
                    top:r.top-vr.top,bottom:r.bottom-vr.top,height:r.height}});
            const selected=rows.find(r=>r.bottom>2)||rows[0];
            return {work:Number(slot.dataset.workId),page:selected.page,insideOffset:Math.max(0,-selected.top),
                pageTop:selected.top,rawScrollTop:v.scrollTop,viewportHeight:v.clientHeight,scrollHeight:v.scrollHeight,
                range:{firstPage:Math.min(...rows.map(r=>r.page)),lastPage:Math.max(...rows.map(r=>r.page))},rows};
        }""")

    def check(name, action):
        nonlocal active
        active = name
        metric = action()
        result["checks"].append(name)
        result["metrics"][name] = metric
        print("PASS", name, flush=True)

    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started, "Synthetic local server did not start"
        pw = sync_playwright().start()
        if pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            mobile = browser.new_context(viewport={"width": 390, "height": 844},
                is_mobile=True, has_touch=True, device_scale_factor=1)
            page = setup(mobile)
            cdp = mobile.new_cdp_session(page)

            def touch(kind, points):
                cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [
                    {"x": x, "y": y, "id": index + 1, "radiusX": 2, "radiusY": 2}
                    for index, (x, y) in enumerate(points)]})

            def pull(distance, *, release=True, x=None, y=None):
                box = viewport().bounding_box()
                x = x if x is not None else box["x"] + 12
                y = y if y is not None else box["y"] + box["height"] * (.35 if distance > 0 else .75)
                touch("touchStart", [(x, y)])
                for step in range(1, 11):
                    touch("touchMove", [(x, y + distance * step / 10)])
                    page.wait_for_timeout(18)
                if release:
                    # End a deliberate drag at rest, so page selection is repeatable.
                    page.wait_for_timeout(120)
                    touch("touchEnd", [])

            def enter():
                page.goto(origin)
                expect(page.locator(".work-card")).to_have_count(3, timeout=15000)
                page.locator('.work-card[data-work-id="2"] .card-open').click()
                expect(page.locator(".reading-page-count")).to_contain_text("1 / 1", timeout=15000)
                expect(page.locator(".page-window")).to_have_attribute("data-window-size", "1", timeout=15000)
                page.get_by_role("button", name="打开菜单", exact=True).click()
                page.get_by_title("流式阅览", exact=True).click()
                expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "2", timeout=15000)
                identity(2)
                settled()
                pump_until(create_started.is_set, "Random preparation never started")
                for slot, work in (("previous", 3), ("next", 1)):
                    target = page.locator(f'[data-stream-slot="{slot}"][data-work-id="{work}"]')
                    expect(target).to_have_count(1)
                    expect(target).to_have_attribute("data-panel-ready", "true")
                    pump_until(lambda: target.locator("img").first.evaluate("e=>e.complete&&e.naturalWidth>0"),
                        f"Adjacent synthetic original {work} did not decode")
                return {"entered": 2, "adjacent": [3, 1], "random_create_blocked": True}

            check("mobile-entry-and-neighbors", enter)

            def continuous():
                pull(70)
                settled()
                identity(2)
                target = page.locator('[data-stream-slot="previous"][data-work-id="3"]')
                target.evaluate("e=>{window.__enteredSlot=e;window.__enteredImage=e.querySelector('img')}")
                pull(260, release=False)
                identity(2)
                offset = float(page.locator(".stream-reader").get_attribute("data-track-offset"))
                assert offset > 100, f"Touch did not move the actual track: {offset}px"
                boxes = page.locator('.stream-reader [data-stream-slot]').evaluate_all("""els=>{
                    const clip=document.querySelector('.stream-reader').getBoundingClientRect();
                    return els.map(e=>{const r=e.getBoundingClientRect();return {slot:e.dataset.streamSlot,work:e.dataset.workId,
                    top:r.top,bottom:r.bottom,clipTop:clip.top,clipBottom:clip.bottom,
                    visible:Math.max(0,Math.min(r.bottom,clip.bottom,innerHeight)-Math.max(r.top,clip.top,0))}})}""")
                visible = [box for box in boxes if box["visible"] > 30]
                assert {box["work"] for box in visible} >= {"2", "3"}, boxes
                screenshot("half-pull-two-visible")
                touch("touchEnd", [])
                identity(3)
                settled()
                assert current().evaluate("e=>e===window.__enteredSlot&&e.querySelector('img')===window.__enteredImage"), "Entered neighbor DOM/image was rebuilt"
                screenshot("previous-committed")
                feed_before = len([request for request in result["requests"] if "/api/library/cursor/feed?" in request["url"] and "limit=64" in request["url"]])
                pull(-260)
                identity(2)
                settled()
                feed_after = len([request for request in result["requests"] if "/api/library/cursor/feed?" in request["url"] and "limit=64" in request["url"]])
                assert feed_before == feed_after, "Returning to prepared work rebuilt a foreground 64-page cursor window"
                return {"half_pull_boxes": boxes, "actual_track_offset": offset, "short_pull_work": 2, "long_pull_work": 3,
                    "same_neighbor_dom": True, "return_work": 2, "new_foreground_64_reads": feed_after-feed_before}

            if not args.remaining_only and not args.finish_only:
                check("mobile-continuous-pull-dom-and-return", continuous)

            def multipage():
                pull(-260)
                identity(1)
                settled()
                pull(-500)
                identity(1)
                pump_until(lambda: viewport().evaluate("e=>e.scrollTop") > 20, "Multpage touch did not scroll inside work")
                first_scroll = viewport().evaluate("e=>e.scrollTop")
                return_drags = []
                # Every return drag starts inside the work; reaching the edge must
                # not arm that same gesture, including its native scroll inertia.
                for _ in range(3):
                    started = viewport().evaluate("e=>e.scrollTop")
                    if started < 2:
                        break
                    assert started >= 2
                    pull(600, y=180)
                    identity(1)
                    page.wait_for_timeout(180)
                    return_drags.append({"before": started, "after": viewport().evaluate("e=>e.scrollTop")})
                pump_until(lambda: viewport().evaluate("e=>e.scrollTop") < 2, "Return gesture never reached top")
                pull(260)
                identity(2)
                settled()
                return {"internal_scroll": first_scroll, "return_drags": return_drags,
                    "same_gesture_at_top_stayed": 1, "new_boundary_gesture_entered": 2}

            if not args.finish_only:
                check("mobile-multipage-boundary-new-gesture", multipage)

            def controls():
                image = current().locator(".inline-zoom-media").first
                image.locator("img").dblclick()
                expect(image).to_have_attribute("data-inline-zoom-scale", "2.25")
                image_box = image.locator("img").bounding_box()
                view_box = viewport().bounding_box()
                left, right = max(image_box["x"], view_box["x"]), min(image_box["x"] + image_box["width"], view_box["x"] + view_box["width"])
                top, bottom = max(image_box["y"], view_box["y"]), min(image_box["y"] + image_box["height"], view_box["y"] + view_box["height"])
                pull(-260, x=(left+right)/2, y=top+(bottom-top)*.75)
                identity(2)
                image.locator("img").dblclick()
                expect(image).to_have_attribute("data-inline-zoom-scale", "1")
                trigger = page.locator(".stream-caption-trigger").bounding_box()
                x, y = trigger["x"] + trigger["width"] / 2, trigger["y"] + trigger["height"] / 2
                touch("touchStart", [(x, y)])
                for step in range(1, 9):
                    touch("touchMove", [(x, y - 160 * step / 8)])
                    page.wait_for_timeout(18)
                touch("touchEnd", [])
                expect(page.locator(".stream-caption-trigger")).to_have_attribute("aria-expanded", "true")
                identity(2)
                page.wait_for_timeout(220)  # Pick the handle at its settled on-screen position.
                handle = page.locator(".stream-sheet-handle").bounding_box()
                x, y = handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2
                touch("touchStart", [(x, y)])
                for step in range(1, 9):
                    touch("touchMove", [(x, y + 160 * step / 8)])
                    page.wait_for_timeout(18)
                touch("touchEnd", [])
                expect(page.locator("#stream-caption-sheet")).to_have_count(0)
                identity(2)
                return {"zoom_and_caption_work": 2}

            if not args.finish_only:
                check("mobile-zoom-caption-do-not-navigate", controls)

            def random_preparation():
                posts = lambda: [request for request in result["requests"] if urlsplit(request["url"]).path == "/api/reading-sessions" and request["method"] == "POST"]
                assert len(posts()) == len(create_calls) == 1, {"posts": posts(), "creates": create_calls}
                assert not [request for request in result["request_failures"] if "/api/reading-sessions" in request["url"]], result["request_failures"]
                create_release.set()
                pump_until(lambda: "completed" in create_calls[0], "Synthetic random preparation did not complete")
                page.wait_for_timeout(250)
                assert not [request for request in result["requests"] if "/api/reading-sessions/" in request["url"] and "/feed?" in request["url"]], "Preparation hydrated an unrelated random page window before choosing random"
                result["metrics"]["random-lifecycle-before-order"] = {"post_count": len(posts()),
                    "backend_create_count": len(create_calls), "session_request_failures": 0,
                    "pre_toggle_random_page_hydration": 0}
                pull(-260)
                identity(1)
                settled()
                pull(-560)
                identity(1)
                pump_until(lambda: "2 / 3" in page.locator(".stream-counter").inner_text(), "Did not reach current work's second page")
                page.wait_for_timeout(300)
                before = page.locator(".stream-counter").inner_text().split("·")[-1].strip()
                before_position = position()
                positions = result["metrics"]["order-position-details"] = {"before": before_position, "switches": []}
                screenshot("random-before")
                for order in ("random", "sequential", "random"):
                    page.get_by_label("流式阅览顺序", exact=True).select_option(order)
                    expect(page.get_by_label("流式阅览顺序", exact=True)).to_have_value(order)
                    identity(1)
                    expect(page.locator(".stream-counter")).to_contain_text(before)
                    settled()
                    after = position()
                    positions["switches"].append({"order": order, "position": after})
                    assert (after["work"], after["page"]) == (before_position["work"], before_position["page"]), "Changing order moved to a different work/page"
                    assert abs(after["insideOffset"]-before_position["insideOffset"]) < 3, "Changing order moved the visible page's internal offset"
                    assert abs(after["pageTop"]-before_position["pageTop"]) < 3, "Changing order moved the image relative to its viewport"
                    if after["range"]["firstPage"] == before_position["range"]["firstPage"]:
                        assert abs(after["rawScrollTop"]-before_position["rawScrollTop"]) < 3, "Same window origin changed raw scrollTop"
                assert len(posts()) == 1, posts()
                screenshot("random-current-page-retained")
                return {"posts_while_switching_and_after_toggle": len(posts()), "current_work": 1,
                    "page_counter_retained": before, "page_internal_offset": before_position["insideOffset"],
                    "pre_toggle_random_page_hydration": 0}

            check("random-preparation-survives-navigation-and-retains-page", random_preparation)

            def narrow():
                page.set_viewport_size({"width": 320, "height": 844})
                page.wait_for_timeout(120)
                geometry = page.evaluate("""()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,
                    dockBottom:document.querySelector('.stream-dock').getBoundingClientRect().bottom,height:innerHeight})""")
                assert geometry["scrollWidth"] <= geometry["width"] and geometry["dockBottom"] <= geometry["height"]+1, geometry
                screenshot("320-geometry")
                return geometry

            check("mobile-320-geometry", narrow)
            mobile.close()
            desktop = browser.new_context(viewport={"width": 1440, "height": 900})
            page = setup(desktop)

            def desktop_navigation():
                page.goto(origin + "/flow")
                expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", "3", timeout=15000)
                identity(3)
                settled()
                expect(page.locator('[data-stream-slot="next"][data-work-id="2"]')).to_have_attribute("data-panel-ready", "true", timeout=15000)
                box = viewport().bounding_box()
                page.mouse.move(box["x"]+12, box["y"]+box["height"]*.5)
                page.mouse.wheel(0, 420)
                identity(2)
                settled()
                viewport().press("ArrowLeft")
                identity(3)
                settled()
                viewport().press("ArrowRight")
                identity(2)
                settled()
                screenshot("1440-wheel-keyboard")
                return {"wheel_next": 2, "arrow_previous": 3, "arrow_next": 2}

            check("desktop-wheel-and-direction-keys", desktop_navigation)
            assert not result["errors"], result["errors"]
            browser.close()
            browser = None
    except Exception as error:
        result["failures"].append({"name": active, "error": str(error), "traceback": traceback.format_exc()})
        if page and not page.is_closed():
            result["failure_state"] = page.evaluate("""()=>({work:document.querySelector('.stream-reader')?.dataset.streamWork,
                counter:document.querySelector('.stream-counter')?.textContent,
                currentScroll:[...document.querySelectorAll('[data-stream-slot="current"] .stream-viewport')].map(e=>({top:e.scrollTop,height:e.clientHeight,total:e.scrollHeight})),
                slots:[...document.querySelectorAll('[data-stream-slot]')].map(e=>({slot:e.dataset.streamSlot,
                    work:e.dataset.workId,round:e.dataset.round,html:e.outerHTML.slice(0,600)}))})""")
            screenshot("failure")
        print("FAIL", active, str(error), flush=True)
    finally:
        create_release.set()
        if browser:
            browser.close()
        if pw:
            pw.stop()
        server.should_exit = True
        thread.join(timeout=8)
        runtime.database.engine.dispose()
        result["backend_create_calls"] = create_calls
        if not thread.is_alive():
            # Only this registered new task directory; never follow an external link.
            assert out.parent == temporary_root and not out.is_symlink()
            shutil.rmtree(out)
            result["temporary_cleaned"] = True
        result_path = evidence / f"{prefix}-result.json"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"checks": result["checks"], "failures": result["failures"],
            "result": str(result_path), "temporary_cleaned": result["temporary_cleaned"]}, ensure_ascii=False), flush=True)
    return int(bool(result["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
