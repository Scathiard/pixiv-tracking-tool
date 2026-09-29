"""Browser acceptance for source availability and a local repair, using synthetic data."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
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
    parser.add_argument("--quick-final", action="store_true",
        help="Recheck gallery markers and detail recovery after a UI-only rebuild")
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    script = Path(__file__).resolve()
    project = script.parents[4] if script.parents[2].parent.name == ".worktrees" else script.parents[2]
    allowed = project / ".devtools" / "test-runs"
    if artifacts.parent != allowed or not artifacts.name.startswith("b4-availability-browser-"):
        raise SystemExit(f"--artifacts must be a new b4-availability-browser-* directory under {allowed}")
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "runtime-env")
    sys.path.insert(0, str(script.parents[1]))

    import uvicorn
    from playwright.sync_api import expect, sync_playwright
    from sqlalchemy import select
    from client_fixture import make_library
    from src.auth import _find_system_browser
    runtime, _ = make_library(artifacts / "fixture", 80)
    from src.core.models import ApplicationState, Asset, Work
    from src.web import create_app
    checked_at = datetime.now(timezone.utc).isoformat()
    attention_reason = "主站暂时无法获取第 1 页；本地其余页面仍可阅览"
    unavailable_reason = "主站暂时无法获取，删除状态未获证实"
    private_reason = "作品已转为非公开，当前账号无法获取"
    with runtime.database.write() as session:
        for work_id, state, reason in ((80, "unavailable", unavailable_reason),
                                       (79, "private", private_reason),
                                       (78, "unavailable", unavailable_reason)):
            work = session.get(Work, work_id)
            work.source_metadata = {**(work.source_metadata or {}), "remote_availability": {
                "state": state, "checked_at": checked_at, "reason": reason}}
        missing = session.scalar(select(Asset).where(
            Asset.work_id == 80, Asset.role == "image", Asset.page_index == 0))
        original = missing.relative_path
        missing.state = "missing"
        missing.error = attention_reason
        session.add(ApplicationState(key="maintenance_work:80", value={
            "status": "attention", "kind": "repair", "next_retry_at": None,
            "reason": attention_reason, "tracks": {"remote": {
                "status": "attention", "kind": "repair", "next_retry_at": None,
                "reason": attention_reason}}}))

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True),
        host="127.0.0.1", port=port, log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{port}"
    checks, errors, metrics = [], [], {"http_errors": [], "ignored_browser_resource_errors": []}
    active_page = None
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started, "synthetic server did not start"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())

            def observe(page):
                page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))
                def console(message):
                    if message.type != "error":
                        return
                    item = f"console: {message.text} @ {message.location}"
                    if str(message.location.get("url", "")).endswith("/favicon.ico"):
                        metrics["ignored_browser_resource_errors"].append(item)
                    else:
                        errors.append(item)
                page.on("console", console)
                page.on("response", lambda response: metrics["http_errors"].append({
                    "status": response.status, "url": response.url}) if response.status >= 400 else None)

            desktop = browser.new_context(viewport={"width": 1440, "height": 900})
            page = active_page = desktop.new_page()
            observe(page)
            counts = {"library": 0, "detail": 0, "feed": 0}

            def count(request):
                url = request.url
                if "/api/library/feed?" in url:
                    counts["feed"] += 1
                elif "/api/library?" in url:
                    counts["library"] += 1
                elif "/api/works/80?" in url:
                    counts["detail"] += 1

            page.on("request", count)
            page.goto(origin + "/")
            expect(page.locator(".work-card[data-work-id='80']")).to_be_visible()
            expect(page.locator(".work-card[data-work-id='79']")).to_be_visible()
            expect(page.locator(".work-card[data-work-id='78']")).to_be_visible()
            for work_id in (79, 78):
                card = page.locator(f".work-card[data-work-id='{work_id}']")
                expect(card.locator("img")).to_be_visible()
                assert "已删除" not in card.inner_text()
            for work_id, label, reason in ((80, "补齐待核对", attention_reason),
                                           (79, "主站非公开", private_reason),
                                           (78, "主站不可获取", unavailable_reason)):
                marker = page.locator(f".work-card[data-work-id='{work_id}'] .card-source")
                expect(marker).to_have_text(label)
                assert marker.get_attribute("title") == reason
            metrics["desktop_card_heights"] = [
                page.locator(f".work-card[data-work-id='{work_id}'] .card-open")
                    .bounding_box()["height"] for work_id in (80, 79, 78, 77)]
            assert max(metrics["desktop_card_heights"]) - min(metrics["desktop_card_heights"]) <= 1
            page.screenshot(path=str(artifacts / "gallery-1440.png"))
            checks.append("full-local-art-remains-in-gallery-with-private-or-unavailable-source")
            checks.append("gallery-cards-show-distinct-attention-private-and-unavailable-markers")

            if args.quick_final:
                for width, height in ((390, 844), (320, 700)):
                    mobile = browser.new_context(viewport={"width": width, "height": height},
                        is_mobile=True, has_touch=True)
                    mobile_page = active_page = mobile.new_page()
                    observe(mobile_page)
                    mobile_page.goto(origin + "/")
                    for work_id, label in ((80, "补齐待核对"), (79, "主站非公开"),
                                           (78, "主站不可获取")):
                        card = mobile_page.locator(f".work-card[data-work-id='{work_id}']")
                        card.scroll_into_view_if_needed()
                        expect(card.locator("img")).to_be_visible()
                        expect(card.locator(".card-source")).to_have_text(label)
                        assert card.locator(".card-source").is_visible()
                    overflow = mobile_page.evaluate(
                        "({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})")
                    metrics[f"gallery_mobile_{width}_overflow"] = overflow
                    assert overflow["scroll"] <= width + 1
                    mobile_page.screenshot(path=str(artifacts / f"gallery-{width}.png"))
                    mobile.close()
                active_page = page
                checks.append("gallery-markers-remain-visible-at-390-and-320-without-overflow")

                page.goto(origin + "/illust/80")
                expect(page.locator(".detail-head .source-notice")).to_contain_text(attention_reason)
                pane = page.locator(".page-viewport")
                pane.evaluate("element => element.scrollTop = 120")
                before = pane.evaluate("element => element.scrollTop")
                assert before >= 100
                with runtime.database.write() as session:
                    asset = session.scalar(select(Asset).where(
                        Asset.work_id == 80, Asset.role == "image", Asset.page_index == 0))
                    asset.state = "verified"
                    asset.error = None
                    assert asset.relative_path == original
                    work = session.get(Work, 80)
                    work.source_metadata = {**(work.source_metadata or {}), "remote_availability": {
                        "state": "available", "checked_at": datetime.now(timezone.utc).isoformat(),
                        "reason": None}}
                    ledger = session.get(ApplicationState, "maintenance_work:80")
                    ledger.value = {"status": "ready", "kind": "repair", "next_retry_at": None,
                        "reason": None, "tracks": {"remote": {"status": "ready", "kind": "repair"}}}
                page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
                expect(page.locator(".image-page[data-global-index='0'] img"))\
                    .to_be_visible(timeout=10000)
                expect(page.locator(".detail-head .source-notice")).to_have_count(0)
                after = pane.evaluate("element => element.scrollTop")
                metrics["detail_scroll_after_repair"] = {"before": before, "after": after}
                assert abs(after - before) < 30
                page.screenshot(path=str(artifacts / "recovered-1440.png"))
                checks.append("latest-build-detail-source-and-image-recover-in-place")
                assert not errors, errors
                desktop.close()
                browser.close()
                print(json.dumps({"checks": checks, "metrics": metrics}, ensure_ascii=False))
                return

            page.goto(origin + "/illust/78")
            expect(page.locator(".detail-head .source-notice")).to_contain_text(unavailable_reason)
            expect(page.locator(".image-page[data-global-index='0'] img")).to_be_visible()
            caption = page.locator(".detail-caption")
            caption.scroll_into_view_if_needed()
            expect(caption).to_contain_text("完整简介第一段")
            assert "已删除" not in page.locator(".detail-head").inner_text()
            checks.append("full-local-art-and-caption-remain-available-in-detail")

            page.goto(origin + "/illust/79")
            expect(page.locator(".detail-head .source-notice")).to_contain_text(private_reason)
            expect(page.locator(".image-page[data-global-index='0'] img")).to_be_visible()
            checks.append("private-source-is-distinct-from-deletion")

            page.goto(origin + "/illust/80")
            expect(page.locator(".detail-head .source-notice")).to_contain_text(attention_reason)
            placeholder = page.locator(".image-page[data-global-index='0'] .media-placeholder")
            expect(placeholder).to_contain_text(attention_reason)
            expect(page.locator(".reader-notice")).to_have_count(0)
            assert "已删除" not in page.locator(".detail-head").inner_text()
            page.screenshot(path=str(artifacts / "attention-1440.png"))
            checks.append("attention-reason-is-visible-without-hover-on-desktop")

            for width, height in ((390, 844), (320, 700)):
                mobile = browser.new_context(viewport={"width": width, "height": height},
                    is_mobile=True, has_touch=True)
                mobile_page = active_page = mobile.new_page()
                observe(mobile_page)
                mobile_page.goto(origin + "/illust/80")
                notice = mobile_page.locator(".detail-head .source-notice")
                expect(notice).to_contain_text(attention_reason)
                expect(mobile_page.locator(".image-page[data-global-index='0'] .media-placeholder"))\
                    .to_contain_text(attention_reason)
                assert notice.is_visible()
                metrics[f"mobile_{width}_overflow"] = mobile_page.evaluate(
                    "({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})")
                assert metrics[f"mobile_{width}_overflow"]["scroll"] <= width + 1
                mobile_page.screenshot(path=str(artifacts / f"attention-{width}.png"))
                mobile.close()
            active_page = page
            checks.append("attention-reason-is-visible-at-390-and-320-without-horizontal-overflow")

            page.goto(origin + "/flow")
            expect(page.locator(".page-viewport.feed .image-page[data-global-index='0'] .page-source"))\
                .to_contain_text(attention_reason)
            assert page.locator(".page-viewport.feed .image-page .page-source").count() == 1
            page.locator(".page-viewport.feed").evaluate("element => element.scrollTop = 1200")
            expect(page.locator(".page-viewport.feed .image-page[data-global-index='1']"))\
                .to_be_visible()
            second = page.locator(".page-viewport.feed .image-page[data-global-index='1']")
            assert second.locator(".page-title,.page-source").count() == 0
            metrics["flow_window_size_with_attention"] = page.locator(".page-window").get_attribute("data-window-size")
            assert int(metrics["flow_window_size_with_attention"]) <= 120
            page.screenshot(path=str(artifacts / "flow-source-once-1440.png"))
            checks.append("feed-multipage-source-and-title-only-on-first-page-with-bounded-window")

            page.goto(origin + "/illust/80")
            expect(page.locator(".image-page[data-global-index='0'] .media-placeholder"))\
                .to_contain_text(attention_reason)

            idle = counts.copy()
            page.wait_for_timeout(13000)
            metrics["requests_during_attention_13s"] = {
                key: counts[key] - idle[key] for key in counts}
            assert metrics["requests_during_attention_13s"]["detail"] == 0
            assert metrics["requests_during_attention_13s"]["library"] == 0
            checks.append("attention-does-not-poll-library-or-detail-after-12-seconds")

            pane = page.locator(".page-viewport")
            pane.evaluate("element => element.scrollTop = 120")
            before = pane.evaluate("element => element.scrollTop")
            assert before >= 100, before
            with runtime.database.write() as session:
                asset = session.scalar(select(Asset).where(
                    Asset.work_id == 80, Asset.role == "image", Asset.page_index == 0))
                asset.state = "verified"
                asset.error = None
                assert asset.relative_path == original
                work = session.get(Work, 80)
                work.source_metadata = {**(work.source_metadata or {}), "remote_availability": {
                    "state": "available", "checked_at": datetime.now(timezone.utc).isoformat(),
                    "reason": None}}
                ledger = session.get(ApplicationState, "maintenance_work:80")
                ledger.value = {"status": "ready", "kind": "repair", "next_retry_at": None,
                    "reason": None, "tracks": {"remote": {"status": "ready", "kind": "repair"}}}
            page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
            expect(page.locator(".image-page[data-global-index='0'] img")).to_be_visible(timeout=10000)
            expect(page.locator(".detail-head .source-notice")).to_have_count(0)
            after = pane.evaluate("element => element.scrollTop")
            metrics["detail_scroll_after_repair"] = {"before": before, "after": after}
            assert abs(after - before) < 30
            page.screenshot(path=str(artifacts / "recovered-1440.png"))
            checks.append("synthetic-database-repair-restores-image-and-source-in-place")

            page.goto(origin + "/flow")
            expect(page.locator(".page-viewport.feed .image-page[data-global-index='0'] img"))\
                .to_be_visible()
            metrics["flow_window_size"] = page.locator(".page-window").get_attribute("data-window-size")
            assert int(metrics["flow_window_size"]) <= 120
            page.screenshot(path=str(artifacts / "flow-recovered-1440.png"))
            checks.append("feed-remains-bounded-after-repair")

            assert not errors, errors
            desktop.close()
            browser.close()
    except Exception:
        metrics["failure"] = traceback.format_exc()
        try:
            if active_page and not active_page.is_closed():
                active_page.screenshot(path=str(artifacts / "failure.png"))
        except Exception:
            pass
        raise
    finally:
        server.should_exit = True
        thread.join(20)
        (artifacts / "result.json").write_text(json.dumps({
            "checks": checks, "errors": errors, "metrics": metrics,
            "frontend_index_sha256": hashlib.sha256(
                (script.parents[1] / "frontend/dist/index.html").read_bytes()).hexdigest(),
            "real_library_used": False, "real_account_used": False},
            ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": checks, "metrics": metrics}, ensure_ascii=False))


if __name__ == "__main__":
    main()
