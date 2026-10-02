"""Unified maintenance/source management on production UI and synthetic data.

Source mode uses the real synthetic backend for source reads and task submits.
EXE mode uses its own smoke fixture; additional source/artist/long-gallery facts
are explicitly browser response replays. Neither mode reads a real library.
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
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--exe", type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    repository = Path(__file__).resolve().parents[2]
    root = repository.parents[1] if repository.parent.name == ".worktrees" else repository
    evidence_root = (root / ".devtools/test-runs").resolve()
    if not artifacts.is_relative_to(evidence_root) or not artifacts.name.startswith("b12-"):
        raise ValueError("Artifacts must be a fresh ROOT/.devtools/test-runs/b12-* directory")
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "fixture")
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    result = {"checks": [], "failures": [], "errors": [], "metrics": {}, "submitted": [],
        "mode": "exe" if args.exe else "source", "real_library_used": False,
        "real_account_used": False, "real_phone_tested": False,
        "layers": {"task_submission": "real synthetic backend", "source_records": "browser response replay" if args.exe else "real synthetic database",
                   "faults": "browser response replay", "scroll": "real wheel or CDP touch", "device": "desktop browser / emulated mobile viewport",
                   "large_gallery": "960 synthetic work metadata clones via browser GET replay; existing synthetic media; real production virtualizer and wheel"},
        "browser_only_large_gallery_works": 960}
    server = thread = runtime = process = output = page = None
    stop = artifacts / "stop.signal"
    started = time.monotonic()

    def read(path):
        with urlopen(origin + path, timeout=10) as response:
            return json.loads(response.read())

    def passed(name, metrics=None):
        result["checks"].append(name)
        if metrics is not None:
            result["metrics"][name] = metrics
        print(name, flush=True)

    def scenario(name, action):
        try:
            action()
        except Exception as exc:
            result["failures"].append({"name": name, "error": str(exc), "traceback": traceback.format_exc()})
            if page and not page.is_closed():
                try:
                    page.screenshot(path=str(artifacts / f"failure-{name}.png"))
                except Exception:
                    pass
            print(f"FAILED {name}: {exc}", flush=True)

    try:
        if args.exe:
            exe = args.exe.resolve(strict=True)
            result.update(exe=str(exe), exe_sha256=hashlib.sha256(exe.read_bytes()).hexdigest(), actual_database_works=3)
            output = (artifacts / "exe-output.txt").open("xb")
            environment = os.environ.copy()
            environment["TEMP"] = environment["TMP"] = str(artifacts)
            process = subprocess.Popen([str(exe), "--smoke-test", "--smoke-output", str(artifacts / "exe-smoke.json"),
                "--smoke-serve-port", str(port), "--smoke-stop-file", str(stop)], cwd=artifacts, env=environment,
                stdout=output, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            from client_fixture import make_library
            from sqlalchemy import select
            from src.core.catalogue_revision import bump_catalogue_revision
            from src.core.models import ApplicationState, Artist, Asset, Derivative, Work
            from src.core.source_status import ARTIST_PREFIX, DeletionEvidence
            import uvicorn
            runtime, remote = make_library(artifacts / "fixture", works=144)
            from src.client import PixivRequestError
            from src.web import create_app
            remote.source_failure = True
            remote.profile_calls = []

            def synthetic_homepage(remote_id):
                remote.profile_calls.append(str(remote_id))
                if remote.source_failure:
                    raise PixivRequestError(404, "合成主页暂不可读", failure_code="artist_unavailable", retryable=False)
                return {"user_id": str(remote_id), "name": "恢复的合成主页", "biography": ""}

            remote.get_artist_detail = synthetic_homepage
            with runtime.database.write() as session:
                for work in session.scalars(select(Work).where(Work.id <= 76)):
                    work.source_metadata = {"remote_availability": {"state": "unavailable", "confirmed": False,
                        "checked_at": "2026-10-03T09:00:00+00:00", "reason": "合成 404，尚不能确认删除；已有存档保留"}}
                for asset in session.scalars(select(Asset).where(Asset.work_id == 5, Asset.page_index == 1)):
                    asset.state, asset.relative_path = "missing", None
                motion = session.get(Work, 3)
                motion.type = "ugoira"
                from PIL import Image
                Image.new("RGB", (120, 120), "#4499bb").save(runtime.images / "synthetic-retained.gif", format="GIF")
                session.add_all([Asset(work_id=3, role="ugoira_zip", state="missing"),
                    Asset(work_id=3, role="ugoira_timing", state="missing"),
                    Derivative(work_id=3, kind="gif", state="verified", relative_path="synthetic-retained.gif")])
                for artist_id in (1, 2):
                    artist = session.get(Artist, artist_id)
                    session.add(ApplicationState(key=ARTIST_PREFIX + artist.pixiv_user_id,
                        value={"state": "unavailable", "confirmed": False, "checked_at": "2026-10-03T09:00:00+00:00", "reason": "合成来源待核对"}))
                bump_catalogue_revision(session)
            version = runtime.source_status.begin("work", 1, "90001")
            runtime.source_status.apply_deleted("work", 1, DeletionEvidence("100001", "reviewed synthetic explicit evidence", True, True), version, "90001")
            server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True),
                host="127.0.0.1", port=port, log_level="error", loop="src.networking:http_loop_factory"))
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            result["actual_database_works"] = 144
        deadline = time.monotonic() + 80
        while time.monotonic() < deadline:
            if process and process.poll() is not None:
                raise RuntimeError(f"Synthetic EXE exited: {process.returncode}")
            try:
                read("/api/status")
                break
            except OSError:
                time.sleep(.15)
        else:
            raise TimeoutError("Synthetic server failed to start")
        from src.auth import _find_system_browser
        from playwright.sync_api import expect, sync_playwright
        library = read("/api/library?limit=3&compact=1")
        base_work = library["items"][0]
        actual_artists = read("/api/artists?limit=50")["items"]
        first_artist = deepcopy(actual_artists[0]) if args.exe else read("/api/artists/1")
        artist_id = first_artist["id"]
        second_id = artist_id + 1 if args.exe else 2
        first_name = first_artist["name"]
        reader_id = base_work["id"] if args.exe else 1
        revision = library.get("catalogue_revision", 0) + 1
        with urlopen(origin + "/", timeout=10) as response:
            html = response.read()
        assets = re.findall(r'/reader/assets/[^"\']+', html.decode("utf-8"))
        assert assets, "Production frontend build is not served"
        result["served_frontend_sha256"] = {"index.html": hashlib.sha256(html).hexdigest()}
        for asset in assets:
            with urlopen(origin + asset, timeout=10) as response:
                result["served_frontend_sha256"][asset] = hashlib.sha256(response.read()).hexdigest()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            for width, height in ((1440, 900), (390, 844), (320, 740)):
                if runtime:
                    version = runtime.source_status.begin("artist", artist_id, "90001")
                    runtime.source_status.apply_failure("artist", artist_id,
                        PixivRequestError(404, "synthetic unavailable", failure_code="artist_unavailable"), version, "90001")
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=width < 500, has_touch=width < 500)
                page = context.new_page()
                page.set_default_timeout(10000)
                page.add_init_script("localStorage.removeItem('pixiv-local-positions-v1');localStorage.setItem('pixiv-local-artist-layout','grid')")
                page.on("pageerror", lambda error, width=width: result["errors"].append({"width": width, "error": str(error)}))
                controls = {"fail_sources": False, "deleted_artist": False, "recovered_artist": False,
                            "homepage_submitted": [], "source_requests": [], "large_gallery": False,
                            "window_requests": [], "window_transitions": []}

                def availability(deleted=False, recovered=False):
                    return {"state": "available" if recovered else "deleted" if deleted else "unavailable",
                        "checked_at": "2026-10-03T09:00:00+00:00", "reason": None if recovered else "合成明确证据" if deleted else "合成 404，尚不能确认删除",
                        "confirmation": "confirmed" if deleted or recovered else "unconfirmed", "confirmed": deleted or recovered}

                def replay_artist(identity):
                    artist = deepcopy(first_artist)
                    artist.update(id=identity, pixiv_id=first_artist["pixiv_id"] if identity == artist_id else str(identity),
                        name=first_name if identity == artist_id else "另一位合成画师", work_count=120,
                        availability=availability(controls["deleted_artist"] and identity == artist_id,
                                                  controls["recovered_artist"] and identity == artist_id))
                    recent = artist.get("recent_works") or [base_work]
                    artist["recent_works"] = [{**deepcopy(recent[index % len(recent)]), "id": index + 10000, "title": f"隔离近期作品 {index + 1}"} for index in range(12)]
                    return artist

                def source_route(route):
                    query = parse_qs(urlsplit(route.request.url).query)
                    controls["source_requests"].append(query)
                    if controls["fail_sources"]:
                        route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "合成列表暂时不可读"}))
                        return
                    if not args.exe:
                        route.continue_()
                        return
                    kind, group = query.get("kind", ["works"])[0], query.get("group", ["all"])[0]
                    offset, limit = int(query.get("offset", [0])[0]), int(query.get("limit", [50])[0])
                    rows = []
                    for index in range(76 if kind == "works" else 2):
                        deleted = index == 0 if kind == "works" else controls["deleted_artist"] and index == 0
                        if kind == "artists" and controls["recovered_artist"] and index == 0:
                            continue
                        row = {"kind": "work" if kind == "works" else "artist", "id": index + 1001,
                            "pixiv_id": str(index + 10001), "availability": availability(deleted),
                            "local": {"state": "complete" if index == 0 else "incomplete" if index in (2, 4) else "unknown", "missing": []}}
                        if kind == "works":
                            row.update(title=f"隔离来源作品 {index + 1}", artist_id=artist_id, artist_name=first_name)
                            if index == 2:
                                row["local"]["missing"] = [{"role": "ugoira_zip", "label": "动图原包", "kind": "original"}, {"role": "ugoira_timing", "label": "动图播放时序", "kind": "original"}]
                            if index == 4:
                                row["local"]["missing"] = [{"role": "image", "page": 1, "label": "第 2 页原图", "kind": "original"}]
                        else:
                            row.update(id=artist_id if index == 0 else second_id,
                                name=first_name if index == 0 else "另一位合成画师", avatar=first_artist.get("avatar"), work_count=120, incomplete_work_count=0)
                        rows.append(row)
                    counts = {"works": {"deleted": 1, "unavailable": 75}, "artists": {"deleted": int(controls["deleted_artist"]), "unavailable": 2 - int(controls["deleted_artist"]) - int(controls["recovered_artist"])}}
                    rows = [row for row in rows if group == "all" or (row["availability"]["state"] == "deleted") == (group == "deleted")]
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": rows[offset:offset+limit], "total": len(rows),
                        "offset": offset, "limit": limit, "counts": counts, "catalogue_revision": revision}, ensure_ascii=False))

                def artist_route(route):
                    path = urlsplit(route.request.url).path
                    if path.endswith("/check-source"):
                        controls["homepage_submitted"].append(path)
                        route.continue_()
                    elif args.exe:
                        match = re.fullmatch(r"/api/artists/(\d+)", path)
                        response = replay_artist(int(match[1])) if match else {"items": [replay_artist(artist_id), replay_artist(second_id)], "total": 2, "offset": 0, "limit": 50}
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(response, ensure_ascii=False))
                    else:
                        route.continue_()

                def gallery_route(route):
                    query = parse_qs(urlsplit(route.request.url).query)
                    if (not args.exe and not controls["large_gallery"]) or "artist_ids" not in query:
                        route.continue_(); return
                    identity = int(query["artist_ids"][0])
                    offset, limit = int(query.get("offset", [0])[0]), int(query.get("limit", [50])[0])
                    count = 960 if controls["large_gallery"] else 120
                    rows = [{**deepcopy(base_work), "id": index + 20000, "pixiv_id": str(index + 30000), "artist_id": identity,
                             "artist_name": first_name if identity == artist_id else "另一位合成画师", "title": f"隔离长图库 {index + 1}"} for index in range(count)]
                    response = {**library, "items": rows[offset:offset+limit], "total": len(rows), "offset": offset, "ceiling": 30000, "catalogue_revision": revision}
                    if controls["large_gallery"]:
                        controls["window_requests"].append({"offset": offset, "limit": limit, "returned": len(response["items"])})
                        before = gallery_geometry()
                        if before and before["size"] and offset >= before["start"] + before["size"]:
                            columns = before["columns"]
                            trim = max(0, before["size"] + len(response["items"]) - 480)
                            trim = (trim + columns - 1) // columns * columns
                            controls["window_transitions"].append({"before": before,
                                "expected_start": before["start"] + trim, "expected_size": before["size"] + len(response["items"]) - trim,
                                "request": {"offset": offset, "limit": limit}})
                    route.fulfill(status=200, content_type="application/json", body=json.dumps(response, ensure_ascii=False))

                page.route("**/api/library/source-status?*", source_route)
                page.route(re.compile(r".*/api/artists(?:\?.*)?/(?:\d+)(?:/check-source)?$|.*/api/artists(?:\?.*)?$"), artist_route)
                page.route(re.compile(r".*/api/library\?.*$"), gallery_route)
                page.on("request", lambda request: result["submitted"].append(request.post_data_json) if request.method == "POST" and urlsplit(request.url).path == "/api/tasks" else None)

                def screenshot(label):
                    page.screenshot(path=str(artifacts / f"{label}-{width}.png"))
                    assert page.evaluate("document.documentElement.scrollWidth") <= width + 1

                def gallery_geometry():
                    return page.evaluate("""() => {
                      const viewport = document.querySelector('.gallery-viewport'), window = document.querySelector('.gallery-window');
                      if (!viewport || !window) return null;
                      const box = viewport.getBoundingClientRect();
                      const cards = [...window.querySelectorAll('.work-card')].map(card => {
                        const r = card.getBoundingClientRect(); return {id: Number(card.dataset.workId), top:r.top, bottom:r.bottom};
                      }).filter(card => card.bottom > box.top + 24 && card.top < box.bottom - 24);
                      const intro = document.querySelector('.gallery-artist-intro')?.getBoundingClientRect();
                      return {start:Number(window.dataset.start),size:Number(window.dataset.windowSize),scrollTop:viewport.scrollTop,
                        columns:window.querySelector('.gallery-row')?.children.length || 1,
                        viewportTop:box.top,viewportBottom:box.bottom,cards,introBottom:intro?.bottom};
                    }""")

                def scroll(locator, distance=650):
                    box = locator.bounding_box()
                    assert box
                    if width > 500:
                        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 260))
                        page.mouse.wheel(0, distance)
                    else:
                        cdp = context.new_cdp_session(page)
                        x = box["x"] + min(box["width"] / 2, 100)
                        start = min(height - 90, box["y"] + box["height"] - 70)
                        end = max(box["y"] + 90, start - min(distance, 430))
                        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": start}]})
                        for step in range(1, 9):
                            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": start + (end-start)*step/8}]})
                            page.wait_for_timeout(20)
                        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                        cdp.detach()
                    page.wait_for_timeout(350)

                def settings_sources():
                    page.goto(origin + "/settings", wait_until="domcontentloaded")
                    overview = page.locator(".source-overview")
                    expect(overview).to_contain_text("原作已不存在")
                    expect(page.get_by_role("button", name="检查并补齐图库", exact=True)).to_have_count(1)
                    for old in ("补下载", "全量核验", "刷新画师信息", "同步收藏状态"):
                        expect(page.locator(".settings-page").get_by_role("button", name=old, exact=True)).to_have_count(0)
                    overview.locator(".source-group-tabs").get_by_role("button", name=re.compile("已确认不存在")).click()
                    expect(overview.locator(".source-status-item")).to_have_count(1)
                    expect(overview).to_contain_text("本地存档完整")
                    screenshot("settings-confirmed-offline")
                    overview.locator(".source-group-tabs").get_by_role("button", name=re.compile("无法访问，待确认")).click()
                    expect(overview.locator(".source-status-item")).to_have_count(50)
                    expect(overview).to_contain_text("第 2 页原图")
                    expect(overview).to_contain_text("动图原包")
                    expect(overview).to_contain_text("动图播放时序")
                    before = overview.locator(".source-status-item").first.inner_text()
                    controls["fail_sources"] = True
                    page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
                    expect(overview.locator("[role=alert]")).to_contain_text("保留上次读取的清单")
                    assert overview.locator(".source-status-item").first.inner_text() == before
                    controls["fail_sources"] = False
                    overview.locator("[role=alert]").get_by_role("button", name="重试", exact=True).click()
                    expect(overview.locator("[role=alert]")).to_have_count(0)
                    overview.locator(".source-pagination").get_by_role("button", name="下一页", exact=True).click()
                    expect(overview.locator(".source-pagination")).to_contain_text("第 2 / 2 页")
                    expect(overview.locator(".source-status-item")).to_have_count(25)
                    overview.locator(".source-pagination").get_by_role("button", name="上一页", exact=True).click()
                    overview.locator("[aria-label=来源类型]").get_by_role("button", name="画师", exact=True).click()
                    expect(overview.locator(".source-status-item")).to_have_count(2)
                    overview.locator("[aria-label=来源类型]").get_by_role("button", name="作品", exact=True).click()
                    overview.evaluate("el=>el.scrollIntoView({block:'start'})")
                    screenshot("settings-pending-gaps")
                    passed(f"settings-sources-pagination-gaps-offline-recovery-{width}")

                def artist_checks():
                    page.goto(origin + f"/artist/{artist_id}", wait_until="domcontentloaded")
                    check = page.get_by_role("button", name="核对主站主页", exact=True)
                    avatar = page.locator(".artist-avatar")
                    expect(avatar).not_to_have_class(re.compile(r"\bsource-deleted\b"))
                    expect(check).to_be_enabled()
                    previous = len(controls["homepage_submitted"])
                    calls = len(remote.profile_calls) if runtime else 0
                    with page.expect_response(lambda response: urlsplit(response.url).path == f"/api/artists/{artist_id}/check-source" and response.request.method == "POST") as response:
                        check.click(click_count=2, delay=25)
                    expect(page.get_by_role("button", name="已排队核对", exact=True)).to_be_disabled()
                    page.wait_for_timeout(150)
                    assert controls["homepage_submitted"][previous:] == [f"/api/artists/{artist_id}/check-source"]
                    if runtime:
                        identity = response.value.json()["operation_id"]
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            operation = read(f"/api/tasks/{identity}?limit=1")
                            if operation["state"] == "finished":
                                break
                            runtime.queue.run_round()
                            page.wait_for_timeout(100)
                        assert operation["state"] == "finished"
                        assert remote.profile_calls[calls:] == [str(first_artist["pixiv_id"])]
                        tasks = runtime.queue.snapshot()["operations"]
                        assert any(task["kind"] == "artist_source" for task in tasks)
                    page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
                    expect(avatar).not_to_have_class(re.compile(r"\bsource-deleted\b"))
                    screenshot("artist-404-not-deleted")
                    if runtime:
                        version = runtime.source_status.begin("artist", artist_id, "90001")
                        runtime.source_status.apply_deleted("artist", artist_id,
                            DeletionEvidence(str(first_artist["pixiv_id"]), "reviewed synthetic explicit evidence", True, True), version, "90001")
                    else:
                        controls["deleted_artist"] = True
                    page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
                    expect(avatar).to_have_class(re.compile(r"\bsource-deleted\b"))
                    screenshot("artist-explicit-synthetic-deletion")
                    if runtime:
                        version = runtime.source_status.begin("artist", artist_id, "90001")
                        runtime.source_status.apply_available("artist", artist_id, {"user_id": str(first_artist["pixiv_id"])}, version, "90001")
                    else:
                        controls.update(deleted_artist=False, recovered_artist=True)
                    page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
                    expect(avatar).not_to_have_class(re.compile(r"\bsource-deleted\b"))
                    screenshot("artist-source-recovered")
                    link = page.get_by_role("link", name=f"在 Pixiv 查看 {first_name}", exact=True)
                    assert link.get_attribute("href") == f"https://www.pixiv.net/users/{first_artist['pixiv_id']}"
                    assert set(link.get_attribute("rel").split()) >= {"noopener", "noreferrer"}
                    passed(f"artist-target-only-dedup-404-evidence-red-and-recovery-{width}")

                def source_artist_link():
                    page.goto(origin + "/settings", wait_until="domcontentloaded")
                    overview = page.locator(".source-overview")
                    overview.locator("[aria-label=来源类型]").get_by_role("button", name="画师", exact=True).click()
                    link = overview.locator(f'.source-status-item[data-source-id="{artist_id}"] a')
                    expect(link).to_have_attribute("href", f"/artist/{artist_id}")
                    link.click()
                    page.wait_for_url(f"**/artist/{artist_id}*")
                    expect(page.locator(".artist-profile h2")).to_have_text(first_name)
                    page.get_by_role("button", name="查看全部作品", exact=False).click()
                    page.wait_for_url(re.compile(r".*/\?.*artist_ids=" + str(artist_id)))
                    expect(page.locator(".gallery-artist-intro")).to_have_attribute("data-artist-id", str(artist_id))
                    expect(page.locator(".gallery-artist-intro h2")).to_have_text(first_name)
                    expected = 120 if args.exe else first_artist["work_count"]
                    expect(page.locator(".gallery-heading")).to_contain_text(f"{expected} 件")
                    assert parse_qs(urlsplit(page.url).query)["artist_ids"] == [str(artist_id)]
                    screenshot("source-artist-link-all-local-works")
                    passed(f"source-artist-link-to-management-and-all-local-works-{width}", {"artist_id": artist_id, "total": expected, "url": page.url})

                def artist_preview_layout():
                    page.goto(origin + "/artists", wait_until="domcontentloaded")
                    expect(page.locator(".artists-list.grid")).to_be_visible()
                    image = page.locator(".artist-work-image").first
                    expect(image).to_be_visible()
                    grid = image.bounding_box()
                    grid_strip = page.locator(".artist-recent-strip").first.bounding_box()
                    count = page.locator(".artist-recent-strip").first.locator(".artist-work").count()
                    assert count == 12 and grid and grid_strip
                    screenshot("artists-grid-previews")
                    page.locator(".artist-layout-switch").get_by_role("button", name="列表", exact=True).click()
                    expect(page.locator(".artists-list.row")).to_be_visible()
                    row = image.bounding_box()
                    row_strip = page.locator(".artist-recent-strip").first.bounding_box()
                    assert row and row_strip and abs(row["width"]-grid["width"]) < 1 and abs(row["height"]-grid["height"]) < 1
                    if width > 500:
                        assert row_strip["width"] > grid_strip["width"] + 100
                    screenshot("artists-row-previews")
                    passed(f"artist-preview-sizes-and-twelve-work-window-{width}", {"grid": grid, "row": row, "grid_strip": grid_strip, "row_strip": row_strip})

                def scoped_gallery():
                    page.goto(origin + f"/?artist_ids={artist_id}", wait_until="domcontentloaded")
                    intro = page.locator(".gallery-artist-intro")
                    expect(intro).to_contain_text(first_name)
                    expect(intro).to_have_attribute("data-artist-id", str(artist_id))
                    assert intro.evaluate("el=>getComputedStyle(el).position") not in ("sticky", "fixed")
                    assert intro.locator(".artist-pixiv-link").get_attribute("href") == f"https://www.pixiv.net/users/{first_artist['pixiv_id']}"
                    screenshot("artist-gallery-intro")
                    viewport = page.locator(".gallery-viewport")
                    original = intro.bounding_box()
                    for _ in range(6):
                        scroll(viewport)
                        position = intro.bounding_box()
                        if position and position["y"] + position["height"] <= viewport.bounding_box()["y"]:
                            break
                    position, box = intro.bounding_box(), viewport.bounding_box()
                    assert original and position and box and position["y"] < original["y"] - 80
                    assert position["y"] + position["height"] <= box["y"] + 1, (position, box)
                    assert viewport.evaluate("el=>el.scrollTop") > 0
                    screenshot("artist-gallery-intro-scrolled-away")
                    page.goto(origin + f"/?artist_ids={second_id}", wait_until="domcontentloaded")
                    expect(intro).to_have_attribute("data-artist-id", str(second_id))
                    expect(intro).not_to_contain_text(first_name)
                    expect(intro.locator("h2")).not_to_be_empty()
                    page.goto(origin + "/", wait_until="domcontentloaded")
                    expect(page.locator(".work-card").first).to_be_visible()
                    assert not re.search("无法访问，待确认|已确认原作不存在|主站暂时无法获取", page.locator(".gallery-viewport").inner_text())
                    page.goto(origin + f"/illust/{reader_id}", wait_until="domcontentloaded")
                    expect(page.locator(".detail-head")).to_be_visible()
                    assert not re.search("无法访问，待确认|已确认原作不存在|主站暂时无法获取", page.locator(".reader-stage").inner_text())
                    screenshot("reader-without-source-warning")
                    passed(f"artist-gallery-intro-real-scroll-switch-links-and-local-reading-{width}")

                def large_artist_gallery():
                    controls["large_gallery"] = True
                    try:
                        page.goto(origin + f"/?artist_ids={artist_id}", wait_until="domcontentloaded")
                        expect(page.locator(".gallery-artist-intro h2")).to_have_text(first_name)
                        expect(page.locator(".gallery-heading")).to_contain_text("960 件")
                        expect(page.locator(".gallery-viewport")).to_have_attribute("aria-busy", "false")
                        assert page.locator(".gallery-artist-intro").evaluate("el=>getComputedStyle(el).position") not in ("sticky", "fixed")
                        geometry = gallery_geometry()
                        assert geometry and geometry["size"] <= 480 and geometry["start"] == 0
                        screenshot("large-artist-gallery-intro")
                        samples, tested = [geometry], 0
                        viewport = page.locator(".gallery-viewport")
                        box = viewport.bounding_box()
                        assert box
                        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        for step in range(90):
                            page.mouse.wheel(0, 5000)
                            page.wait_for_timeout(140)
                            expect(viewport).to_have_attribute("aria-busy", "false")
                            page.wait_for_timeout(40)
                            current = gallery_geometry()
                            assert current and current["size"] <= 480 and current["cards"]
                            visible_ids = [card["id"] for card in current["cards"]]
                            assert visible_ids == sorted(set(visible_ids)), visible_ids
                            previous_ids = [card["id"] for card in samples[-1]["cards"]]
                            if previous_ids:
                                assert visible_ids[0] >= previous_ids[0], (previous_ids, visible_ids)
                            assert current["introBottom"] <= current["viewportTop"] + 1, current
                            for transition in controls["window_transitions"][tested:]:
                                assert current["start"] == transition["expected_start"], (transition, current)
                                assert current["size"] == transition["expected_size"]
                                before_ids = [card["id"] for card in transition["before"]["cards"]]
                                assert before_ids and visible_ids[0] == before_ids[0], (transition, current)
                                assert abs(current["cards"][0]["top"] - transition["before"]["cards"][0]["top"]) < 3, (transition, current)
                                transition["after"] = current
                                transition["wheel_step"] = step
                                if current["start"] > 0:
                                    screenshot(f"large-artist-gallery-window-{current['start']}")
                            tested = len(controls["window_transitions"])
                            samples.append(current)
                            if current["start"] >= 400 and visible_ids[-1] >= 20945:
                                break
                        trims = [entry for entry in controls["window_transitions"] if entry["after"]["start"] > entry["before"]["start"]]
                        assert len(trims) >= 2, controls["window_transitions"]
                        assert samples[-1]["start"] >= 400 and samples[-1]["cards"][-1]["id"] >= 20945, samples[-1]
                        screenshot("large-artist-gallery-final-window")
                        passed(f"large-artist-gallery-cross-480-window-real-wheel-continuity-{width}",
                            {"scope": "960 metadata clones via browser GET response replay", "requests": controls["window_requests"],
                             "transitions": controls["window_transitions"], "samples": samples, "trim_count": len(trims)})
                    finally:
                        controls["large_gallery"] = False

                scenario(f"settings-{width}", settings_sources)
                scenario(f"source-artist-link-{width}", source_artist_link)
                scenario(f"artist-check-{width}", artist_checks)
                scenario(f"artist-layout-{width}", artist_preview_layout)
                scenario(f"gallery-{width}", scoped_gallery)
                scenario(f"large-gallery-{width}", large_artist_gallery)
                if width == 320:
                    def submit_unified():
                        page.goto(origin + "/settings", wait_until="domcontentloaded")
                        button = page.get_by_role("button", name="检查并补齐图库", exact=True)
                        previous = len(result["submitted"])
                        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/tasks" and response.request.method == "POST") as submitted:
                            button.click()
                        data = submitted.value.json()
                        assert submitted.value.status == 200 and data.get("operation_id")
                        assert result["submitted"][previous:] == [{"kind": "reconcile"}]
                        operation = read(f"/api/tasks/{data['operation_id']}?limit=1")
                        assert operation["kind"] == "reconcile"
                        screenshot("unified-task-submitted")
                        passed("real-synthetic-reconcile-submit-one-operation-no-old-task-chain", {"operation_id": data["operation_id"]})
                    scenario("unified-submit", submit_unified)
                page.unroute_all(behavior="wait")
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
        if runtime:
            runtime.close()
        result["elapsed_seconds"] = time.monotonic() - started
        (artifacts / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not result["failures"] and not result["errors"], json.dumps(result["failures"] + result["errors"], ensure_ascii=False)
    print(json.dumps({"passed": len(result["checks"]), "artifacts": str(artifacts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
