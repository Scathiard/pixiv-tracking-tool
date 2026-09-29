"""Trusted touch, zoom, IME, and history checks on an isolated R4 library.

Run from app/: python tests/client_touch_browser.py --artifacts NEW_DIRECTORY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
import traceback
from uuid import UUID


def touch(cdp, points, kind):
    cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [
        {"id": identifier, "x": round(x), "y": round(y), "radiusX": 7, "radiusY": 7,
         "force": 1} for identifier, x, y in points]})


def stroke(cdp, start, end, *, steps=8, second=None):
    first = (1, *start)
    initial = [first] + ([(2, *second[0])] if second else [])
    touch(cdp, initial, "touchStart")
    for step in range(1, steps + 1):
        fraction = step / steps
        x = start[0] + (end[0] - start[0]) * fraction
        y = start[1] + (end[1] - start[1]) * fraction
        points = [(1, x, y)]
        if second:
            sx = second[0][0] + (second[1][0] - second[0][0]) * fraction
            sy = second[0][1] + (second[1][1] - second[0][1]) * fraction
            points.append((2, sx, sy))
        touch(cdp, points, "touchMove")
        time.sleep(0.025)
    touch(cdp, [], "touchEnd")


def anchor(page, kind):
    data = page.evaluate("() => JSON.parse(localStorage.getItem('pixiv-local-positions-v1') || '{}')")
    return (data.get("all") or {}).get(kind)


def first_work(page, selector=".page-viewport.feed"):
    value = page.evaluate("""() => {
      const viewport = document.querySelector('.page-viewport.feed');
      if (!viewport) return null;
      const top = viewport.getBoundingClientRect().top;
      const figures = [...viewport.querySelectorAll('.image-page')];
      const first = figures.find(item => item.getBoundingClientRect().bottom > top + 6);
      return first?.dataset.workId || null;
    }""")
    return int(value) if value else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--width", type=int, choices=(320, 390), default=390)
    parser.add_argument("--uuid-fallback", action="store_true",
                        help="Use a synthetic remote after removing crypto.randomUUID in Chromium")
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

    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist" / "index.html"
    dist_bytes = dist.read_bytes()
    dist_hash = hashlib.sha256(dist_bytes).hexdigest()
    bundle_name = re.search(rb'/reader/assets/(index-[^" ]+\.js)', dist_bytes)
    assert bundle_name is not None
    bundle = dist.parent / "assets" / bundle_name.group(1).decode("ascii")
    bundle_hash = hashlib.sha256(bundle.read_bytes()).hexdigest()
    runtime, remote = make_library(fixture, works=80)
    if not args.uuid_fallback:
        runtime.client = None
        runtime.account_id = runtime.library.account_id = None
    from src.auth import _find_system_browser
    from src.web import create_app

    app = create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
        log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, name="isolated-touch-browser", daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{port}"
    checks, errors, failures, probes = [], [], [], []
    browser = None
    playwright = None
    page = None
    started = time.monotonic()

    def passed(label):
        checks.append(label)
        print(label, flush=True)

    def picture_box():
        page.locator(".image-page .page-picture").first.wait_for()
        box = page.locator(".image-page .page-picture").first.bounding_box()
        assert box and box["width"] > 200 and box["height"] > 150
        return box

    def at_work(work_id):
        page.wait_for_url(f"**/illust/{work_id}*")
        page.locator(f".page-viewport.reader .image-page[data-work-id='{work_id}'] img").first.wait_for()
        page.wait_for_function("[...document.querySelectorAll('.image-page img')].some(i=>i.complete&&i.naturalWidth>0)")
        page.wait_for_function("document.querySelectorAll('.detail-head').length === 1")

    try:
        deadline = time.monotonic() + 25
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("isolated server did not start")
        playwright = sync_playwright().start()
        if playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
            context = browser.new_context(viewport={"width": args.width, "height": 844}, is_mobile=True, has_touch=True,
                                          device_scale_factor=1)
            page = context.new_page()
            page.set_default_timeout(12000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            cdp = context.new_cdp_session(page)

            if args.uuid_fallback:
                page.add_init_script("""(() => {
                  Object.defineProperty(crypto, 'randomUUID', {value: undefined, configurable: true});
                  window.__uuidFallbackReady = typeof crypto.randomUUID === 'undefined' &&
                    typeof crypto.getRandomValues === 'function';
                })()""")
                page.goto(origin + "/illust/79")
                at_work(79)
                assert page.evaluate("window.__uuidFallbackReady") is True
                passed("randomUUID-unavailable-getRandomValues-available")
                page.wait_for_timeout(3000)
                heart = page.locator(".detail-actions .bookmark-button")
                heart.scroll_into_view_if_needed()
                page.wait_for_function("() => !document.querySelector('.detail-actions .bookmark-button')?.disabled")

                def tap_bookmark():
                    box = heart.bounding_box()
                    assert box and box["width"] >= 40
                    page.touchscreen.tap(box["x"] + box["width"] / 2,
                                         box["y"] + box["height"] / 2)

                def await_remote(expected, count):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        if remote.values.get("100079") is expected and remote.write_count == count:
                            return
                        time.sleep(.05)
                    raise AssertionError({"expected": expected, "writes": remote.write_count,
                                          "remote_value": remote.values.get("100079")})

                request_ids = []
                for desired, write_count in ((True, 1), (False, 2)):
                    with page.expect_request(lambda request: request.method == "POST" and
                                             request.url.endswith("/api/works/79/bookmark")) as requested:
                        tap_bookmark()
                    body = requested.value.post_data_json
                    assert body["desired"] is desired
                    identifier = body["request_id"]
                    assert UUID(identifier).version == 4 and str(UUID(identifier)) == identifier
                    request_ids.append(identifier)
                    await_remote(desired, write_count)
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        response = page.request.get(origin + "/api/works/79/bookmark")
                        state = response.json()
                        if state["status"] == "confirmed" and state["value"] is desired:
                            break
                        time.sleep(0.1)
                    else:
                        raise AssertionError(f"readback not confirmed: {state}")
                    probes.append({"bookmark_post": body, "api_after_readback": state,
                        "button_after_readback": heart.evaluate("e => e.outerHTML")})
                    page.wait_for_function("""expected => {
                      const button = document.querySelector('.detail-actions .bookmark-button');
                      return button && !button.disabled && button.getAttribute('aria-pressed') === String(expected);
                    }""", arg=desired)
                    probes[-1]["button_after_ui"] = heart.evaluate("e => ({className: e.className, pressed: e.getAttribute('aria-pressed'), text: e.innerText})")
                    page.screenshot(path=str(artifacts / ("bookmark-on.png" if desired else "bookmark-off.png")))
                    passed("fallback-bookmark-confirmed" if desired else "fallback-unbookmark-confirmed")
                assert request_ids[0] != request_ids[1]
                probes.append({"fallback_request_ids": request_ids, "remote_writes": remote.write_count,
                    "remote_final_value": remote.values.get("100079")})
                if errors:
                    raise AssertionError(errors)
                context.close()
                browser.close()
                browser = None
                return

            page.goto(origin + "/illust/80")
            at_work(80)
            box = picture_box()
            x = box["x"] + box["width"] / 2
            y = min(660, box["y"] + min(box["height"] * .7, 300))
            before_scroll = page.locator(".page-viewport").evaluate("e => e.scrollTop")
            stroke(cdp, (x, y), (x + 9, y - 260))
            page.wait_for_timeout(450)
            after_scroll = page.locator(".page-viewport").evaluate("e => e.scrollTop")
            assert after_scroll > before_scroll + 25, (before_scroll, after_scroll)
            assert "/illust/80" in page.url
            passed("trusted-vertical-touch-scrolls-long-work-without-navigation")

            box = picture_box()
            x = box["x"] + box["width"] / 2
            y = max(230, min(620, box["y"] + 260))
            stroke(cdp, (x - 55, y + 90), (x + 55, y - 110))
            page.wait_for_timeout(250)
            assert "/illust/80" in page.url
            passed("diagonal-touch-keeps-the-same-work")

            page.goto(origin + "/illust/79")
            at_work(79)
            box = picture_box()
            x = box["x"] + box["width"] / 2
            y = min(630, max(260, box["y"] + box["height"] / 2))
            stroke(cdp, (x + 100, y), (x - 120, y + 7))
            at_work(78)
            passed("horizontal-touch-navigates-one-ordinary-work")

            box = picture_box()
            x = box["x"] + box["width"] / 2
            y = min(630, max(260, box["y"] + box["height"] / 2))
            stroke(cdp, (x - 100, y), (x + 120, y + 6))
            at_work(79)
            passed("reverse-horizontal-touch-navigates-one-work")

            box = picture_box()
            x = box["x"] + box["width"] / 2
            y = min(620, max(260, box["y"] + box["height"] / 2))
            stroke(cdp, (x - 70, y), (x + 80, y), second=((x + 70, y + 40), (x - 80, y + 40)))
            page.wait_for_timeout(300)
            assert "/illust/79" in page.url
            passed("two-pointer-gesture-does-not-navigate-ordinary-work")
            page.wait_for_timeout(250)
            zoom_browser_scale_before = page.evaluate("visualViewport?.scale || 1")

            page.get_by_role("button", name="放大此页").first.scroll_into_view_if_needed()
            box = page.get_by_role("button", name="放大此页").first.bounding_box()
            page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            zoom = page.get_by_role("dialog", name="放大查看")
            zoom.wait_for()
            original_url = page.url
            original_scroll = page.locator(".page-viewport").evaluate("e => e.scrollTop")
            page.get_by_role("button", name="放大", exact=True).last.wait_for()
            plus = page.get_by_role("button", name="放大", exact=True).last
            plus_box = plus.bounding_box()
            page.touchscreen.tap(plus_box["x"] + plus_box["width"] / 2, plus_box["y"] + plus_box["height"] / 2)
            reset = zoom.get_by_role("button", name="125%")
            reset.wait_for()
            old_transform = page.locator(".zoom-canvas").get_attribute("style")
            stroke(cdp, (200, 360), (250, 390))
            moved_transform = page.locator(".zoom-canvas").get_attribute("style")
            assert moved_transform != old_transform
            stroke(cdp, (150, 330), (120, 330), second=((230, 330), (260, 330)))
            assert page.locator(".zoom-toolbar button").nth(1).inner_text() != "125%"
            assert page.url == original_url
            close = zoom.get_by_role("button", name="关闭放大")
            close_box = close.bounding_box()
            page.touchscreen.tap(close_box["x"] + close_box["width"] / 2,
                                 close_box["y"] + close_box["height"] / 2)
            zoom.wait_for(state="hidden")
            assert page.url == original_url
            assert abs(page.locator(".page-viewport").evaluate("e => e.scrollTop") - original_scroll) < 30
            zoom_browser_scale_after = page.evaluate("visualViewport?.scale || 1")
            probes.append({"zoom_browser_scale": {"before": zoom_browser_scale_before,
                "after": zoom_browser_scale_after}})
            assert abs(zoom_browser_scale_after - zoom_browser_scale_before) < .02
            passed("zoom-toolbar-pan-pinch-and-close-preserve-reader-position")

            page.get_by_role("button", name="打开菜单", exact=True).click()
            page.get_by_role("navigation", name="主导航").get_by_role("button", name="流式").click()
            page.locator(".page-viewport.feed").wait_for()
            page.locator(".page-viewport.feed .image-page").first.wait_for()
            page.wait_for_function("""() => {
              const view = document.querySelector('.page-viewport.feed');
              const top = view?.getBoundingClientRect().top || 0;
              return [...(view?.querySelectorAll('.image-page') || [])]
                .some(item => item.getBoundingClientRect().bottom > top + 6);
            }""")
            feed_url = page.url
            feed_work_before = first_work(page)
            feed_scroll_before = page.locator(".page-viewport.feed").evaluate("e => e.scrollTop")
            box = picture_box()
            x = box["x"] + box["width"] / 2
            y = min(630, max(260, box["y"] + box["height"] / 2))
            stroke(cdp, (x + 95, y), (x - 120, y + 4))
            page.wait_for_timeout(300)
            assert page.url == feed_url and page.locator(".page-viewport.feed").count() == 1
            feed_work_after = first_work(page)
            feed_scroll_after = page.locator(".page-viewport.feed").evaluate("e => e.scrollTop")
            probes.append({"feed_horizontal": {"work_before": feed_work_before, "work_after": feed_work_after,
                "scroll_before": feed_scroll_before, "scroll_after": feed_scroll_after}})
            assert feed_work_after == feed_work_before
            passed("horizontal-touch-in-feed-does-not-navigate")
            page.wait_for_timeout(1000)
            page.evaluate("""() => {
              window.__inputTrace = [];
              for (const kind of ['pointerdown','pointerup','touchstart','touchend','click'])
                document.addEventListener(kind, event => window.__inputTrace.push({kind,
                  target: event.target?.closest?.('button')?.className || event.target?.tagName}), true);
            }""")

            heart = page.locator(".reading-toolbar .bookmark-button")
            page.wait_for_function("() => !document.querySelector('.reading-toolbar .bookmark-button')?.disabled")
            heart_box = heart.bounding_box()
            assert heart_box
            heart_hit = page.evaluate("""box => {
              const node = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
              return node?.closest('.bookmark-button') !== null;
            }""", heart_box)
            assert heart_hit
            page.evaluate("""() => {
              window.__heartClicks = 0;
              document.querySelector('.reading-toolbar .bookmark-button')
                ?.addEventListener('click', () => window.__heartClicks++);
            }""")
            page.screenshot(path=str(artifacts / "feed-before-bookmark.png"))
            page.touchscreen.tap(heart_box["x"] + heart_box["width"] / 2,
                                 heart_box["y"] + heart_box["height"] / 2)
            page.wait_for_timeout(300)
            heart_clicks = page.evaluate("window.__heartClicks")
            probes.append({"bookmark_hit_test": {"box": heart_box, "hit": heart_hit, "clicks": heart_clicks,
                "input_trace": page.evaluate("window.__inputTrace"),
                "viewport": page.evaluate("""() => ({innerHeight, scrollY, scale: visualViewport?.scale,
                  visualHeight: visualViewport?.height,
                  toolbar: document.querySelector('.reading-toolbar')?.getBoundingClientRect().toJSON(),
                  style: getComputedStyle(document.querySelector('.reading-toolbar')).cssText})""")}})
            assert heart_clicks == 1
            assert page.url == feed_url
            passed("trusted-feed-bookmark-control-receives-tap")

            skip = page.get_by_role("button", name="跳过作品", exact=False)
            skip_box = skip.bounding_box()
            hit = page.evaluate("""box => {
              const node = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
              return {tag: node?.tagName, className: node?.className?.baseVal || node?.className,
                html: node?.outerHTML?.slice(0, 240)};
            }""", skip_box)
            probes.append({"skip_hit_test": {"box": skip_box, "hit": hit}})
            page.screenshot(path=str(artifacts / "feed-before-skip.png"))
            page.evaluate("""() => {
              window.__skipClicks = 0;
              [...document.querySelectorAll('.reading-toolbar button')].find(button => button.textContent?.includes('跳过作品'))
                ?.addEventListener('click', () => window.__skipClicks++);
            }""")
            page.touchscreen.tap(skip_box["x"] + skip_box["width"] / 2,
                                 skip_box["y"] + skip_box["height"] / 2)
            page.wait_for_timeout(500)
            skip_probe = {"clicks": page.evaluate("window.__skipClicks"), "work_after_500ms": first_work(page),
                "scroll_after_500ms": page.locator(".page-viewport.feed").evaluate("e => e.scrollTop")}
            page.wait_for_timeout(1000)
            skip_probe["work_after_1500ms"] = first_work(page)
            skip_probe["scroll_after_1500ms"] = page.locator(".page-viewport.feed").evaluate("e => e.scrollTop")
            probes.append({"trusted_skip": skip_probe})
            if skip_probe["clicks"] and skip_probe["work_after_1500ms"] == feed_work_after - 1:
                passed("trusted-feed-skip-button-advances-one-work")
            else:
                failures.append({"check": "trusted-feed-skip-button-advances-one-work", "probe": skip_probe})

            # A trusted wheel gives a separate feed anchor even if skip is broken.
            if first_work(page) != feed_work_after - 1:
                page.locator(".page-viewport.feed").hover()
                for _ in range(8):
                    page.mouse.wheel(0, 260)
                    page.wait_for_timeout(150)
                    if first_work(page) == feed_work_after - 1:
                        break
            page.wait_for_timeout(700)
            feed_anchor = anchor(page, "feed")
            assert feed_anchor and feed_anchor["work_id"] == feed_work_after - 1, feed_anchor
            page.go_back()
            at_work(79)
            reader_anchor = anchor(page, "reader")
            assert reader_anchor and reader_anchor["work_id"] == 79, reader_anchor
            assert anchor(page, "feed")["work_id"] == feed_work_after - 1
            passed("browser-back-restores-ordinary-reader-with-independent-feed-anchor")

            # A focused editable probe tests the app's global navigation guard.
            page.evaluate("""() => {
              const input = document.createElement('input'); input.id = 'ime-probe';
              input.setAttribute('aria-label', 'IME navigation probe');
              input.style.cssText = 'position:fixed;top:90px;left:15px;z-index:1000;width:200px';
              document.body.append(input);
              window.__imeEvents = [];
              for (const kind of ['compositionstart','compositionupdate','compositionend'])
                input.addEventListener(kind, () => window.__imeEvents.push(kind));
            }""")
            probe = page.get_by_label("IME navigation probe")
            probe.click()
            cdp.send("Input.imeSetComposition", {"text": "画师", "selectionStart": 2, "selectionEnd": 2})
            page.keyboard.press("ArrowRight")
            assert "/illust/79" in page.url
            cdp.send("Input.insertText", {"text": "画师"})
            cdp.send("Input.imeSetComposition", {"text": "", "selectionStart": 0, "selectionEnd": 0})
            assert page.evaluate("window.__imeEvents.length") >= 2
            page.keyboard.press("ArrowLeft")
            assert "/illust/79" in page.url
            passed("trusted-keyboard-and-cdp-ime-in-input-do-not-navigate")

            page.screenshot(path=str(artifacts / "touch-final.png"))
            if errors:
                raise AssertionError(errors)
            if failures:
                raise AssertionError(failures)
            context.close()
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
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        server.should_exit = True
        thread.join(20)
        (artifacts / "result.json").write_text(json.dumps({"checks": checks, "errors": errors, "failures": failures,
            "probes": probes,
            "dist_index_sha256": dist_hash, "bundle_name": bundle.name, "bundle_sha256": bundle_hash,
            "uuid_fallback": args.uuid_fallback, "synthetic_remote_used": args.uuid_fallback,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "real_account_used": False, "real_library_used": False, "real_phone_tested": False,
            "emulated_mobile_width": args.width,
            "trusted_touch_method": "Chromium CDP Input.dispatchTouchEvent"}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
