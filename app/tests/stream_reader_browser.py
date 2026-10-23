"""Focused R-074 browser checks against the compiled production UI.

Uses three tiny synthetic works and local images. Long works and random reading
are deterministic API replays, never a large database or a real Pixiv account.
Run from app/: python tests/stream_reader_browser.py --artifacts NEW_DIRECTORY
Optional: --url http://127.0.0.1:PORT (an already isolated synthetic server),
--exe PATH (the existing packaged smoke server), --frontend-dist PATH.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen


def tiny_library(root):
    """Reuse existing fixture runtime/remote, without its default 2000-page work."""
    from client_fixture import SyntheticRemote
    from PIL import Image, ImageDraw
    from src.core.models import Artist, Asset, Derivative, TrackingPolicy, Work
    from src.core.runtime import Runtime
    root.mkdir(parents=True, exist_ok=False)
    runtime = Runtime(root / "data/pixiv_tracker.db", root / "images", create=True, client=SyntheticRemote())
    runtime.account_id = runtime.library.account_id = "90001"
    with runtime.database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="90001", name="R074 local artist"))
        session.flush()
        session.add(TrackingPolicy(artist_id=1, mode="active"))
        for identity, count in ((1, 3), (2, 1), (3, 1)):
            session.add(Work(id=identity, pixiv_illust_id=str(900000 + identity), artist_id=1,
                title=f"R074 synthetic work {identity}", type="manga" if count > 1 else "illust",
                page_count=count, caption="<p>R074 local full caption.</p>",
                caption_state="available", posted_at=datetime(2026, 10, identity)))
            session.flush()
            source = runtime.images / f"fixture-{identity}.png"
            with Image.new("RGB", (640, 860), (40 + identity*25, 65, 115)) as picture:
                ImageDraw.Draw(picture).text((30, 30), f"R074 SYNTHETIC {identity}", fill="white")
                picture.save(source, format="PNG")
                picture.thumbnail((360, 360))
                picture.save(runtime.images / f"fixture-{identity}.webp", format="WEBP")
            stat = source.stat()
            for part in range(count):
                session.add(Asset(work_id=identity, role="image", page_index=part,
                    relative_path=source.name, state="verified", width=640, height=860,
                    sha256=hashlib.sha256(source.read_bytes()).hexdigest(), byte_size=stat.st_size,
                    file_mtime_ns=stat.st_mtime_ns, file_identity=stat.st_ino))
            session.add(Derivative(work_id=identity, kind="thumbnail", state="verified",
                relative_path=f"fixture-{identity}.webp", width=360, height=360))
    return runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--url", help="Only an explicitly isolated synthetic local server")
    mode.add_argument("--exe", type=Path)
    parser.add_argument("--frontend-dist", type=Path)
    parser.add_argument("--suite", choices=("full", "chrome", "screenshots"), default="full")
    args = parser.parse_args()
    out = args.artifacts.resolve()
    out.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(out / "configuration")
    os.environ["TEMP"] = os.environ["TMP"] = str(out)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    result = {"checks": [], "failures": [], "errors": [], "metrics": {},
        "real_library_used": False, "real_account_used": False, "real_phone_tested": False,
        "mode": "url" if args.url else "exe" if args.exe else "source",
        "backend": "isolated local service; reader metadata/bookmarks replayed in browser",
        "fixture": "3 works / 5 actual pages in source mode; 2000-page metadata is replay only",
        "input": "Chromium actual mouse wheel and CDP touch; no physical phone"}
    server = worker = process = output = runtime = page = None
    stop = out / "stop.signal"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = (args.url or f"http://127.0.0.1:{port}").rstrip("/")
    assert urlsplit(origin).hostname in ("127.0.0.1", "localhost", "::1"), "Only loopback synthetic servers"

    def fetch(path):
        with urlopen(origin + path, timeout=8) as response:
            return response.read()

    def read(path):
        return json.loads(fetch(path))

    def scenario(name, action):
        try:
            metric = action()
            result["checks"].append(name)
            if metric is not None:
                result["metrics"][name] = metric
            print("PASS " + name, flush=True)
        except Exception as error:
            result["failures"].append({"name": name, "error": str(error), "traceback": traceback.format_exc()})
            if page and not page.is_closed():
                page.screenshot(path=str(out / (name + "-failure.png")))
            print("FAIL " + name + ": " + str(error), flush=True)

    try:
        if args.exe:
            exe = args.exe.resolve(strict=True)
            result.update(exe=str(exe), exe_sha256=hashlib.sha256(exe.read_bytes()).hexdigest())
            output = (out / "exe-output.txt").open("xb")
            process = subprocess.Popen([str(exe), "--smoke-test", "--smoke-output", str(out / "exe-smoke.json"),
                "--smoke-serve-port", str(port), "--smoke-stop-file", str(stop)], cwd=out,
                env=os.environ.copy(), stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        elif not args.url:
            import uvicorn
            runtime = tiny_library(out / "fixture")
            from src.web import create_app
            server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False,
                schedule=False, reading_ui=True), host="127.0.0.1", port=port, log_level="error",
                loop="src.networking:http_loop_factory"))
            worker = threading.Thread(target=server.run, daemon=True)
            worker.start()
        deadline = time.monotonic() + (80 if args.exe else 25)
        while time.monotonic() < deadline:
            if process and process.poll() is not None:
                raise RuntimeError(f"Isolated EXE exited: {process.returncode}")
            try:
                read("/api/status")
                break
            except OSError:
                time.sleep(.15)
        else:
            raise TimeoutError("Isolated server did not start")

        bases = [read(f"/api/works/{identity}?page_limit=1") for identity in (1, 2, 3)]
        # Only the first static PNG is reused. Every expanded page remains synthetic.
        source = bases[0]["pages"][0]["src"]
        dist = args.frontend_dist.resolve(strict=True) if args.frontend_dist else None
        html = (dist / "index.html").read_bytes() if dist else fetch("/")
        assets = re.findall(r'/reader/assets/[^"\s>]+', html.decode("utf-8"))
        assert assets, "Expected a compiled production UI"
        result["frontend_sha256"] = {"index.html": hashlib.sha256(html).hexdigest()}
        for asset in assets:
            data = (dist / asset.removeprefix("/reader/")).read_bytes() if dist else fetch(asset)
            result["frontend_sha256"][asset] = hashlib.sha256(data).hexdigest()
        from src.auth import _find_system_browser
        from playwright.sync_api import expect, sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            for width in (1440, 390):
                context = browser.new_context(viewport={"width": width, "height": 900},
                    is_mobile=width < 500, has_touch=width < 500, device_scale_factor=1)
                page = context.new_page()
                page.set_default_timeout(7000)
                page.on("pageerror", lambda error: result["errors"].append(str(error)))
                page.add_init_script("localStorage.clear();localStorage.setItem('pixiv-local-theme','dark')")
                control = {"long": False, "requests": [], "writes": [], "details": [], "session": 0}
                facts = {}

                def bookmark(identity):
                    return facts.setdefault(identity, {"work_id": identity, "operation_id": None,
                        "status": "confirmed", "desired": None, "value": False, "error": None,
                        "version": 1000, "verified": True, "cached": False,
                        "confirmed_at": "2026-10-20T00:00:00Z", "fact_account_id": "90001",
                        "current_account_id": "90001", "account_epoch": 1000})

                def counts():
                    return [2000 if control["long"] else 3, 1, 1]

                def ordering(random=False, round_number=0):
                    return ([2, 1, 3] if round_number % 2 == 0 else [3, 2, 1]) if random else [1, 2, 3]

                def replay_page(index, random=False):
                    span = sum(counts()); round_number, relative = divmod(index, span)
                    order = ordering(random, round_number)
                    start = 0
                    for position, identity in enumerate(order):
                        count = counts()[identity - 1]
                        if relative < start + count:
                            part = relative - start
                            base = bases[identity - 1]
                            return {**deepcopy(base["pages"][0]), "src": source, "preview": None,
                                "width": 640, "height": 860, "index": index, "page": part,
                                "work_id": identity, "work_index": position, "page_count": count,
                                "next_work_index": round_number * span + start + count,
                                "previous_work_index": round_number * span + start - counts()[order[position-1]-1]
                                    if position else (round_number * span - counts()[ordering(True, round_number-1)[-1]-1]
                                        if random and round_number else None),
                                "round": round_number, "has_next_work": random or position < 2,
                                "work_cursor": f"r074-{identity}", "revision_id": base.get("revision_id"),
                                **{key: base.get(key) for key in ("title", "artist_name", "artist_id", "pixiv_id", "type", "x_restrict")},
                                "bookmark": deepcopy(bookmark(identity))}
                        start += count
                    raise AssertionError(index)

                def batch(q, random=False):
                    limit = int(q.get("limit", [48])[0]); requested = int(q.get("index" if not random else "offset", [0])[0])
                    start = max(0, requested - limit + 1) if q.get("direction") == ["previous"] else requested
                    total = sum(counts()) * (3 if random else 1)
                    end = min(total, start + limit)
                    control["requests"].append({"start": start, "limit": limit, "long": control["long"], "random": random})
                    return {"items": [replay_page(index, random) for index in range(start, end)],
                        "total": total, "offset": start, "has_more": end < total,
                        "generation": 900, "catalogue_revision": 900, "work_total": 3,
                        "ceiling": 3, "as_of": "2026-10-20T00:00:00Z"}

                def route_api(route):
                    path = urlsplit(route.request.url).path
                    q = parse_qs(urlsplit(route.request.url).query)
                    if path == "/api/library/cursor/feed":
                        route.fulfill(json=batch(q)); return
                    if path == "/api/reading-sessions" and route.request.method == "POST":
                        control["session"] += 1
                        route.fulfill(json={"session_id": "r074-local", "total": 3, "page_total": sum(counts())}); return
                    if path.startswith("/api/reading-sessions/"):
                        if path.endswith("/feed"):
                            route.fulfill(json=batch(q, True)); return
                        if path.endswith("/locate"):
                            identity = int(q.get("work_id", [1])[0]); order = ordering(True)
                            start = sum(counts()[item-1] for item in order[:order.index(identity)])
                            item = replay_page(start, True)
                            route.fulfill(json={**item, "offset": 0}); return
                        if path.endswith("/entry"):
                            index = int(q.get("index", [0])[0]); identity = ordering(True)[index]
                            route.fulfill(json={"work_id": identity, "work_index": index,
                                "revision_id": bases[identity-1].get("revision_id"), "page_count": counts()[identity-1], "total": 3}); return
                        route.fulfill(json={"ok": True}); return
                    match = re.fullmatch(r"/api/works/(\d+)(/bookmark(?:/refresh)?|/cursor-neighbors)?", path)
                    if match:
                        identity = int(match[1]); state = bookmark(identity)
                        if match[2] and match[2].startswith("/bookmark"):
                            if route.request.method == "POST" and match[2] == "/bookmark":
                                desired = route.request.post_data_json["desired"]
                                control["writes"].append({"work_id": identity, "desired": desired})
                                state.update(value=desired, desired=desired, version=state["version"]+1)
                            route.fulfill(json=deepcopy(state)); return
                        if match[2] == "/cursor-neighbors":
                            def neighbor(item):
                                return None if not 1 <= item <= 3 else {"work_id": item,
                                    "revision_id": bases[item-1].get("revision_id"), "page_count": counts()[item-1], "cursor": f"r074-{item}"}
                            route.fulfill(json={"anchor_cursor": f"r074-{identity}", "generation": 900,
                                "previous": neighbor(identity-1), "next": neighbor(identity+1)}); return
                        control["details"].append(identity)
                        offset = int(q.get("page_offset", [0])[0]); limit = int(q.get("page_limit", [48])[0])
                        data = deepcopy(bases[identity-1]); count = counts()[identity-1]
                        data.update(page_count=count, display_page_count=count, bookmark=deepcopy(state),
                            caption="<p>R074_FULL_CAPTION_START</p><p>" + "完整本地简介。" * 180 + "</p><p>R074_FULL_CAPTION_END</p>",
                            caption_state="available", tags=["r074_local_tag", "second_tag"],
                            tag_details=[{"name": "r074_local_tag", "translated_name": "本地标签", "language": "zh-cn", "source": "pixiv"}],
                            pages=[{"index": part, "src": source, "width": 640, "height": 860, "state": "verified"}
                                for part in range(offset, min(count, offset+limit))])
                        route.fulfill(json=data); return
                    if route.request.method not in ("GET", "HEAD") and path != "/api/diagnostics/events":
                        raise AssertionError(f"Unexpected mutation in isolated browser check: {path}")
                    route.continue_()

                page.route("**/api/**", route_api)
                if dist:
                    def frontend(route):
                        path = urlsplit(route.request.url).path
                        target = dist / (path.removeprefix("/reader/") if path.startswith("/reader/") else "index.html")
                        assert target.resolve().is_relative_to(dist)
                        route.fulfill(path=str(target), content_type=mimetypes.guess_type(str(target))[0])
                    page.route(re.compile(r".*/reader/.*"), frontend)
                    page.route(re.compile(re.escape(origin) + r"/(?:flow|illust/\d+)?(?:\?.*)?$"), frontend)
                cdp = context.new_cdp_session(page)

                def touch(kind, points):
                    cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [
                        {"x": x, "y": y, "id": index, "radiusX": 2, "radiusY": 2}
                        for index, (x, y) in enumerate(points)]})

                def tap(x, y):
                    touch("touchStart", [(x, y)]); page.wait_for_timeout(35); touch("touchEnd", [])

                def point(locator):
                    box = locator.bounding_box(); assert box
                    return box["x"] + box["width"] / 2, max(160, min(640, box["y"] + box["height"] / 2))

                def swipe(x, y, distance=350):
                    touch("touchStart", [(x, y)])
                    for step in range(1, 11):
                        touch("touchMove", [(x, y-distance*step/10)]); page.wait_for_timeout(18)
                    touch("touchEnd", [])

                def fresh(long=False):
                    control["long"] = long
                    page.goto(origin + "/flow?sort=oldest", wait_until="domcontentloaded")
                    page.locator(".stream-viewport").wait_for()
                    page.wait_for_function("document.querySelector('.inline-zoom-media img')?.naturalWidth > 0")
                    page.wait_for_timeout(180)

                def identity():
                    return int(page.locator(".stream-reader").get_attribute("data-stream-work"))

                def viewport():
                    return page.locator(".stream-viewport")

                def bottom():
                    viewport().evaluate("e=>e.scrollTop=e.scrollHeight")
                    page.wait_for_timeout(180)

                def geometry():
                    metric = page.evaluate("""() => ({overflow:document.documentElement.scrollWidth>innerWidth,
                      boxes:[...document.querySelectorAll('.stream-top,.stream-dock,.stream-caption-trigger')].map(e=>{
                        const r=e.getBoundingClientRect();return {name:e.className,x:r.x,right:r.right,y:r.y,bottom:r.bottom}})})""")
                    assert not metric["overflow"], metric
                    assert len(metric["boxes"]) >= 3, metric
                    assert all(box["x"] >= -.5 and box["right"] <= page.viewport_size["width"]+.5 for box in metric["boxes"]), metric
                    return metric

                if args.suite == "screenshots":
                    fresh()
                    page.wait_for_timeout(300)
                    scenario(f"{width}-final-static-geometry",geometry)
                    page.screenshot(path=str(out/f"stream-final-{width}.png"))
                    if width==390:
                        page.set_viewport_size({"width":320,"height":900});page.wait_for_timeout(300)
                        scenario("320-final-static-geometry",geometry)
                        page.screenshot(path=str(out/"stream-final-320.png"))
                    context.close()
                    continue

                if args.suite == "chrome":
                    def stable_chrome():
                        fresh()
                        trigger = page.locator(".stream-caption-trigger")
                        expect(trigger).to_contain_text("R074_FULL_CAPTION_START")
                        details_before = len(control["details"])
                        image = page.locator(".inline-zoom-media img").first
                        media = page.locator(".inline-zoom-media").first
                        x, y = point(image)
                        if width > 500: image.dblclick(position={"x":x-image.bounding_box()["x"],"y":y-image.bounding_box()["y"]})
                        else: tap(x,y); page.wait_for_timeout(80); tap(x,y)
                        expect(media).to_have_attribute("data-inline-zoom-active", "true")
                        scale = media.get_attribute("data-inline-zoom-scale")
                        box = viewport().bounding_box(); gx=box["x"]+4; gy=min(700,box["y"]+box["height"]-60)
                        if width > 500: page.mouse.move(gx,gy); page.mouse.wheel(0,110)
                        else: swipe(gx,gy,110)
                        page.wait_for_function("document.querySelector('.stream-viewport').scrollTop > 40")
                        page.wait_for_timeout(200)
                        expect(media).to_have_attribute("data-inline-zoom-scale", scale)
                        assert len(control["details"]) == details_before, control["details"]
                        expect(trigger).to_contain_text("R074_FULL_CAPTION_START")
                        trigger.click()
                        sheet = page.locator(".stream-sheet")
                        expect(sheet).to_contain_text("R074_FULL_CAPTION_END")
                        panel = sheet.locator(".stream-details")
                        bounds=panel.bounding_box(); assert bounds
                        page.mouse.move(bounds["x"]+bounds["width"]/2,bounds["y"]+bounds["height"]/2)
                        page.mouse.wheel(0,2400)
                        page.wait_for_function("document.querySelector('.stream-details').scrollTop > 30")
                        end=sheet.locator(".stream-full-caption p").last.bounding_box()
                        assert end and end["y"] >= bounds["y"]-2 and end["y"]+end["height"] <= bounds["y"]+bounds["height"]+2, (end,bounds)
                        sheet.get_by_role("button",name="关闭作品简介",exact=True).click()
                        page.wait_for_timeout(250)
                        def dock_text():
                            metric=page.locator(".stream-caption-trigger").evaluate("""e=>{
                              const box=n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom}};
                              return {trigger:box(e),name:box(e.querySelector('strong')),caption:box(e.querySelector(':scope > span')),dock:box(e.parentElement)}}""")
                            assert metric["name"]["height"]<=25 and metric["caption"]["height"]<=22, metric
                            assert metric["name"]["bottom"]<=metric["caption"]["y"]+1,metric
                            assert metric["caption"]["bottom"]<=metric["dock"]["bottom"]-4,metric
                            return metric
                        metric={"metadata_requests_before":details_before,"metadata_requests_after_scroll":len(control["details"]),
                            "preserved_zoom_scale":scale,"caption_panel_scroll_top":panel.evaluate("e=>e.scrollTop") if panel.count() else "closed",
                            "dock":dock_text(),"geometry":geometry()}
                        page.screenshot(path=str(out/f"stream-final-{width}.png"))
                        if width==390:
                            page.set_viewport_size({"width":320,"height":900}); page.wait_for_timeout(200)
                            metric["320_dock"]=dock_text();metric["320_geometry"]=geometry()
                            page.screenshot(path=str(out/"stream-final-320.png"))
                        page.get_by_role("button",name="查看画师全部作品",exact=True).click()
                        page.locator(".gallery-viewport").wait_for()
                        assert parse_qs(urlsplit(page.url).query).get("artist_ids")==[str(bases[0]["artist_id"])],page.url
                        assert page.locator(".work-card").count()==3
                        metric["artist_all_works_url"]=page.url
                        return metric
                    scenario(f"{width}-final-chrome-stability-sheet-scroll-and-artist",stable_chrome)
                    context.close()
                    continue

                def edge_scroll():
                    fresh(); before = identity(); v = viewport()
                    v.evaluate("e=>e.scrollTop=Math.max(0,e.scrollHeight-e.clientHeight-110)")
                    page.wait_for_timeout(180)
                    box = v.bounding_box(); x=box["x"]+12; y=min(730, box["y"]+box["height"]-50)
                    if width > 500:
                        page.mouse.move(x, y)
                        for _ in range(5):
                            page.mouse.wheel(0, 140); page.wait_for_timeout(22)
                    else:
                        swipe(x, y, 320)
                    page.wait_for_timeout(500)
                    assert identity() == before, "The gesture/burst that reached the edge changed work"
                    assert v.evaluate("e=>e.scrollHeight-e.clientHeight-e.scrollTop") < 4
                    if width > 500:
                        page.mouse.wheel(0, 320)
                    else:
                        swipe(x, y, 240)
                    page.wait_for_function("document.querySelector('.stream-reader')?.dataset.streamWork === '2'")
                    page.wait_for_timeout(450)
                    assert identity() == 2, "One edge gesture changed more than one work"
                    return {"first_gesture_work": before, "second_edge_gesture_work": identity()}

                scenario(f"{width}-edge-gesture-latch", edge_scroll)

                def zoom():
                    fresh(); media = page.locator(".inline-zoom-media").first
                    img = media.locator("img"); x,y=point(img)
                    baseline=viewport().evaluate("e=>e.scrollTop"); work=identity()
                    if width > 500:
                        img.dblclick(position={"x":x-img.bounding_box()["x"], "y":y-img.bounding_box()["y"]})
                    else:
                        tap(x,y); page.wait_for_timeout(80); tap(x,y)
                    expect(media).to_have_attribute("data-inline-zoom-active", "true")
                    initial=float(media.get_attribute("data-inline-zoom-scale"))
                    if width > 500:
                        page.mouse.move(x,y); page.mouse.wheel(0,-200); page.wait_for_timeout(100)
                    else:
                        touch("touchStart", [(x-35,y),(x+35,y)])
                        for step in range(1,7):
                            touch("touchMove", [(x-35-step*6,y),(x+35+step*6,y)]); page.wait_for_timeout(20)
                        touch("touchEnd", [])
                    page.wait_for_timeout(100)
                    enlarged=float(media.get_attribute("data-inline-zoom-scale"))
                    assert enlarged>initial, (initial,enlarged)
                    assert abs(viewport().evaluate("e=>e.scrollTop")-baseline)<3 and identity()==work
                    # Reset through the actual image's second double action.
                    page.wait_for_timeout(750)
                    if width > 500: img.dblclick(position={"x":x-img.bounding_box()["x"],"y":y-img.bounding_box()["y"]})
                    else: tap(x,y); page.wait_for_timeout(80); tap(x,y)
                    expect(media).to_have_attribute("data-inline-zoom-active", "false")
                    page.wait_for_timeout(750)
                    if width > 500: img.dblclick(position={"x":x-img.bounding_box()["x"],"y":y-img.bounding_box()["y"]})
                    else: tap(x,y); page.wait_for_timeout(80); tap(x,y)
                    expect(media).to_have_attribute("data-inline-zoom-active", "true")
                    page.wait_for_timeout(300)
                    box=viewport().bounding_box(); gx=box["x"]+4; gy=min(700,box["y"]+box["height"]-60)
                    if width>500: page.mouse.move(gx,gy); page.mouse.wheel(0,260)
                    else: swipe(gx,gy,250)
                    page.wait_for_function("document.querySelector('.stream-viewport').scrollTop > 40")
                    assert not control["writes"], control["writes"]
                    return {"double_action_scale":initial,"wheel_or_pinch_scale":enlarged,
                        "gutter_scrolled_while_image_zoomed":True,"gutter_scroll_top":viewport().evaluate("e=>e.scrollTop")}

                scenario(f"{width}-inline-zoom-and-gutter-scroll", zoom)

                def caption_and_heart():
                    fresh(); control["writes"].clear()
                    page.locator(".stream-caption-trigger").click()
                    panel=page.locator(".stream-sheet[role=dialog]")
                    expect(panel).to_be_visible()
                    expect(panel).to_contain_text("R074_FULL_CAPTION_START")
                    expect(panel).to_contain_text("R074_FULL_CAPTION_END")
                    expect(panel).to_contain_text("r074_local_tag")
                    before=identity(); requests=len(control["details"])
                    # The close control is the panel's explicit accessible button.
                    panel.get_by_role("button",name="关闭作品简介",exact=True).click()
                    page.locator(".stream-caption-trigger").click()
                    expect(panel).to_be_visible(); assert len(control["details"])==requests, "Reopening local information refetched metadata"
                    panel.get_by_role("button",name="关闭作品简介",exact=True).click()
                    hearts=page.locator(".stream-dock .bookmark-button")
                    expect(hearts).to_have_count(1); hearts.click()
                    page.wait_for_function("document.querySelector('.stream-dock .bookmark-button')?.getAttribute('aria-pressed') === 'true'")
                    assert control["writes"]==[{"work_id":before,"desired":True}], control["writes"]
                    return {"target_work":before,"writes":deepcopy(control["writes"]),"cached_reopen":True}

                scenario(f"{width}-full-local-caption-tags-and-shared-heart",caption_and_heart)
                scenario(f"{width}-geometry",geometry)
                page.screenshot(path=str(out/f"stream-{width}.png"))

                if width==1440:
                    def bounded_long_work():
                        fresh(long=True)
                        for _ in range(125):
                            page.keyboard.press("ArrowDown")
                            page.wait_for_timeout(65)
                        page.wait_for_timeout(400)
                        metric=page.evaluate("""() => ({window:Number(document.querySelector('.stream-window')?.dataset.windowSize),
                          images:document.querySelectorAll('.stream-viewport img').length,
                          pages:document.querySelectorAll('.stream-viewport .stream-page').length,
                          range:[...document.querySelectorAll('.stream-page')].map(e=>Number(e.dataset.page))})""")
                        assert 0<metric["window"]<=120,metric
                        assert metric["images"]<=12 and metric["pages"]<=12,metric
                        assert any(item["long"] and item["start"]>=100 for item in control["requests"]), control["requests"]
                        assert max(item["limit"] for item in control["requests"])<=120
                        return {**metric,"metadata_only_pages":2000,"actual_source_pages":5}
                    scenario("1440-long-work-bounded-window-replay",bounded_long_work)

                    def random_short():
                        fresh()
                        page.get_by_label("流式阅览顺序",exact=True).select_option("random")
                        page.wait_for_timeout(400)
                        first=identity(); page.get_by_role("button",name="下一件作品",exact=True).click()
                        page.wait_for_function("old=>document.querySelector('.stream-reader')?.dataset.streamWork !== String(old)",arg=first)
                        second=identity(); page.get_by_role("button",name="上一件作品",exact=True).click()
                        page.wait_for_function("old=>document.querySelector('.stream-reader')?.dataset.streamWork === String(old)",arg=first)
                        assert first!=second and control["session"]>0
                        return {"first":first,"next":second,"previous":identity(),"scope":"deterministic session API replay, not random distribution acceptance"}
                    scenario("1440-short-random-next-previous-replay",random_short)

                def ordinary_detail():
                    control["long"]=False
                    page.goto(origin+"/illust/1?sort=oldest",wait_until="domcontentloaded")
                    page.locator(".page-viewport.reader .image-page img").first.wait_for()
                    page.wait_for_function("document.querySelector('.page-viewport.reader img')?.naturalWidth > 0")
                    page.keyboard.press("ArrowDown")
                    expect(page.locator(".reading-counter small")).to_contain_text("第 2 / 3 页")
                    page.keyboard.press("ArrowUp")
                    expect(page.locator(".reading-counter small")).to_contain_text("第 1 / 3 页")
                    page.locator(".page-viewport.reader .image-page img").first.dblclick()
                    expect(page.locator(".page-viewport.reader .inline-zoom-media").first).to_have_attribute("data-inline-zoom-scale", "2.25")
                    page.get_by_role("button",name="放大此页",exact=True).first.click()
                    expect(page.get_by_role("dialog",name="放大查看",exact=True)).to_be_visible()
                    page.get_by_role("button",name="关闭放大",exact=False).click()
                    expect(page.locator(".page-viewport.reader .inline-zoom-media").first).to_have_attribute("data-inline-zoom-scale", "1")
                scenario(f"{width}-ordinary-detail-doubleclick-and-arrows",ordinary_detail)
                if width==390:
                    fresh(); page.set_viewport_size({"width":320,"height":900}); page.wait_for_timeout(180)
                    scenario("320-geometry-only",geometry)
                    page.screenshot(path=str(out/"stream-320.png"))
                context.close()
            browser.close()
    except Exception:
        result["failures"].append({"name":"setup-or-harness","traceback":traceback.format_exc()})
        raise
    finally:
        if server:
            server.should_exit=True
            worker.join(15)
            if worker.is_alive(): result["failures"].append({"name":"shutdown","error":"Isolated server did not stop"})
        if process:
            stop.write_text("stop",encoding="utf-8")
            try: process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                result["failures"].append({"name":"shutdown","error":"Smoke EXE did not acknowledge stop file"})
        if output: output.close()
        (out/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    if result["failures"] or result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
