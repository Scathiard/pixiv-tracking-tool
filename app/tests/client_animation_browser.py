"""Real two-frame card playback and bounded media lifetime; synthetic localhost only."""
from __future__ import annotations

import argparse
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
    from PIL import Image
    from playwright.sync_api import expect, sync_playwright
    from client_fixture import make_library

    frames = [Image.new("RGB", (360, 360), color) for color in ("#e93d50", "#246ce9")]
    media = io.BytesIO()
    frames[0].save(media, format="GIF", save_all=True, append_images=frames[1:], duration=300, loop=0)
    gif = media.getvalue()
    (artifacts / "two-frame.gif").write_bytes(gif)
    with Image.open(io.BytesIO(gif)) as picture:
        assert picture.n_frames == 2
    runtime, remote = make_library(artifacts / "fixture", 120)
    from src.auth import _find_system_browser
    from src.web import create_app
    server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False,
        schedule=False, reading_ui=True), host="127.0.0.1", port=port,
        log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    checks, errors, metrics = [], [], {}
    revision, repaired, recovery_ready = [10], [False], [True]
    requests = Counter()
    page = None
    origin = f"http://127.0.0.1:{port}"

    def changing_frames(image, name):
        samples = []
        for index in range(7):
            image.page.wait_for_timeout(120)
            shot = image.screenshot(path=str(artifacts / f"{name}-frame-{index}.png"))
            with Image.open(io.BytesIO(shot)) as rendered:
                # Cropping the center excludes borders, badges and surrounding UI.
                center = rendered.crop((rendered.width // 4, rendered.height // 4,
                    rendered.width * 3 // 4, rendered.height * 3 // 4)).convert("RGB")
                samples.append(hashlib.sha256(center.tobytes()).hexdigest())
        metrics[name + "_unique_frames"] = len(set(samples))
        assert len(set(samples)) >= 2, f"{name}: visible card never changed frames: {samples}"

    def visible_media():
        return page.evaluate("""()=>{const root=document.querySelector('.gallery-viewport').getBoundingClientRect();
          const images=[...document.querySelectorAll('.card-picture img[data-media-source="animation"]')];
          return {animations:images.length,cards:document.querySelectorAll('.work-card').length,
            outside:images.filter(e=>{const r=e.getBoundingClientRect();return r.bottom<=root.top||r.top>=root.bottom}).length};}""")

    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            for width, height in ((1440, 900), (390, 844), (320, 720)):
                context = browser.new_context(viewport={"width": width, "height": height},
                    is_mobile=width < 500, has_touch=width < 500)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                repaired[0] = False
                recovery_ready[0] = True

                def library(route):
                    response = route.fetch()
                    data = response.json()
                    data["catalogue_revision"] = revision[0]
                    for work in data["items"]:
                        original_preview = work.get("preview")
                        work.update(type="ugoira", page_count=1, preview_status="ready",
                            preview=f"/animation-fixture/{work['id']}.gif")
                        work["recovery"] = {"status": "ready", "kind": "preview", "next_retry_at": None, "reason": None}
                        if work["id"] == 119 and not recovery_ready[0]:
                            work["recovery"]["status"] = "queued"
                        if work["id"] in (117, 118):
                            work.update(preview=None, preview_status="pending")
                        if work["id"] == 117:
                            work["thumbnail"] = None
                        if work["id"] in (114, 116):
                            work.update(type="illust", preview=original_preview)
                            if work["id"] == 114:
                                work["thumbnail"] = None
                    route.fulfill(response=response, body=json.dumps(data, ensure_ascii=False))

                def animation(route):
                    work = int(route.request.url.rsplit("/", 1)[1].split(".")[0])
                    requests[(width, work)] += 1
                    if work == 119 and not repaired[0]:
                        route.fulfill(status=404, body="synthetic missing GIF")
                    else:
                        route.fulfill(content_type="image/gif", body=gif, headers={"Cache-Control": "no-store"})

                page.route("**/api/library?*", library)
                page.route("**/animation-fixture/*.gif", animation)
                page.goto(origin)
                card = page.locator(".work-card[data-work-id='120']")
                expect(card.locator("img")).to_be_visible()
                changing_frames(card.locator("img"), f"{width}-initial")
                expect(card.locator("img")).to_have_attribute("data-media-source", "animation")
                checks.append(f"{width}-visible-gif-plays-over-existing-thumbnail")
                page.wait_for_timeout(250)
                initial = visible_media()
                assert 0 < initial["animations"] <= 20 and initial["outside"] == 0, initial
                assert initial["cards"] <= 45, initial
                assert sum(count for (screen, _), count in requests.items() if screen == width) <= 20
                metrics[str(width) + "_initial"] = initial

                # Scroll the initially failed card into view before checking fallback.
                bad = page.locator(".work-card[data-work-id='119']")
                bad.scroll_into_view_if_needed()
                expect(bad.locator("img")).to_have_attribute("data-media-source", "thumbnail")
                assert requests[(width, 119)] == 1, requests
                page.wait_for_timeout(1200)
                assert requests[(width, 119)] == 1, requests
                page.screenshot(path=str(artifacts / f"{width}-gif-fallback.png"))
                for _ in range(5):
                    revision[0] += 1
                    page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
                    page.wait_for_timeout(150)
                assert requests[(width, 119)] == 1, "unrelated metadata updates caused a GIF retry storm"
                checks.append(f"{width}-rapid-catalogue-updates-do-not-retry-failed-gif")
                if width == 1440:
                    page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'hidden'});document.dispatchEvent(new Event('visibilitychange'))")
                    expect(page.locator('.card-picture img[data-media-source="animation"]')).to_have_count(0)
                    before = sum(requests.values())
                    page.wait_for_timeout(12000)
                    assert sum(requests.values()) == before, "pending failed-media retry fired in background"
                    checks.append("1440-pending-retry-cancelled-while-background-past-cooldown")
                repaired[0] = True
                revision[0] += 1
                page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
                if width == 1440:
                    page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'visible'});document.dispatchEvent(new Event('visibilitychange'))")
                expect(bad.locator("img")).to_have_attribute("data-media-source", "animation", timeout=20000)
                changing_frames(bad.locator("img"), f"{width}-same-url-repaired")
                assert requests[(width, 119)] == 2, requests
                checks.append(f"{width}-failed-gif-stays-static-with-one-request-and-recovers-same-url")
                if width == 1440:
                    repaired[0] = recovery_ready[0] = False
                    page.reload()
                    expect(bad.locator("img")).to_have_attribute("data-media-source", "thumbnail")
                    assert requests[(width, 119)] == 3
                    repaired[0] = recovery_ready[0] = True
                    ready_at = time.monotonic()
                    page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
                    expect(bad.locator("img")).to_have_attribute("data-media-source", "animation")
                    assert time.monotonic() - ready_at < 3, "explicit recovery ready unnecessarily waited for cooldown"
                    changing_frames(bad.locator("img"), "1440-same-revision-ready")
                    assert requests[(width, 119)] == 4
                    checks.append("1440-same-revision-recovery-ready-immediately-restores-same-url")

                static_card = page.locator(".work-card[data-work-id='116']")
                static_card.scroll_into_view_if_needed()
                expect(static_card.locator("img")).to_have_attribute("data-media-source", "thumbnail")
                assert static_card.locator("img").evaluate("e=>e.complete && e.naturalWidth > 0")
                assert requests[(width, 116)] == 0
                checks.append(f"{width}-static-card-still-prefers-readable-thumbnail")
                original_card = page.locator(".work-card[data-work-id='114']")
                original_card.scroll_into_view_if_needed()
                expect(original_card.locator("img")).to_have_attribute("data-media-source", "original")
                expect(original_card.locator("img")).to_be_visible()
                page.wait_for_function("()=>{const e=document.querySelector('.work-card[data-work-id=\"114\"] img');return e && e.complete && e.naturalWidth > 0}")
                checks.append(f"{width}-static-card-without-thumbnail-loads-visible-original")

                page.locator(".work-card[data-work-id='118']").scroll_into_view_if_needed()
                expect(page.locator(".work-card[data-work-id='118'] img")).to_have_attribute("data-media-source", "thumbnail")
                page.locator(".work-card[data-work-id='117']").scroll_into_view_if_needed()
                expect(page.locator(".work-card[data-work-id='117'] .media-placeholder")).to_be_visible()
                checks.append(f"{width}-missing-gif-keeps-thumbnail-or-missing-preview-message")

                pane = page.locator(".gallery-viewport")
                pane.evaluate("e=>e.scrollTop=0")
                expect(card.locator('img[data-media-source="animation"]')).to_be_visible()
                page.evaluate("window.__oldAnimation=document.querySelector('.work-card[data-work-id=\"120\"] img')")
                pane.evaluate("e=>e.scrollTop=1800")
                page.wait_for_timeout(300)
                assert page.evaluate("!window.__oldAnimation.hasAttribute('src') && !window.__oldAnimation.isConnected")
                scrolled = visible_media()
                assert scrolled["outside"] == 0 and scrolled["animations"] <= 20 and scrolled["cards"] <= 45, scrolled
                metrics[str(width) + "_scrolled"] = scrolled
                pane.evaluate("e=>e.scrollTop=0")
                expect(card.locator('img[data-media-source="animation"]')).to_be_visible()
                changing_frames(card.locator("img"), f"{width}-returned")
                checks.append(f"{width}-offscreen-media-released-and-return-restores-playback")

                page.evaluate("window.__oldAnimation=document.querySelector('.work-card[data-work-id=\"120\"] img')")
                # Deterministic browser visibility lifecycle; headless tabs do not report
                # hidden consistently merely from bring_to_front().
                page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'hidden'});document.dispatchEvent(new Event('visibilitychange'))")
                expect(page.locator('.card-picture img[data-media-source="animation"]')).to_have_count(0)
                assert page.evaluate("!window.__oldAnimation.hasAttribute('src') && !window.__oldAnimation.isConnected")
                before = sum(requests.values())
                page.wait_for_timeout(700)
                assert sum(requests.values()) == before
                page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,get:()=> 'visible'});document.dispatchEvent(new Event('visibilitychange'))")
                expect(card.locator('img[data-media-source="animation"]')).to_be_visible()
                changing_frames(card.locator("img"), f"{width}-foreground")
                checks.append(f"{width}-background-releases-animation-without-media-requests-and-resumes")
                for offset in (1800, 3600, 5400, 7200, 9000):
                    before_requests = sum(requests.values())
                    pane.evaluate("(e,top)=>e.scrollTop=top", offset)
                    page.wait_for_timeout(180)
                    bounded = visible_media()
                    assert bounded["outside"] == 0 and bounded["animations"] <= 20 and bounded["cards"] <= 45, bounded
                    assert sum(requests.values()) - before_requests <= 20, "one viewport jump loaded too much animation media"
                checks.append(f"{width}-long-scroll-dom-and-animation-count-bounded")
                assert remote.write_count == 0
                context.close()
            browser.close()
        assert not errors, errors
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
        metrics["gif_requests"] = {f"{width}:{work}": count for (width, work), count in requests.items()}
        (artifacts / "result.json").write_text(json.dumps({"checks": checks, "errors": errors,
            "metrics": metrics, "real_library_used": False, "real_account_used": False,
            "real_phone_tested": False, "visibility_test": "synthetic visibilitychange in actual Chromium"},
            ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": checks, "metrics": metrics}, ensure_ascii=False))


if __name__ == "__main__":
    main()
