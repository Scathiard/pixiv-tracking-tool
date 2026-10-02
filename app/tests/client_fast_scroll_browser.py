"""Fast wheel, plain-text 503 and unchanged-URL media recovery; localhost fixtures only."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit


class ProbeComplete(Exception):
    """Finish the isolated natural-load probe without running protocol replays."""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--media-delay-ms", type=int, choices=(0, 500, 2000), default=0)
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--recovery-suite", action="store_true")
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    workspace = Path(__file__).resolve().parents[2]
    repository = workspace.parent.parent if workspace.parent.name == ".worktrees" else workspace
    test_root = repository / ".devtools" / "test-runs"
    if not artifacts.is_relative_to(test_root.resolve()) or not artifacts.name.startswith("b10-fast-scroll-"):
        parser.error("artifacts must be a new main-repository .devtools/test-runs/b10-fast-scroll-* directory")
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "fixture")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    os.environ["PORT"] = str(port)
    import uvicorn
    from PIL import Image
    from playwright.sync_api import sync_playwright
    from client_fixture import make_library

    runtime, remote = make_library(artifacts / "fixture", 800)
    from src.auth import _find_system_browser
    from src.web import create_app
    frames = [Image.new("RGB", (360, 270), color) for color in ("#e93d50", "#246ce9")]
    animation_buffer = io.BytesIO()
    frames[0].save(animation_buffer, format="WEBP", save_all=True,
        append_images=frames[1:], duration=300, loop=0, lossless=True)
    animation = animation_buffer.getvalue()
    (artifacts / "two-frame.webp").write_bytes(animation)
    with Image.open(io.BytesIO(animation)) as decoded:
        assert decoded.n_frames == 2
    static_buffer = io.BytesIO()
    Image.new("RGB", (360, 360), "#39ba83").save(static_buffer, format="WEBP")
    static_media = static_buffer.getvalue()
    application = create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True)
    delay_active = [True]
    async def local_media_delay(scope, receive, send):
        if delay_active[0] and scope["type"] == "http" and scope["path"].startswith("/images/"):
            await asyncio.sleep(args.media_delay_ms / 1000)
        await application(scope, receive, send)
    server = uvicorn.Server(uvicorn.Config(local_media_delay, host="127.0.0.1", port=port,
        log_level="error", loop="src.networking:http_loop_factory", limit_concurrency=128))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()
    origin = f"http://127.0.0.1:{port}"
    result = {"checks": [], "failures": [], "errors": [], "metrics": {},
        "real_library_used": False, "real_account_used": False, "real_phone_tested": False,
        "service_overload_reproduced": False, "uvicorn_limit_concurrency": 128,
        "fault_origin": "browser protocol replay, not reduced server concurrency"}
    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    index = (dist / "index.html").read_text(encoding="utf-8")
    import re
    assets = [dist / path.removeprefix("/reader/") for path in re.findall(r'(?:src|href)="(/reader/[^"]+)"', index)]
    result["frontend_sha256"] = {str(file.relative_to(dist)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in [dist / "index.html", *assets]}
    result["controlled_media_delay_ms"] = args.media_delay_ms
    page = browser = None
    sample_stop = threading.Event()
    samples = []
    sample_thread = None

    def state(current):
        return current.evaluate("""()=>{const root=document.querySelector('.gallery-viewport');
          const win=document.querySelector('.gallery-window');const area=root.getBoundingClientRect();
          const cards=[...document.querySelectorAll('.work-card')].filter(e=>{const r=e.getBoundingClientRect();
            return r.bottom>area.top && r.top<area.bottom});return {offset:Number(win.dataset.start),
            window_size:Number(win.dataset.windowSize),scroll_top:root.scrollTop,
            scroll_height:root.scrollHeight,visible_ids:cards.map(e=>Number(e.dataset.workId)),
            readable_images:cards.filter(e=>{const i=e.querySelector('img');return i&&i.complete&&i.naturalWidth>0}).length,
            error:document.querySelector('.gallery-view>.notice.error')?.textContent||''};}""")

    def burst(current, steps=12):
        size = current.viewport_size
        current.mouse.move(size["width"] - 40, size["height"] * .65)
        for _ in range(steps):
            current.mouse.wheel(0, 100000)
            current.wait_for_timeout(20)

    def loaded_card(current, work):
        return current.locator(f".work-card[data-work-id='{work}'] img").evaluate_all(
            "items=>items.some(e=>e.complete&&e.naturalWidth>0)")

    def record_check(name, passed, details=None):
        result["checks" if passed else "failures"].append({"name": name, "details": details})

    def monitor(current, scenario):
        records = []
        counts = Counter()
        result["metrics"][scenario + "_request_counts"] = counts
        def request_seen(request):
            path = urlsplit(request.url).path
            key = "library" if path == "/api/library" else "thumbnail" if path.endswith("-thumb.webp") else "media" if request.resource_type == "image" else "other"
            counts[key] += 1
        current.on("request", request_seen)
        def response_seen(response):
            path = urlsplit(response.url).path
            if path == "/api/library" or response.status >= 500:
                query = parse_qs(urlsplit(response.url).query)
                records.append({"path": path, "offset": int(query.get("offset", [0])[0]),
                    "limit": int(query.get("limit", [0])[0]), "status": response.status,
                    "content_type": response.headers.get("content-type"), "at": time.monotonic()})
        current.on("response", response_seen)
        current.on("pageerror", lambda error: result["errors"].append({"scenario": scenario, "message": str(error)}))
        return records

    def recovery_suite(browser):
        for width, height in ((1440, 900), (390, 844), (320, 720)):
            label = str(width)
            context = browser.new_context(viewport={"width": width, "height": height},
                is_mobile=width < 500, has_touch=width < 500)
            current = context.new_page()
            records = monitor(current, label + "-persistent-library")
            rejected = [True]
            failed_offset = [None]
            def persistent_library(route):
                offset = int(parse_qs(urlsplit(route.request.url).query).get("offset", [0])[0])
                if offset > 0 and rejected[0]:
                    if failed_offset[0] is None:
                        failed_offset[0] = offset
                    route.fulfill(status=503, content_type="text/plain", body="Service Unavailable")
                else:
                    route.continue_()
            current.route("**/api/library?*", persistent_library)
            current.goto(origin)
            current.locator(".work-card[data-work-id='800']").wait_for()
            burst(current, 4)
            current.locator(".gallery-view>.notice.error").wait_for(timeout=8000)
            failure = state(current)
            failed_records = [r for r in records if r["offset"] == failed_offset[0] and r["status"] == 503]
            current.wait_for_timeout(2500)
            stable_failures = [r for r in records if r["offset"] == failed_offset[0] and r["status"] == 503]
            record_check(label + "-persistent-503-stops-after-three-safe-get-attempts",
                len(failed_records) == len(stable_failures) == 3 and "503" in failure["error"]
                    and "Unexpected token" not in failure["error"], failure)
            current.evaluate("window.dispatchEvent(new Event('archive-updated'))")
            current.wait_for_timeout(400)
            refreshed_failure = state(current)
            record_check(label + "-background-refresh-keeps-failed-extension-and-retry-button",
                refreshed_failure["error"] == failure["error"]
                    and current.locator(".gallery-view>.notice.error button").count() == 1,
                refreshed_failure)
            current.screenshot(path=str(artifacts / f"{label}-persistent-list-error.png"))
            rejected[0] = False
            prior = len(records)
            current.locator(".gallery-view>.notice.error button").click()
            current.locator(".gallery-view>.notice.error").wait_for(state="hidden")
            current.wait_for_timeout(300)
            manual = state(current)
            manual_records = records[prior:]
            record_check(label + "-manual-extension-retries-original-offset-and-keeps-position",
                any(r["offset"] == failed_offset[0] and r["status"] == 200 for r in manual_records)
                    and manual["offset"] == failure["offset"]
                    and manual["window_size"] > failure["window_size"]
                    and abs(manual["scroll_top"] - failure["scroll_top"]) < 4
                    and manual["readable_images"] > 0,
                {"failure": failure, "manual": manual, "requests": manual_records})
            result["metrics"][label + "_persistent_library"] = {"failed_state": failure,
                "manual_state": manual, "requests": records, "failed_offset": failed_offset[0]}
            current.screenshot(path=str(artifacts / f"{label}-manual-extension-restored.png"))
            context.close()

            context = browser.new_context(viewport={"width": width, "height": height},
                is_mobile=width < 500, has_touch=width < 500)
            current = context.new_page()
            media_records = monitor(current, label + "-media-lifecycle")
            attempts = Counter()
            revision = [None]
            def fixture_metadata(route):
                response = route.fetch()
                data = response.json()
                if revision[0] is None:
                    revision[0] = data.get("catalogue_revision", 0)
                data["catalogue_revision"] = revision[0]
                for work in data["items"]:
                    if work["id"] == 800:
                        work.update(type="ugoira", page_count=1, preview_status="ready",
                            preview="/fast-scroll-suite/800-animation.webp")
                    elif work["id"] in (798, 799):
                        work.update(type="illust", page_count=1, preview_status="ready",
                            thumbnail=f"/fast-scroll-suite/{work['id']}-thumb.webp",
                            preview=f"/fast-scroll-suite/{work['id']}-original.webp")
                route.fulfill(response=response, body=json.dumps(data, ensure_ascii=False))
            def fixture_media(route):
                name = urlsplit(route.request.url).path.rsplit("/", 1)[1]
                attempts[name] += 1
                if name.startswith("798-") or name.startswith("799-") and attempts[name] == 1:
                    route.fulfill(status=503, content_type="text/plain", body="Service Unavailable")
                else:
                    route.fulfill(content_type="image/webp", body=animation if name == "800-animation.webp" else static_media,
                        headers={"Cache-Control": "no-store"})
            current.route("**/api/library?*", fixture_metadata)
            current.route("**/fast-scroll-suite/*.webp", fixture_media)
            current.goto(origin)
            current.locator(".work-card[data-work-id='800'] img[data-media-source='animation']").wait_for()
            deadline = time.monotonic() + 6
            while not loaded_card(current, 799) and time.monotonic() < deadline:
                current.wait_for_timeout(100)
            record_check(label + "-first-static-503-recovers-at-same-url-without-metadata-update",
                loaded_card(current, 799) and attempts["799-thumb.webp"] == 2,
                dict(attempts))
            hashes = []
            for index in range(5):
                current.wait_for_timeout(160)
                shot = current.locator(".work-card[data-work-id='800'] img").screenshot(
                    path=str(artifacts / f"{label}-lightweight-frame-{index}.png"))
                with Image.open(io.BytesIO(shot)) as rendered:
                    center = rendered.crop((rendered.width//4, rendered.height//4,
                        rendered.width*3//4, rendered.height*3//4)).convert("RGB")
                    hashes.append(hashlib.sha256(center.tobytes()).hexdigest())
            record_check(label + "-animation-remains-actually-playing", len(set(hashes)) >= 2,
                {"unique_frames": len(set(hashes))})
            current.wait_for_timeout(4500)
            exhausted = dict(attempts)
            current.wait_for_timeout(2500)
            record_check(label + "-permanent-static-media-has-three-attempt-budget-per-url",
                attempts["798-thumb.webp"] == 3 and attempts["798-original.webp"] == 3
                    and exhausted == dict(attempts) and not loaded_card(current, 798), dict(attempts))
            for _ in range(4):
                revision[0] += 1
                current.evaluate("window.dispatchEvent(new Event('archive-updated'))")
                current.wait_for_timeout(120)
            current.wait_for_timeout(3600)
            record_check(label + "-unrelated-catalogue-revisions-cannot-reset-static-retry-budget",
                attempts["798-thumb.webp"] == exhausted["798-thumb.webp"]
                    and attempts["798-original.webp"] == exhausted["798-original.webp"],
                {"before": exhausted, "after": dict(attempts), "catalogue_revision": revision[0]})
            current.screenshot(path=str(artifacts / f"{label}-static-recovered-and-permanent-failure.png"))
            lifecycle = {"budget": dict(attempts), "animation_unique_frames": len(set(hashes)),
                "metadata_changed_on_recovery": False, "url_changed_on_recovery": False}

            # A fresh mount owns a fresh pending retry; deterministic visibility events are explicitly recorded.
            current.reload()
            current.locator(".work-card[data-work-id='798']").wait_for()
            current.wait_for_timeout(150)
            current.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'hidden'});document.dispatchEvent(new Event('visibilitychange'))")
            current.wait_for_timeout(150)
            hidden_before = {key: attempts[key] for key in ("798-thumb.webp", "798-original.webp", "800-animation.webp")}
            current.wait_for_timeout(3600)
            hidden_after = {key: attempts[key] for key in hidden_before}
            record_check(label + "-background-cancels-pending-static-retries-and-animation-requests",
                hidden_before == hidden_after, {"before": hidden_before, "after": hidden_after})
            current.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'visible'});document.dispatchEvent(new Event('visibilitychange'))")
            current.wait_for_timeout(4000)
            lifecycle["background"] = {"before": hidden_before, "after": hidden_after,
                "foreground": dict(attempts), "visibility_test": "synthetic visibilitychange in actual Chromium"}

            current.reload()
            current.locator(".work-card[data-work-id='798']").wait_for()
            current.wait_for_timeout(150)
            size = current.viewport_size
            current.mouse.move(size["width"] - 40, size["height"] * .65)
            current.mouse.wheel(0, 2400)
            current.wait_for_timeout(200)
            offscreen_before = {key: attempts[key] for key in ("798-thumb.webp", "798-original.webp", "800-animation.webp")}
            current.wait_for_timeout(3600)
            offscreen_after = {key: attempts[key] for key in offscreen_before}
            offscreen_state = state(current)
            record_check(label + "-offscreen-cancels-pending-static-retries",
                798 not in offscreen_state["visible_ids"] and offscreen_before == offscreen_after,
                {"before": offscreen_before, "after": offscreen_after, "state": offscreen_state})
            lifecycle["offscreen"] = {"before": offscreen_before, "after": offscreen_after, "state": offscreen_state}
            lifecycle["requests"] = media_records
            result["metrics"][label + "_media_lifecycle"] = lifecycle
            context.close()

    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started, "isolated server did not start"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            natural_requests = monitor(page, "natural-fast-wheel")
            def sample_load():
                while not sample_stop.wait(.005):
                    samples.append({"connections": len(server.server_state.connections),
                        "tasks": len(server.server_state.tasks)})
            sample_thread = threading.Thread(target=sample_load, daemon=True)
            sample_thread.start()
            page.goto(origin)
            page.locator(".work-card[data-work-id='800']").wait_for()
            burst(page, 24)
            page.wait_for_timeout(max(500, args.media_delay_ms + 300))
            sample_stop.set()
            sample_thread.join(3)
            natural = {"state": state(page), "requests": natural_requests,
                "peak_connections": max((s["connections"] for s in samples), default=0),
                "peak_tasks": max((s["tasks"] for s in samples), default=0),
                "samples": samples, "503_count": sum(r["status"] == 503 for r in natural_requests)}
            result["service_overload_reproduced"] = bool(natural["503_count"])
            result["metrics"]["natural_fast_wheel"] = natural
            page.screenshot(path=str(artifacts / "natural-fast-wheel.png"))
            record_check("natural-fast-wheel-reaches-readable-final-card", 1 in natural["state"]["visible_ids"]
                and natural["state"]["readable_images"] > 0, natural["state"])
            context.close()
            delay_active[0] = False
            if args.probe_only:
                browser.close()
                browser = None
                raise ProbeComplete()

            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            library_requests = monitor(page, "plain-text-list-503")
            fault = {"injected": False, "offset": None}
            def fail_next_batch(route):
                offset = int(parse_qs(urlsplit(route.request.url).query).get("offset", [0])[0])
                if offset > 0 and not fault["injected"]:
                    fault.update(injected=True, offset=offset)
                    route.fulfill(status=503, content_type="text/plain; charset=utf-8", body="Service Unavailable")
                else:
                    route.continue_()
            page.route("**/api/library?*", fail_next_batch)
            page.goto(origin)
            page.locator(".work-card[data-work-id='800']").wait_for()
            initial_state = state(page)
            burst(page, 4)
            page.wait_for_timeout(400)
            failed_state = state(page)
            assert fault["injected"], "actual wheel did not issue an appended-library request"
            page.screenshot(path=str(artifacts / "list-503.png"))
            requests_at_failure = len(library_requests)
            # The browser remains at the list tail while the local service is already restored.
            for _ in range(30):
                page.mouse.wheel(0, 100000)
                page.wait_for_timeout(500)
            restored_state = state(page)
            auto_requests = library_requests[requests_at_failure:]
            result["metrics"]["plain_text_list_503"] = {"fault": fault,
                "failed_state": failed_state, "restored_service_state": restored_state,
                "requests_after_service_restored": auto_requests, "requests": library_requests,
                "exact_syntax_error": "Unexpected token 'S'" in failed_state["error"]
                    and '"Service Unavailable" is not valid JSON' in failed_state["error"]}
            record_check("plain-text-503-has-readable-status-feedback",
                not result["metrics"]["plain_text_list_503"]["exact_syntax_error"], failed_state["error"])
            record_check("restored-list-auto-retries-without-manual-button",
                any(r["offset"] == fault["offset"] and r["status"] == 200 for r in library_requests)
                    and not restored_state["error"]
                    and (restored_state["window_size"] > initial_state["window_size"]
                        or restored_state["offset"] > initial_state["offset"]), restored_state)
            if page.locator(".gallery-view>.notice.error button").count():
                page.locator(".gallery-view>.notice.error button").click()
                page.wait_for_timeout(500)
                manual_state = state(page)
                result["metrics"]["plain_text_list_503"]["manual_retry_state"] = manual_state
                record_check("manual-list-retry-restores-readable-cards", not manual_state["error"]
                    and manual_state["readable_images"] > 0, manual_state)
            burst(page, 24)
            page.wait_for_timeout(500)
            final_state = state(page)
            result["metrics"]["plain_text_list_503"]["final_state"] = final_state
            record_check("list-continues-to-final-work-after-recovery", 1 in final_state["visible_ids"]
                and final_state["readable_images"] > 0, final_state)
            page.screenshot(path=str(artifacts / "list-restored.png"))
            context.close()

            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            media_requests = monitor(page, "unchanged-static-media-503")
            media_fault = {"active": True}
            media_counts = Counter()
            def stable_metadata(route):
                response = route.fetch()
                data = response.json()
                for work in data["items"]:
                    if work["id"] == 800:
                        work.update(type="ugoira", page_count=1, preview_status="ready",
                            preview="/fast-scroll-fixture/animation.webp")
                    elif work["id"] == 799:
                        work.update(type="illust", page_count=1, preview_status="ready",
                            thumbnail="/fast-scroll-fixture/static-thumb.webp",
                            preview="/fast-scroll-fixture/static-original.webp")
                route.fulfill(response=response, body=json.dumps(data, ensure_ascii=False))
            def synthetic_media(route):
                name = urlsplit(route.request.url).path.rsplit("/", 1)[1]
                media_counts[name] += 1
                if name.startswith("static-") and (media_counts[name] == 1 if args.recovery_suite else media_fault["active"]):
                    route.fulfill(status=503, content_type="text/plain; charset=utf-8", body="Service Unavailable")
                else:
                    route.fulfill(content_type="image/webp", body=animation if name == "animation.webp" else static_media,
                        headers={"Cache-Control": "no-store"})
            page.route("**/api/library?*", stable_metadata)
            page.route("**/fast-scroll-fixture/*.webp", synthetic_media)
            page.goto(origin)
            page.locator(".work-card[data-work-id='800'] img[data-media-source='animation']").wait_for()
            page.wait_for_timeout(200 if args.recovery_suite else 600)
            static_initial = loaded_card(page, 799)
            animation_card = page.locator(".work-card[data-work-id='800'] img")
            rendered = []
            for index in range(7):
                page.wait_for_timeout(120)
                shot = animation_card.screenshot(path=str(artifacts / f"animation-frame-{index}.png"))
                with Image.open(io.BytesIO(shot)) as image:
                    center = image.crop((image.width//4, image.height//4, image.width*3//4, image.height*3//4)).convert("RGB")
                    rendered.append(hashlib.sha256(center.tobytes()).hexdigest())
            counts_before_restore = dict(media_counts)
            page.screenshot(path=str(artifacts / "static-503-animation-readable.png"))
            media_fault["active"] = False
            # Recovery changes only the HTTP response, not URL, revision, props or catalogue events.
            deadline = time.monotonic() + 18
            while time.monotonic() < deadline and not loaded_card(page, 799):
                page.wait_for_timeout(250)
            static_recovered = loaded_card(page, 799)
            static_dom = page.locator(".work-card[data-work-id='799']").inner_text()
            result["metrics"]["unchanged_static_media_503"] = {"initial_static_readable": static_initial,
                "static_recovered": static_recovered, "static_visible_text": static_dom,
                "counts_before_restore": counts_before_restore, "counts_after_restore": dict(media_counts),
                "animation_unique_frames": len(set(rendered)), "requests": media_requests,
                "metadata_changed_on_recovery": False, "url_changed_on_recovery": False}
            record_check("temporary-static-503-was-observed", not static_initial
                and counts_before_restore.get("static-thumb.webp", 0) > 0)
            record_check("lightweight-animation-actually-plays-while-static-fails", len(set(rendered)) >= 2,
                {"unique_frames": len(set(rendered))})
            record_check("same-url-static-media-recovers-without-metadata-update", static_recovered,
                {"before": counts_before_restore, "after": dict(media_counts), "visible_text": static_dom})
            page.screenshot(path=str(artifacts / "static-restored-service.png"))
            record_check("no-real-account-writes", remote.write_count == 0)
            context.close()
            if args.recovery_suite:
                recovery_suite(browser)
            browser.close()
            browser = None
    except ProbeComplete:
        pass
    except Exception:
        result["errors"].append({"scenario": "harness", "message": traceback.format_exc()})
        if page is not None:
            try:
                page.screenshot(path=str(artifacts / "failure.png"))
            except Exception:
                pass
    finally:
        sample_stop.set()
        if sample_thread is not None:
            sample_thread.join(3)
        server.should_exit = True
        server_thread.join(20)
        result["server_stopped"] = not server_thread.is_alive()
        result["passed"] = not result["failures"] and not result["errors"] and result["server_stopped"]
        (artifacts / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "checks": result["checks"], "failures": result["failures"],
        "errors": result["errors"], "artifacts": str(artifacts)}, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
