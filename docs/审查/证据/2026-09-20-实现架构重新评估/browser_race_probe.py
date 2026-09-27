"""Fresh Chromium interaction probe against existing templates and synthetic DB.

Only localhost is permitted. An earlier real fixture response is deliberately
held until a later response renders, to test latest-user-intent ordering.
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from architecture_probes import APP, EVIDENCE, isolate


async def main():
    root = isolate()
    sys.path.insert(0, str(APP / "tests"))
    import uvicorn
    from playwright.async_api import async_playwright
    from browser_fixture import build_app
    from src.auth import _find_system_browser
    from src import models, progress

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error"))
    server_thread = threading.Thread(target=server.run, daemon=False)
    server_thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        await asyncio.sleep(0.025)
    if not server.started:
        server.should_exit = True
        server_thread.join(timeout=10)
        raise RuntimeError("Fixture server startup failed")
    first_seen, release_first = asyncio.Event(), asyncio.Event()
    page_errors = []
    held_count = 0
    result = {}
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            async def route_handler(route):
                nonlocal held_count
                url = urlsplit(route.request.url)
                if url.hostname not in {"localhost", "127.0.0.1"} or url.port != port:
                    await route.abort()
                    return
                params = parse_qs(url.query)
                if params.get("fragment") == ["1"] and params.get("types") == ["manga,ugoira"]:
                    response = await route.fetch()
                    held_count += 1
                    first_seen.set()
                    await asyncio.wait_for(release_first.wait(), timeout=10)
                    await route.fulfill(response=response)
                else:
                    await route.continue_()
            await context.route("**/*", route_handler)
            page = await context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            await page.goto(f"http://localhost:{port}/", wait_until="networkidle")
            assert await page.locator(".illust-card").count() == 24
            await page.locator("#filter-toggle").click()
            await page.locator('[data-section="type"]').click()
            await page.locator('[data-type="illust"]').click()
            await asyncio.wait_for(first_seen.wait(), timeout=5)
            await page.locator('[data-type="manga"]').click()
            await page.wait_for_function("new URLSearchParams(location.search).get('types') === 'illust,ugoira'")
            await page.wait_for_function("document.querySelectorAll('.illust-card').length === 18")
            after_latest = {"query": urlsplit(page.url).query, "cards": await page.locator(".illust-card").count()}
            release_first.set()
            await page.wait_for_function("new URLSearchParams(location.search).get('types') === 'manga,ugoira'")
            await page.wait_for_function("document.querySelectorAll('.illust-card').length === 6")
            after_old = {"query": urlsplit(page.url).query, "cards": await page.locator(".illust-card").count()}
            await page.locator("#filter-close").click()
            await page.screenshot(path=str(EVIDENCE / "stale-filter-response.png"), full_page=False)
            result = {"temporary_root": str(root), "fixture_works": 24, "held_earlier_responses": held_count,
                      "after_latest_user_action": after_latest, "after_earlier_response_released": after_old,
                      "stale_response_overwrites_latest": after_latest != after_old, "page_errors": page_errors,
                      "scope": "Current homepage template in headless Chromium; isolated fixture; deterministic response reordering; no real Pixiv requests"}
            await context.close()
            await browser.close()
    finally:
        release_first.set()
        server.should_exit = True
        await asyncio.to_thread(server_thread.join, 10)
        if server_thread.is_alive():
            raise RuntimeError("Owned fixture server did not stop")
        models.engine.dispose()
        progress.shutdown(wait=True)
    (EVIDENCE / "browser-race.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
