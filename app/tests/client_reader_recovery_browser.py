"""Reader repairs appear in place from a bounded metadata refresh on synthetic data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "fixture")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    import uvicorn
    from playwright.sync_api import expect, sync_playwright
    from client_fixture import make_library

    runtime, _ = make_library(artifacts / "fixture", 80)
    from src.auth import _find_system_browser
    from src.web import create_app

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True),
        host="127.0.0.1", port=port, log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    checks, errors, metrics = [], [], {}
    ready = [False]
    same_url = [False]
    requests = {"detail": 0, "flow": 0}
    recovery = {"status": "queued", "kind": "original", "next_retry_at": None, "reason": None}
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            def detail(route):
                response = route.fetch()
                data = response.json()
                if "page_offset=" in route.request.url:
                    requests["detail"] += 1
                    if data.get("pages") and not ready[0]:
                        if not same_url[0]:
                            data["pages"][0]["src"] = None
                            data["pages"][0]["state"] = "missing"
                        else:
                            data["pages"][0]["state"] = "failed"
                    data["recovery"] = {**recovery, "status": "ready" if ready[0] else "queued"}
                route.fulfill(response=response, body=json.dumps(data, ensure_ascii=False))

            page.route("**/api/works/80?*", detail)
            page.goto(f"http://127.0.0.1:{port}/illust/80")
            expect(page.locator(".image-page[data-global-index='0'] .media-placeholder")).to_contain_text("已排队补齐")
            expect(page.locator(".reader-notice")).to_have_count(0)
            pane = page.locator(".page-viewport")
            before = pane.evaluate("e => e.scrollTop")
            ready[0] = True
            expect(page.locator(".image-page[data-global-index='0'] img")).to_be_visible(timeout=20000)
            assert requests["detail"] >= 2, requests
            after = pane.evaluate("e => e.scrollTop")
            metrics["detail_scroll"] = {"before": before, "after": after}
            assert abs(after - before) < 3, metrics["detail_scroll"]
            checks.append("detail-visible-missing-page-auto-recovers-in-place")

            same_url[0] = True
            ready[0] = False
            page.goto(f"http://127.0.0.1:{port}/illust/80")
            image = page.locator(".image-page[data-global-index='0'] img")
            expect(image).to_be_visible()
            expect(page.locator(".reader-notice")).to_have_count(0)
            source = image.get_attribute("src")
            image.dispatch_event("error")
            expect(page.locator(".image-page[data-global-index='0'] .media-placeholder")).to_contain_text("暂时无法显示")
            detail_before = requests["detail"]
            ready[0] = True
            expect(page.locator(".image-page[data-global-index='0'] img")).to_be_visible(timeout=20000)
            assert page.locator(".image-page[data-global-index='0'] img").get_attribute("src") == source
            assert requests["detail"] > detail_before, requests
            checks.append("same-url-failed-image-auto-retries-after-recovery")

            ready[0] = False
            same_url[0] = False
            def flow(route):
                response = route.fetch()
                data = response.json()
                requests["flow"] += 1
                if data.get("items"):
                    if not ready[0]:
                        data["items"][0]["src"] = None
                        data["items"][0]["state"] = "missing"
                    data["items"][0]["recovery"] = {**recovery, "status": "ready" if ready[0] else "queued"}
                route.fulfill(response=response, body=json.dumps(data, ensure_ascii=False))

            page.route("**/api/library/feed?*", flow)
            page.goto(f"http://127.0.0.1:{port}/flow")
            expect(page.locator(".image-page[data-global-index='0'] .media-placeholder")).to_contain_text("已排队补齐")
            expect(page.locator(".reader-notice")).to_have_count(0)
            ready[0] = True
            page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
            expect(page.locator(".image-page[data-global-index='0'] img")).to_be_visible(timeout=10000)
            assert requests["flow"] >= 2, requests
            checks.append("flow-visible-missing-page-refreshes-on-archive-event")
            metrics["requests"] = requests
            page.screenshot(path=str(artifacts / "flow-recovered.png"))
            context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(20)
        (artifacts / "result.json").write_text(json.dumps({"checks": checks, "errors": errors,
            "metrics": metrics, "real_library_used": False, "real_account_used": False},
            ensure_ascii=False, indent=2), encoding="utf-8")
    assert not errors, errors
    print(json.dumps({"checks": checks, "metrics": metrics}, ensure_ascii=False))


if __name__ == "__main__":
    main()
