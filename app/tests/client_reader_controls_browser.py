"""Production reader controls on a new synthetic fixture, optionally from an EXE.

Run from app/: python tests/client_reader_controls_browser.py --artifacts NEW_PATH
EXE mode keeps its three-work database unchanged; long-page and fault scenarios
are explicitly browser-only response replays using its own synthetic media.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--suite", choices=("full", "bookmarks"), default="full")
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "fixture")
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    result = {"checks": [], "failures": [], "errors": [], "metrics": {},
              "mode": "exe" if args.exe else "source", "real_library_used": False,
              "real_account_used": False, "real_phone_tested": False,
              "scope": "production frontend; synthetic metadata, bookmark and media faults"}
    server = thread = process = output = None
    stop = artifacts / "stop.signal"
    started = time.monotonic()
    page = None

    def fetch(path):
        with urlopen(origin + path, timeout=5) as response:
            return response.read()

    def read(path):
        return json.loads(fetch(path))

    def passed(label, metric=None):
        result["checks"].append(label)
        if metric is not None:
            result["metrics"][label] = metric
        print(label, flush=True)

    def scenario(label, action):
        try:
            action()
        except Exception as error:
            result["failures"].append({"name": label, "error": str(error),
                                       "traceback": traceback.format_exc()})
            if page:
                try:
                    page.screenshot(path=str(artifacts / f"failure-{label}.png"))
                except Exception:
                    pass
            print(f"FAILED {label}: {error}", flush=True)

    try:
        if args.exe:
            exe = args.exe.resolve(strict=True)
            result.update(exe=str(exe), exe_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(),
                          actual_database_works=3, browser_only_long_work_pages=2000)
            output = (artifacts / "exe-output.txt").open("xb")
            env = os.environ.copy()
            env["TEMP"] = env["TMP"] = str(artifacts)
            process = subprocess.Popen([str(exe), "--smoke-test", "--smoke-output",
                str(artifacts / "exe-smoke.json"), "--smoke-serve-port", str(port),
                "--smoke-stop-file", str(stop)], cwd=artifacts, env=env, stdout=output,
                stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            from client_fixture import make_library
            runtime, remote = make_library(artifacts / "fixture", works=80)
            from src.web import create_app
            import uvicorn
            server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime,
                authenticate=False, schedule=False, reading_ui=True), host="127.0.0.1",
                port=port, log_level="error", loop="src.networking:http_loop_factory"))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            result.update(actual_database_works=80, actual_long_work_pages=2000)
        deadline = time.monotonic() + 80
        while time.monotonic() < deadline:
            if process and process.poll() is not None:
                raise RuntimeError(f"isolated EXE exited: {process.returncode}")
            try:
                read("/api/status")
                break
            except OSError:
                time.sleep(.15)
        else:
            raise TimeoutError("isolated service did not start")

        from src.auth import _find_system_browser
        from playwright.sync_api import expect, sync_playwright
        long_id = 2 if args.exe else 80
        other_id = 1 if args.exe else 79
        original = read(f"/api/works/{long_id}?page_limit=1")
        mock_epoch = original["bookmark"]["account_epoch"] + 1
        static = read(f"/api/works/{other_id}?page_limit=1")
        media_src = static["pages"][0]["src"]
        media = fetch(media_src)
        html = fetch("/")
        assets = re.findall(r'/reader/assets/[^"\']+', html.decode("utf-8"))
        result["served_frontend_sha256"] = {"index.html": hashlib.sha256(html).hexdigest(),
            **{asset: hashlib.sha256(fetch(asset)).hexdigest() for asset in assets}}
        assert assets, "production frontend assets were not served"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            for width, theme in ((1440, "dark"), (1440, "light"), (390, "dark"),
                                 (390, "light"), (320, "dark"), (320, "light")):
                if args.suite == "bookmarks" and (width, theme) != (1440, "dark"):
                    continue
                context = browser.new_context(viewport={"width": width, "height": 900 if width > 500 else 844},
                                              is_mobile=width < 500, has_touch=width < 500)
                page = context.new_page()
                page.set_default_timeout(10000)
                label = f"{width}-{theme}"
                page.add_init_script("localStorage.setItem('pixiv-local-theme', " + json.dumps(theme) + ")")
                page.add_init_script("localStorage.removeItem('pixiv-local-positions-v1')")
                page.on("pageerror", lambda error, label=label: result["errors"].append({"view": label, "error": str(error)}))
                controls = {"failure": False, "inject_fault": False, "delay": False,
                            "bookmark": "fast", "operations": {}, "queries": [], "bypass_filters": False,
                            "real_bookmarks": False, "delayed_responses": 0, "metadata_requests": []}
                facts = {}

                def fact(work_id):
                    return facts.setdefault(work_id, {"work_id": work_id, "operation_id": None,
                        "status": "confirmed", "desired": None, "value": False,
                        "confirmed_at": "2026-10-03T00:00:00Z", "error": None, "version": 100,
                        "cached": False, "verified": True, "fact_account_id": "synthetic",
                        "current_account_id": "synthetic", "account_epoch": mock_epoch})

                def route_api(route):
                    path = urlsplit(route.request.url).path
                    params = parse_qs(urlsplit(route.request.url).query)
                    match = re.fullmatch(r"/api/works/(\d+)(/bookmark(?:/refresh)?)?", path)
                    if path == "/api/library":
                        controls["queries"].append(params)
                        if controls["bypass_filters"]:
                            clean = {key: values for key, values in params.items() if key not in {"q", "tags", "types", "favorites", "recent"}}
                            response = route.fetch(url=origin + path + "?" + urlencode(clean, doseq=True))
                            route.fulfill(response=response)
                        else:
                            route.continue_()
                    elif path == "/api/library/locate" and controls["bypass_filters"]:
                        clean = {key: values for key, values in params.items() if key not in {"q", "tags", "types", "favorites", "recent"}}
                        response = route.fetch(url=origin + path + "?" + urlencode(clean, doseq=True))
                        route.fulfill(response=response)
                    elif path == "/api/tasks" and route.request.method == "POST":
                        body = route.request.post_data_json
                        assert body.get("kind") == "metadata", body
                        controls["metadata_requests"].append(body)
                        route.fulfill(json={"operation_id": "synthetic-metadata", "created": True})
                    elif path == "/api/tasks/synthetic-metadata":
                        route.fulfill(json={"id": "synthetic-metadata", "label": "刷新作品信息",
                            "state": "finished", "source": "manual", "phase": {"name": "captions",
                            "done": 1, "total": 1, "unit": "works", "detail": ""}, "result": {"outcome": "success"}})
                    elif match and match[2]:
                        if controls["real_bookmarks"]:
                            route.continue_()
                            return
                        work_id = int(match[1])
                        state = fact(work_id)
                        if route.request.method == "POST" and match[2] == "/bookmark":
                            desired = route.request.post_data_json["desired"]
                            state["version"] += 1
                            kind = controls["bookmark"]
                            if kind == "slow":
                                operation = f"synthetic-{work_id}-{state['version']}"
                                state.update(status="pending", desired=desired, operation_id=operation)
                                controls["operations"][operation] = (work_id, time.monotonic() + 3.8, desired)
                            elif kind == "failure":
                                state.update(status="failed", desired=desired, error="合成拒绝", operation_id=None)
                            else:
                                state.update(status="confirmed", value=desired, desired=desired, operation_id=None, error=None)
                        route.fulfill(json=deepcopy(state))
                    elif path.startswith("/api/bookmark-operations/"):
                        if controls["real_bookmarks"]:
                            route.continue_()
                            return
                        operation = path.rsplit("/", 1)[-1]
                        work_id, finish, desired = controls["operations"][operation]
                        state = fact(work_id)
                        if time.monotonic() >= finish:
                            state.update(status="confirmed", value=desired, version=state["version"] + 1)
                        route.fulfill(json=deepcopy(state))
                    elif match:
                        response = route.fetch()
                        data = response.json()
                        if not controls["real_bookmarks"]:
                            data["bookmark"] = deepcopy(fact(int(match[1])))
                        data["tags"] = ["landscape", "english_only", "no_translation"]
                        data["tag_details"] = [
                            {"name": "landscape", "translated_name": "风景", "language": "zh-cn", "source": "pixiv"},
                            {"name": "english_only", "translated_name": "English label", "language": "en", "source": "pixiv"},
                            {"name": "no_translation", "translated_name": None, "language": "zh-cn", "source": "pixiv"}]
                        if int(match[1]) == long_id:
                            offset = int(params.get("page_offset", [0])[0])
                            limit = int(params.get("page_limit", [1])[0])
                            if args.exe:
                                data["display_page_count"] = data["page_count"] = 2000
                                data["pages"] = [{"index": index, "src": media_src, "width": 640,
                                    "height": 860, "state": "verified"} for index in range(offset, min(2000, offset + limit))]
                            if controls["inject_fault"]:
                                for item in data["pages"]:
                                    if item["index"] == 1:
                                        item["src"] = "/controls-target.png"
                            if controls["delay"] and offset >= 100:
                                # Delay delivery, including a response for an aborted old intent.
                                controls["delayed_responses"] += 1
                                page.wait_for_timeout(280)
                        route.fulfill(response=response, json=data)
                    else:
                        route.continue_()

                page.route("**/api/**", route_api)
                page.route("**/controls-target.png", lambda route: route.fulfill(status=503,
                    content_type="text/plain", body="synthetic image failure") if controls["failure"]
                    else route.fulfill(content_type="image/png", body=media))

                def open_long(query=""):
                    page.goto(origin + f"/illust/{long_id}" + query, wait_until="domcontentloaded")
                    page.locator(".page-viewport.reader .image-page").first.wait_for()
                    expect(page.locator(".reading-counter small")).to_contain_text("第 1 / 2000 页")
                    page.wait_for_function("document.querySelector('.image-page img')?.naturalWidth > 0")
                    expect(page.locator(".reader-notice")).to_have_count(0)

                def page_number():
                    text = page.locator(".reading-counter small").inner_text()
                    return int(re.search(r"第 (\d+) /", text)[1])

                def key_to(key, number):
                    page.keyboard.press(key)
                    expect(page.locator(".reading-counter small")).to_contain_text(f"第 {number} / 2000 页")
                    expect(page.locator(".reader-notice")).to_have_count(0)

                def layout():
                    open_long()
                    geometry = page.evaluate("""() => {
                      const bar=document.querySelector('.reading-toolbar'), v=document.querySelector('.page-viewport');
                      const box=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom}};
                      const buttons=[...bar.querySelectorAll('.reader-nav-button')];
                      return {bar:box(bar),viewport:box(v),overflow:document.documentElement.scrollWidth>innerWidth,
                        hearts:document.querySelectorAll('.bookmark-button').length,
                        footerHearts:bar.querySelectorAll('.bookmark-button').length,
                        buttons:buttons.map(e=>{const label=e.querySelector('.reader-nav-label');const range=document.createRange();range.selectNodeContents(label);
                          return {box:box(e),labelDisplay:getComputedStyle(label).display,textRects:range.getClientRects().length,aria:e.getAttribute('aria-label')}})};
                    }""")
                    assert not geometry["overflow"] and geometry["bar"]["x"] >= 0
                    assert geometry["bar"]["right"] <= width + 1
                    assert geometry["hearts"] == geometry["footerHearts"] == 1, geometry
                    assert geometry["viewport"]["bottom"] <= geometry["bar"]["y"] + 2, geometry
                    for button in geometry["buttons"]:
                        assert button["aria"]
                        if width > 500:
                            assert button["labelDisplay"] != "none" and button["textRects"] == 1, button
                        else:
                            assert button["labelDisplay"] == "none" and button["box"]["width"] >= 44 and button["box"]["height"] >= 44, button
                    page.screenshot(path=str(artifacts / f"detail-{label}.png"))
                    passed(label + "-footer-layout", geometry)

                if args.suite == "full":
                    scenario(label + "-footer-layout", layout)
                if theme == "light":
                    context.close()
                    continue

                def keyboard():
                    open_long()
                    before_url = page.url
                    page.keyboard.press("ArrowUp")
                    assert page_number() == 1 and page.url == before_url
                    key_to("ArrowDown", 2)
                    key_to("ArrowDown", 3)
                    key_to("ArrowUp", 2)
                    key_to("ArrowUp", 1)
                    page.keyboard.press("ArrowDown")
                    page.keyboard.press("ArrowDown")
                    page.keyboard.press("ArrowUp")
                    expect(page.locator(".reading-counter small")).to_contain_text("第 2 / 2000 页")
                    page.wait_for_timeout(350)
                    assert page_number() == 2
                    passed(label + "-keyboard-single-page-boundary-and-latest-intent")
                    page.goto(origin + f"/illust/{other_id}")
                    page.locator(".image-page").first.wait_for()
                    old_url = page.url
                    page.keyboard.press("ArrowUp"); page.keyboard.press("ArrowDown")
                    page.wait_for_timeout(250)
                    assert page.url == old_url
                    passed(label + "-ordinary-single-image-boundaries-do-not-change-work")

                if args.suite == "full":
                    scenario(label + "-keyboard", keyboard)

                def no_keyboard_theft():
                    open_long()
                    if width > 500:
                        search = page.get_by_role("searchbox", name="搜索作品、画师或标签")
                        search.focus(); page.keyboard.press("ArrowDown")
                        assert page_number() == 1
                        page.locator(".page-viewport").focus()
                    else:
                        page.get_by_role("button", name="打开菜单", exact=True).click()
                        page.keyboard.press("ArrowDown")
                        assert page_number() == 1
                        page.keyboard.press("Escape")
                    page.get_by_role("button", name="放大此页", exact=True).first.click()
                    page.get_by_role("dialog", name="放大查看").wait_for()
                    page.keyboard.press("ArrowDown")
                    assert page_number() == 1
                    page.get_by_role("button", name="关闭放大").click()
                    page.get_by_role("button", name="整理作品", exact=True).click()
                    page.locator(".info-panel").wait_for()
                    page.keyboard.press("ArrowDown"); assert page_number() == 1
                    page.get_by_role("button", name="刷新作品信息", exact=True).click()
                    page.wait_for_timeout(100)
                    assert controls["metadata_requests"] == [{"kind": "metadata", "work_ids": [long_id]}]
                    passed(label + "-single-work-metadata-refresh-target", deepcopy(controls["metadata_requests"]))
                    page.get_by_role("button", name="永久删除", exact=True).click()
                    page.get_by_role("alertdialog", name="永久删除作品").wait_for()
                    page.keyboard.press("ArrowDown"); assert page_number() == 1
                    page.get_by_role("button", name="取消", exact=True).click()
                    page.get_by_role("button", name="关闭作品整理", exact=True).click()
                    passed(label + "-input-zoom-panel-and-dialog-do-not-steal-arrows")

                if args.suite == "full":
                    scenario(label + "-no-keyboard-theft", no_keyboard_theft)

                def artist_and_tags():
                    open_long()
                    translated = page.locator(".detail-tags .tag-translation")
                    expect(translated).to_have_count(1)
                    expect(translated).to_have_text("风景")
                    assert translated.get_attribute("data-source") == "pixiv"
                    colors = translated.evaluate("e=>({translation:getComputedStyle(e).color,original:getComputedStyle(e.previousElementSibling).color})")
                    assert colors["translation"] != colors["original"], colors
                    translated.click()
                    page.locator(".gallery-viewport").wait_for()
                    assert parse_qs(urlsplit(page.url).query).get("tags") == ["landscape"]
                    passed(label + "-pixiv-only-chinese-translation-and-original-tag-filter", colors)
                    # Ignore unrelated filters only in synthetic API responses so a
                    # visible detail can carry every excluded filter into this action.
                    unrelated = "?" + urlencode({"q": "local", "tags": "landscape", "types": "manga", "favorites": "1", "recent": "1", "sort": "oldest"})
                    controls["bypass_filters"] = True
                    page.goto(origin + f"/illust/{long_id}" + unrelated)
                    page.locator(".detail-meta .text-link").wait_for()
                    controls["queries"].clear()
                    page.locator(".detail-meta .text-link").click()
                    page.locator(".gallery-viewport").wait_for()
                    params = parse_qs(urlsplit(page.url).query)
                    assert params.get("artist_ids") == [str(original["artist_id"])]
                    assert params.get("sort") == ["oldest"]
                    assert not set(params).intersection({"q", "tags", "tags_exclude", "types", "favorites", "recent", "artist_ids_exclude"}), params
                    assert any(query.get("artist_ids") == [str(original["artist_id"])] and not set(query).intersection({"q", "tags", "types", "favorites", "recent"}) for query in controls["queries"])
                    controls["bypass_filters"] = False
                    passed(label + "-reader-artist-opens-complete-local-artist-gallery", params)
                    page.goto(origin + "/flow?sort=oldest")
                    page.locator(".page-viewport.feed .image-page").first.wait_for()
                    artist = page.locator(".page-artist").first
                    expect(artist).to_be_visible()
                    artist.click(); page.locator(".gallery-viewport").wait_for()
                    assert "artist_ids" in parse_qs(urlsplit(page.url).query)
                    passed(label + "-feed-artist-opens-local-gallery")

                if args.suite == "full":
                    scenario(label + "-artist-tags", artist_and_tags)

                if width == 1440:
                    def window_and_failure():
                        controls["delay"] = True
                        open_long()
                        for number in range(2, 134):
                            key_to("ArrowDown", number)
                            assert int(page.locator(".page-window").get_attribute("data-window-size")) <= 120
                        key_to("ArrowUp", 132)
                        passed("desktop-crosses-120-page-window-with-bounded-metadata")
                        for _ in range(60):
                            page.keyboard.press("ArrowDown")
                        page.keyboard.press("ArrowUp")
                        expect(page.locator(".reading-counter small")).to_contain_text("第 191 / 2000 页")
                        page.wait_for_timeout(650)
                        assert page_number() == 191 and controls["delayed_responses"] > 0
                        page.keyboard.down("ArrowDown")
                        page.wait_for_timeout(180)
                        page.keyboard.down("ArrowDown")
                        page.wait_for_timeout(180)
                        page.keyboard.down("ArrowDown")
                        page.keyboard.up("ArrowDown")
                        expect(page.locator(".reading-counter small")).to_contain_text("第 194 / 2000 页")
                        passed("desktop-delayed-response-new-intent-and-held-key-order", {"delayed_responses": controls["delayed_responses"]})
                        controls["delay"] = False
                        page.locator(".page-viewport").hover()
                        for _ in range(70):
                            page.mouse.wheel(0, 10000000)
                            page.wait_for_timeout(90)
                            assert int(page.locator(".page-window").get_attribute("data-window-size")) <= 120
                            if page_number() == 2000:
                                break
                        assert page_number() == 2000
                        terminal_url = page.url
                        terminal_top = page.locator(".page-viewport").evaluate("e=>e.scrollTop")
                        page.keyboard.press("ArrowDown")
                        page.wait_for_timeout(200)
                        assert page_number() == 2000 and page.url == terminal_url
                        assert abs(page.locator(".page-viewport").evaluate("e=>e.scrollTop") - terminal_top) < 2
                        passed("desktop-final-image-arrow-boundary-keeps-work-and-position")
                        controls.update(delay=False, inject_fault=True, failure=True)
                        open_long()
                        top = page.locator(".page-viewport").evaluate("e=>e.scrollTop")
                        page.keyboard.press("ArrowDown")
                        expect(page.locator(".reader-notice")).to_contain_text("当前画面已保留")
                        assert page_number() == 1
                        assert abs(page.locator(".page-viewport").evaluate("e=>e.scrollTop") - top) < 2
                        controls["failure"] = False
                        page.locator(".reader-notice").get_by_role("button", name="重试").click()
                        expect(page.locator(".reading-counter small")).to_contain_text("第 2 / 2000 页")
                        expect(page.locator(".reader-notice")).to_have_count(0)
                        passed("desktop-target-image-failure-retains-page-and-retry")
                        controls["inject_fault"] = False
                        page.goto(origin + "/flow?sort=oldest")
                        page.locator(".page-viewport.feed .image-page").first.wait_for()
                        first = page.locator(".reading-counter small").inner_text()
                        page.keyboard.press("ArrowDown")
                        expect(page.locator(".reading-counter small")).not_to_have_text(first)
                        second = page.locator(".reading-counter small").inner_text()
                        page.keyboard.press("ArrowUp")
                        expect(page.locator(".reading-counter small")).to_have_text(first)
                        passed("desktop-feed-steps-one-image-across-work", {"first": first, "next": second})

                    if args.suite == "full":
                        scenario("desktop-window-failure-feed", window_and_failure)

                    def bookmark_feedback():
                        for kind in ("fast", "slow", "failure"):
                            controls["bookmark"] = kind
                            facts.clear()
                            open_long()
                            heart = page.locator(".reading-toolbar .bookmark-button")
                            expect(heart).to_be_enabled()
                            result["metrics"][f"bookmark-{kind}-initial"] = {"button": heart.evaluate("e=>e.outerHTML"), "mock_state": deepcopy(fact(long_id))}
                            expect(heart).to_have_attribute("aria-pressed", "false")
                            begin = time.monotonic()
                            heart.click()
                            expect(heart).to_have_class(re.compile(r"\backnowledging\b"))
                            page.wait_for_function("getComputedStyle(document.querySelector('.bookmark-button.acknowledging')).color !== getComputedStyle(document.querySelector('.reader-nav-button')).color")
                            color = heart.evaluate("e=>getComputedStyle(e).color")
                            assert color != page.locator(".reader-nav-button").first.evaluate("e=>getComputedStyle(e).color")
                            if kind != "fast":
                                assert heart.get_attribute("aria-pressed") == "false"
                            if kind == "slow":
                                page.wait_for_timeout(3100)
                                assert "acknowledging" in heart.get_attribute("class") and heart.get_attribute("aria-pressed") == "false"
                            expect(heart).not_to_have_class(re.compile(r"\backnowledging\b"), timeout=9000)
                            elapsed = time.monotonic() - begin
                            assert elapsed >= 2.7, elapsed
                            assert heart.get_attribute("aria-pressed") == ("false" if kind == "failure" else "true")
                            expect(heart).to_be_enabled()
                            passed(f"desktop-bookmark-{kind}-feedback-fact-and-settlement", {"red_seconds": elapsed, "color": color})
                        controls["bookmark"] = "slow"
                        facts.clear(); open_long()
                        page.locator(".reading-toolbar .bookmark-button").click()
                        expect(page.locator(".reading-toolbar .bookmark-button")).to_have_class(re.compile(r"\backnowledging\b"))
                        page.get_by_role("button", name="下一件作品", exact=True).click()
                        page.wait_for_url(f"**/illust/{other_id}*")
                        other = page.locator(".reading-toolbar .bookmark-button")
                        expect(other).not_to_have_class(re.compile(r"\backnowledging\b"))
                        assert other.get_attribute("aria-pressed") == "false"
                        passed("desktop-bookmark-feedback-does-not-leak-to-next-work")

                    scenario("desktop-bookmark-feedback", bookmark_feedback)
                    if not args.exe:
                        def actual_bookmark_chain():
                            controls["real_bookmarks"] = True
                            open_long()
                            heart = page.locator(".reading-toolbar .bookmark-button")
                            expect(heart).to_have_attribute("aria-pressed", "false", timeout=10000)
                            expect(heart).to_be_enabled()
                            previous_writes = remote.write_count
                            heart.click()
                            expect(heart).to_have_class(re.compile(r"\backnowledging\b"))
                            expect(heart).to_have_attribute("aria-pressed", "true", timeout=10000)
                            expect(heart).not_to_have_class(re.compile(r"\backnowledging\b"), timeout=10000)
                            assert remote.write_count == previous_writes + 1
                            assert remote.values[str(100000 + long_id)] is True
                            actual = page.request.get(origin + f"/api/works/{long_id}/bookmark").json()
                            assert actual["status"] == "confirmed" and actual["value"] is True and actual["verified"] is True
                            passed("desktop-real-synthetic-backend-bookmark-write-and-readback", actual)
                        scenario("desktop-real-synthetic-backend-bookmark", actual_bookmark_chain)
                context.close()
            browser.close()
    finally:
        if process:
            stop.write_text("stop", encoding="utf-8")
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.terminate(); process.wait(timeout=10)
            if output:
                output.close()
        if server:
            server.should_exit = True
            thread.join(timeout=20)
        result["elapsed_seconds"] = time.monotonic() - started
        (artifacts / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not result["failures"] and not result["errors"], json.dumps(result["failures"] + result["errors"], ensure_ascii=False)
    print(json.dumps({"passed": len(result["checks"]), "artifacts": str(artifacts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
