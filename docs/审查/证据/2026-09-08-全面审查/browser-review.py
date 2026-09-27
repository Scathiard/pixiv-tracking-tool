"""Extended Chromium audit using the repository's isolated browser fixture.

Only localhost is allowed. Bookmark requests are intercepted; no account writes occur.
Application source and persistent configuration are never changed.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

EVIDENCE = Path(__file__).resolve().parent
REPO = EVIDENCE.parents[3]
sys.path[:0] = [str(REPO / "app"), str(REPO / "app/tests")]
FIXTURE = Path(tempfile.mkdtemp(prefix="pixiv-review-browser-"))
os.environ["DATA_ROOT"] = str(FIXTURE)
os.environ["PIXIV_REFRESH_TOKEN"] = ""
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    PORT = sock.getsockname()[1]
os.environ["PORT"] = str(PORT)

import uvicorn
from playwright.sync_api import sync_playwright
from browser_fixture import build_app
from src import config, models, networking, progress
from src.auth import _find_system_browser

APP = build_app()
# Use documentation-only example addresses in screenshots, never host interfaces.
networking.active_ip_addresses = lambda: ["192.0.2.10", "2001:db8:1234:5678:1234:5678:90ab:cdef"]
config.PAGE_SIZE = 50
config.MAX_VISIBLE_ITEMS = 0
config.PAGINATION_MODE = "scroll"
with models.Session() as session:
    now = datetime.utcnow()
    for i in range(100):
        item = models.Illustration(pixiv_illust_id=str(90000+i), artist_id=1,
            title=f"Synthetic archive {i}", type="illust", page_count=1,
            posted_at=now-timedelta(days=i % 12), file_paths='["/static/placeholder.svg"]', tags='["fixture"]')
        session.add(item)
        session.flush()
        session.add(models.IllustrationTag(illustration_id=item.id, tag="fixture"))
    session.commit()

SERVER = uvicorn.Server(uvicorn.Config(APP, host="127.0.0.1", port=PORT, log_level="error"))
THREAD = threading.Thread(target=SERVER.run)
THREAD.start()
deadline = time.monotonic() + 10
while not SERVER.started and time.monotonic() < deadline:
    time.sleep(.05)
if not SERVER.started:
    raise RuntimeError("Fixture server failed to start")

RESULTS = []
BASE = f"http://localhost:{PORT}"


def record(name, callback):
    try:
        if "--layout-only" in sys.argv and name != "mobile_and_keyboard_inspection":
            return
        RESULTS.append({"case": name, "completed": True, "observation": callback()})
    except Exception as exc:
        RESULTS.append({"case": name, "completed": False, "error": f"{type(exc).__name__}: {exc}"})


def main(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    def local_only(route):
        if route.request.url.startswith(BASE + "/"):
            route.continue_()
        else:
            route.abort()
    context.route("**/*", local_only)
    page = context.new_page()
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.goto(BASE, wait_until="networkidle")
    # Inspect rendered DOM before choosing interaction targets.
    (EVIDENCE / "rendered-controls.json").write_text(json.dumps(page.evaluate("""() =>
        [...document.querySelectorAll('button,input,select,a')].map(e => ({tag:e.tagName,
        id:e.id, text:(e.textContent||'').trim().slice(0,80), href:e.getAttribute('href'),
        type:e.getAttribute('type'), role:e.getAttribute('role')}))"""), ensure_ascii=False, indent=2), encoding="utf-8")
    page.screenshot(path=str(EVIDENCE / "desktop-gallery.png"))

    def artist_scroll():
        page.goto(BASE + "/artist/1", wait_until="networkidle")
        before = page.locator(".illust-card").count()
        sentinel = page.locator("#scroll-sentinel").count()
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)
        after = page.locator(".illust-card").count()
        header = page.locator(".artist-header p").inner_text()
        assert before == after == 50 and sentinel == 0
        return {"expected_works":112, "before":before, "after_bottom_scroll":after,
                "scroll_sentinel_count":sentinel, "header":header}
    record("artist_initial_scroll_not_initialized", artist_scroll)

    def duplicate_bookmark():
        page.goto(BASE, wait_until="networkidle")
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_function("document.querySelectorAll('.illust-card').length >= 100")
        calls = []
        def fake_bookmark(route):
            calls.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body='{"ok":true,"is_bookmarked":true}')
        page.route("**/bookmark", fake_bookmark)
        button = page.locator(".card-bookmark-btn").first
        previous = button.get_attribute("class")
        button.click()
        page.wait_for_timeout(300)
        current = button.get_attribute("class")
        page.unroute("**/bookmark", fake_bookmark)
        assert len(calls) == 2
        return {"single_click_requests":len(calls), "visual_class_before":previous, "visual_class_after":current}
    record("bookmark_bound_twice_after_scroll", duplicate_bookmark)

    def pagination_after_filter():
        config.PAGINATION_MODE = "paginate"
        page.goto(BASE, wait_until="networkidle")
        before = page.locator(".pagination").is_visible()
        page.locator("#filter-toggle").click()
        page.locator('[data-section="display"]').click()
        page.locator("#section-display a").click()
        page.wait_for_url("**show_hidden=1")
        after = page.locator(".pagination").is_visible()
        cards = page.locator(".illust-card").count()
        page.locator("#filter-close").click()
        page.screenshot(path=str(EVIDENCE / "pagination-after-filter.png"), full_page=True)
        assert before and not after and cards == 50
        return {"matching_works":124, "visible_works":cards, "pagination_before":before, "pagination_after":after}
    record("pagination_hidden_after_ajax_filter", pagination_after_filter)
    config.PAGINATION_MODE = "scroll"

    def empty_recovery():
        page.goto(BASE + "/?artist_ids=", wait_until="networkidle")
        spinner = page.locator("#load-spinner").count()
        page.locator("#filter-toggle").click()
        page.locator('[data-section="artist"]').click()
        page.locator("#section-artist a").filter(has_text="全部").click()
        page.wait_for_function("document.querySelector('.toast') !== null")
        outcome = {"initial_spinner_count":spinner, "cards_after_selecting_all":page.locator(".illust-card").count(),
                   "toast":page.locator(".toast").inner_text(), "url_query":page.evaluate("location.search")}
        assert outcome["cards_after_selecting_all"] == 0
        return outcome
    record("cannot_recover_from_initial_empty_filter", empty_recovery)

    def expand_short_result():
        page.goto(BASE + "/?types=manga", wait_until="networkidle")
        assert page.locator(".illust-card").count() == 6
        page.locator("#filter-toggle").click()
        page.locator('[data-section="type"]').click()
        page.locator("#section-type a").filter(has_text="全部").click()
        page.wait_for_function("document.querySelectorAll('.illust-card').length === 50")
        page.locator("#filter-close").click()
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)
        after = page.locator(".illust-card").count()
        assert after == 50
        return {"initial_count":6, "expanded_expected_count":124, "expanded_after_bottom_scroll":after}
    record("has_more_stale_after_filter_expansion", expand_short_result)

    def custom_date():
        page.goto(BASE, wait_until="networkidle")
        page.locator("#filter-toggle").click()
        page.locator('[data-section="period"]').click()
        page.locator("#section-period a").filter(has_text="指定期间").click()
        page.wait_for_url("**period=custom")
        visible = page.locator("#custom-date-range").is_visible()
        assert not visible
        return {"selected_period":"custom", "date_inputs_visible":visible}
    record("custom_date_inputs_remain_hidden", custom_date)

    def period_erases_artist():
        page.goto(BASE, wait_until="networkidle")
        page.locator("#filter-toggle").click()
        page.locator('[data-section="artist"]').click()
        page.locator('[data-artist-id="2"]').click()
        page.wait_for_url("**artist_ids=1")
        page.locator('[data-section="period"]').click()
        page.locator("#section-period a").filter(has_text="一周内").click()
        page.wait_for_url("**period=week")
        query = page.evaluate("location.search")
        assert "artist_ids" not in query
        return {"artist_before":1, "query_after_changing_period":query}
    record("changing_period_drops_artist_filter", period_erases_artist)

    def mobile_layout():
        page.set_viewport_size({"width":390, "height":844})
        outcomes = {}
        for label, url in (("gallery", "/"), ("artists", "/artists"), ("settings", "/settings"), ("detail", "/illust/1")):
            page.goto(BASE + url, wait_until="networkidle")
            outcomes[label] = page.evaluate("({viewport:innerWidth, scroll:document.documentElement.scrollWidth})")
            page.screenshot(path=str(EVIDENCE / f"mobile-{label}.png"), full_page=(label in {"artists", "settings"}))
        page.goto(BASE + "/?period=custom", wait_until="networkidle")
        page.locator("#filter-toggle").click()
        outcomes["date_panel"] = page.locator(".filter-panel-box").evaluate("e => ({width:e.clientWidth, scroll:e.scrollWidth})")
        page.screenshot(path=str(EVIDENCE / "mobile-filter.png"))
        page.keyboard.press("Escape")
        outcomes["escape_closes_filter"] = not page.locator("#filter-panel").is_visible()
        outcomes["filter_dialog_role"] = page.locator("#filter-panel").get_attribute("role")
        return outcomes
    record("mobile_and_keyboard_inspection", mobile_layout)
    context.close()
    return page_errors


try:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
        errors = main(browser)
        browser.close()
finally:
    SERVER.should_exit = True
    THREAD.join(timeout=10)
    progress.shutdown()
    models.engine.dispose()

output = {"fixture":str(FIXTURE), "total_synthetic_works":124, "real_pixiv_requests":0,
          "page_errors":errors, "results":RESULTS}
output_name = "browser-layout-results.json" if "--layout-only" in sys.argv else "browser-results.json"
(EVIDENCE / output_name).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(output, ensure_ascii=False, indent=2))
sys.exit(0 if all(item["completed"] for item in RESULTS) else 1)
