"""Cached hearts, bounded original fallback and thumbnail failure on synthetic media."""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "fixture")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    os.environ["PORT"] = str(port)
    import uvicorn
    from playwright.sync_api import expect, sync_playwright
    from sqlalchemy import update
    from client_fixture import make_library
    from src.core.models import Asset, Derivative, RemoteBookmark, utcnow

    runtime, remote = make_library(artifacts / "fixture", 601)
    with runtime.database.write() as session:
        session.execute(update(Derivative).values(state="missing", relative_path=None))
        session.execute(update(Asset).where(Asset.work_id == 599).values(
            state="failed", relative_path=None, error="synthetic download failure"))
        session.execute(update(RemoteBookmark).values(value=True, source="readback", account_id="90001",
            confirmed_at=utcnow() - timedelta(minutes=2), version=1))
    remote.values = {str(100000 + work): True for work in range(1, 602)}
    remote_reads = []
    original_read = remote.read_bookmark
    def read_bookmark(remote_id):
        remote_reads.append(remote_id)
        return original_read(remote_id)
    remote.read_bookmark = read_bookmark
    from src.auth import _find_system_browser
    from src.web import create_app
    server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False,
        schedule=False, reading_ui=True), host="127.0.0.1", port=port,
        log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    checks, errors, metrics, thumbnail_posts = [], [], {}, []
    reject_library = [False]
    origin = f"http://127.0.0.1:{port}"
    page = browser = None
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            context = browser.new_context(viewport={"width":1440,"height":900})
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            def tasks(route):
                body = route.request.post_data_json or {}
                if body.get("kind") == "thumbnails":
                    thumbnail_posts.append(body)
                route.fulfill(status=503, content_type="application/json",
                    body=json.dumps({"detail":"synthetic task service unavailable"}))
            def library(route):
                if reject_library[0]:
                    route.fulfill(status=503, content_type="application/json", body='{"detail":"synthetic refresh failed"}')
                else:
                    route.continue_()
            page.route("**/api/tasks", tasks)
            page.route("**/api/library?*", library)
            page.goto(origin)
            page.locator(".work-card[data-work-id='601']").wait_for()
            expect(page.locator(".work-card[data-work-id='601'] .bookmark-button")).to_have_class(
                re.compile(r"\bon\b"))
            page.wait_for_timeout(500)
            assert not remote_reads, "gallery cache must not require remote readback"
            checks.append("cached-confirmed-heart-renders-without-remote-request")
            expect(page.locator(".work-card[data-work-id='599'] .media-placeholder")).to_contain_text(
                re.compile("原图|已排队补齐|等待自动重试"))
            assert not thumbnail_posts, "gallery must not submit thumbnail tasks while opening"
            assert page.locator(".gallery-view>.notice.error").count() == 0
            original_metrics = page.evaluate("""()=>{const area=document.querySelector('.gallery-viewport').getBoundingClientRect();
              const images=[...document.querySelectorAll('.card-picture img[data-media-source="original"]')];
              return {count:images.length,outside:images.filter(e=>{const b=e.getBoundingClientRect();
                return b.bottom<=area.top || b.top>=area.bottom}).length};}""")
            metrics["initial_originals"] = original_metrics
            assert 0 < original_metrics["count"] <= 20 and original_metrics["outside"] == 0, original_metrics
            page.screenshot(path=str(artifacts / "gallery-cached-hearts-and-fallback.png"))
            checks.append("original-fallback-visible-only-and-missing-original-card-retained")

            with runtime.database.write() as session:
                session.execute(update(RemoteBookmark).where(RemoteBookmark.work_id == 601).values(value=False, version=2))
            page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
            expect(page.locator(".work-card[data-work-id='601'] .bookmark-button")).to_have_attribute("aria-pressed", "false")
            checks.append("existing-card-observes-updated-confirmed-cache-without-remount")
            ids = page.locator(".work-card").evaluate_all("elements=>elements.map(e=>e.dataset.workId)")
            reject_library[0] = True
            page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
            expect(page.locator(".toast")).to_contain_text("已载入的作品仍可浏览")
            assert page.locator(".work-card").evaluate_all("elements=>elements.map(e=>e.dataset.workId)") == ids
            reject_library[0] = False
            checks.append("failed-background-refresh-retains-visible-cards")

            with runtime.database.write() as session:
                session.execute(update(Derivative).where(Derivative.work_id == 601).values(
                    state="verified", relative_path="100601-thumb.webp"))
            expect(page.locator(".work-card[data-work-id='601'] img")).to_have_attribute(
                "data-media-source", "thumbnail", timeout=20000)
            checks.append("automatic-visible-refresh-replaces-original-fallback")

            page.mouse.move(1100, 550)
            windows, image_counts = [], []
            for _ in range(24):
                page.mouse.wheel(0, 4800)
                page.wait_for_timeout(200)
                windows.append(int(page.locator(".gallery-window").get_attribute("data-window-size")))
                image_counts.append(page.locator(".card-picture img").count())
                if page.locator(".work-card[data-work-id='1']").count():
                    break
            assert page.locator(".work-card[data-work-id='1']").count(), "missing thumbnails blocked continuous library"
            assert max(windows) <= 480 and max(image_counts) <= 40
            assert not page.locator(".gallery-view>.notice.error").count()
            assert not thumbnail_posts, "scrolling must not submit thumbnail tasks"
            metrics.update(max_window=max(windows), max_images=max(image_counts), thumbnail_submissions=len(thumbnail_posts))
            checks.append("continuous-browse-past-240-through-601-without-thumbnail-posts")

            feed = browser.new_context(viewport={"width":390,"height":844},is_mobile=True,has_touch=True)
            flow = feed.new_page()
            flow.on("pageerror", lambda error: errors.append(str(error)))
            flow.goto(origin + "/flow")
            flow.locator(".reading-toolbar .bookmark-button").wait_for()
            for _ in range(3):
                flow.get_by_role("button", name="跳过作品", exact=False).tap()
                expect(flow.locator(".reading-toolbar .bookmark-button")).to_have_attribute("aria-pressed", "true")
                flow.wait_for_timeout(200)
            flow.wait_for_timeout(2700)
            assert not remote_reads, remote_reads
            flow.screenshot(path=str(artifacts / "feed-local-heart.png"))
            checks.append("cold-feed-has-batched-local-hearts-and-no-redundant-remote-read")
            feed.close()
            assert remote.write_count == 0 and not errors
            context.close()
            browser.close()
            browser = None
    except Exception:
        errors.append(traceback.format_exc())
        if page is not None:
            try:
                page.screenshot(path=str(artifacts / "failure.png"))
            except Exception:
                pass
        raise
    finally:
        server.should_exit = True
        thread.join(20)
        (artifacts / "result.json").write_text(json.dumps({"checks":checks,"errors":errors,"metrics":metrics,
            "remote_reads":len(remote_reads),"real_account_used":False,"real_library_used":False,
            "real_phone_tested":False},ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"checks":checks,"metrics":metrics},ensure_ascii=False))


if __name__ == "__main__":
    main()
