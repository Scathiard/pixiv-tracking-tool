"""Synthetic pipeline UI contracts; serves only the built frontend and in-memory fixtures."""
from __future__ import annotations

import argparse
from copy import deepcopy
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    frontend = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    assert (frontend / "index.html").is_file(), "Build frontend first"

    from PIL import Image
    from playwright.sync_api import expect, sync_playwright

    buffer = BytesIO()
    Image.new("RGB", (600, 900), (137, 163, 180)).save(buffer, format="PNG")
    media = buffer.getvalue()

    class Static(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(frontend), **kwargs)

        def do_GET(self):
            path = urlsplit(self.path).path
            self.path = path.removeprefix("/reader") if path.startswith("/reader/assets/") else "/index.html"
            super().do_GET()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Static)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    checks, errors, metrics = [], [], {}
    state = {"revision": 10, "repaired": False, "offline": False, "operations": [], "hidden_manual": False,
             "metadata_revision": None, "added": [], "empty": False, "hold_first_progress": False}
    held_progress = []
    calls = {"library": 0, "detail": 0, "feed": 0, "bookmark_refresh": 0}

    def op(identity, label, lane, done, total, status="running"):
        return {"id": identity, "label": label, "lane": lane, "state": status, "source": "manual",
                "phase": {"name": "checking" if lane == "check" else "downloading" if lane == "download" else "thumbnails",
                          "done": done, "total": total, "unit": "works", "detail": ""}, "result": {}}

    def bookmark(identity):
        return {"work_id": identity, "status": "confirmed", "operation_id": None, "desired": None,
                "value": True, "confirmed_at": "2026-09-30T00:00:00Z", "error": None,
                "version": 1, "cached": False, "verified": True, "fact_account_id": "synthetic",
                "current_account_id": "synthetic", "account_epoch": 1}

    def work(identity):
        saved = identity != 2
        complete = identity != 1 or state["repaired"]
        archive = {"state": "complete" if complete else "partial", "total": 2 if identity in (1, 2) else 1,
                   "verified": 2 if identity in (1, 2) and complete else 1, "missing": 0 if complete else 1, "unknown": 0}
        processing = {"state": "queued" if identity == 2 else "attention" if identity == 1 and not complete else "idle",
                      "operation_id": "local" if identity == 2 else None, "next_retry_at": None,
                      "reason": "主站暂时无法获取；可在作品整理中补下载" if identity == 1 and not complete else None}
        if identity == 3:
            archive.update(state="unknown", verified=0, unknown=1)
        return {"id": identity, "pixiv_id": str(1000 + identity), "title": f"合成作品 {identity:03d} — 长标题阅读位置验证",
                "artist_id": 1, "artist_name": "合成画师", "type": "ugoira" if identity == 2 else "manga" if identity == 1 else "illust",
                "page_count": 2 if identity == 1 else 1, "x_restrict": None,
                "thumbnail": "/fixture.png" if saved else None, "preview": "/fixture.png" if saved else None,
                "preview_status": "ready" if saved else "pending", "caption": "<p>合成简介，未使用真实图库。</p>",
                "caption_state": "available", "tags": ["合成"], "is_hidden": False, "is_bookmarked": True,
                "bookmark": bookmark(identity), "rating": 0, "posted_at": "2026-09-30T00:00:00Z",
                "bookmark_count": 1, "view_count": 2, "archive": archive, "processing": processing,
                "source_update": {"pending": identity == 3, "reason": "主站图片已更新；已有存档保留" if identity == 3 else None},
                "availability": {"state": "unavailable", "checked_at": "2026-09-30T00:00:00Z", "reason": "主站暂时无法获取"} if identity == 3 else None,
                "recovery": None}

    def pages(identity):
        item = work(identity)
        return [{"index": index, "src": None if identity == 2 or identity == 1 and index == 1 and not state["repaired"] else "/fixture.png",
                 "width": 600, "height": 900,
                 "state": "unknown" if identity == 3 else "missing" if identity == 2 or identity == 1 and index == 1 and not state["repaired"] else "verified"}
                for index in range(item["page_count"])]

    def identities(query):
        items = list(reversed(state["added"])) + ([] if state["empty"] else list(range(1, 81)))
        if "ceiling" in query:
            items = [identity for identity in items if identity <= int(query["ceiling"][0])]
        return items

    def progress_snapshot():
        visible = [item for item in state["operations"] if not state["hidden_manual"] or item["id"] != "manual"]
        return {"round": None, "operations": deepcopy(visible), "worker_error": None,
                "active_operation_count": sum(item["state"] != "finished" for item in state["operations"]),
                "automatic_paused": False, "catalogue_revision": state["revision"]}

    def route_api(route):
        parsed = urlsplit(route.request.url)
        path, query = parsed.path, parse_qs(parsed.query)
        offset = int(query.get("offset", [0])[0])
        limit = int(query.get("limit", [100])[0])
        if path == "/api/progress":
            if state["hold_first_progress"] and not held_progress:
                held_progress.append(route)
                return
            if state["offline"]:
                route.fulfill(status=503, json={"detail": "synthetic offline"})
                return
            data = progress_snapshot()
        elif path == "/api/status":
            data = {"page_size": 100, "pagination_mode": "scroll"}
        elif path == "/api/library":
            calls["library"] += 1
            ids = identities(query)
            ceiling = int(query["ceiling"][0]) if "ceiling" in query else max(identities({}), default=0)
            data = {"items": [work(i) for i in ids[offset:offset + limit]], "total": len(ids),
                    "offset": offset, "ceiling": ceiling, "as_of": "2026-09-30T00:00:00Z", "catalogue_revision": state["metadata_revision"] if state["metadata_revision"] is not None else state["revision"]}
        elif path == "/api/library/locate":
            identity = int(query["work_id"][0])
            ids = identities(query)
            index = ids.index(identity)
            data = {"work_id": identity, "page": 0, "offset": 0, "work_index": index, "index": sum(len(pages(item)) for item in ids[:index])}
        elif path == "/api/library/feed":
            calls["feed"] += 1
            items = []
            for work_index, identity in enumerate(identities(query)):
                item = work(identity)
                next_index = len(items) + len(pages(identity))
                for page_data in pages(identity):
                    items.append({**item, **page_data, "index": len(items), "work_id": identity,
                                  "page": page_data["index"], "work_index": work_index,
                                  "next_work_index": next_index})
            data = {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "catalogue_revision": state["metadata_revision"] or state["revision"]}
        elif path.startswith("/api/works/"):
            identity = int(path.split("/")[3])
            if path.endswith("/bookmark/refresh"):
                calls["bookmark_refresh"] += 1
                data = bookmark(identity)
            elif path.endswith("/bookmark"):
                data = bookmark(identity)
            else:
                calls["detail"] += 1
                first = int(query.get("page_offset", [0])[0])
                count = int(query.get("page_limit", [100])[0])
                data = {**work(identity), "pages": pages(identity)[first:first + count],
                        "display_page_count": work(identity)["page_count"], "assets": [], "legacy_variants": [],
                        "catalogue_revision": state["metadata_revision"] or state["revision"]}
        elif path == "/api/tasks":
            state["operations"].append(op("manual", "手动检查", "check", 0, 7, "queued"))
            state["hidden_manual"] = True
            data = {"operation_id": "manual", "created": True}
        elif path.startswith("/api/tasks/"):
            data = next(item for item in state["operations"] if item["id"] == path.split("/")[-1])
        else:
            errors.append(f"Unexpected synthetic API route: {path}")
            route.fulfill(status=404, json={"detail": "unhandled fixture route"})
            return
        route.fulfill(json=data)

    try:
        executable = next((Path(root) / suffix for root in [os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("PROGRAMFILES", "")]
                           for suffix in ["Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"]
                           if (Path(root) / suffix).is_file()), None)
        assert executable, "No installed Chromium browser"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=str(executable))
            for width in (1440, 390, 320):
                context = browser.new_context(viewport={"width": width, "height": 900})
                page = context.new_page()
                page.add_init_script("window.__pipelineUpdated = 0; window.addEventListener('archive-updated', () => window.__pipelineUpdated++)")
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.route("**/api/**", route_api)
                page.route("**/fixture.png", lambda route: route.fulfill(body=media, content_type="image/png"))
                state.update(repaired=False, offline=False, hidden_manual=False, operations=[op("check", "发现新作品", "check", 2, 11),
                             op("local", "生成本地预览", "local", 18, 29), op("download", "保存原件", "download", 1, 2000)])
                page.goto(origin + "/?q=合成")
                expect(page.locator(".work-card[data-work-id='1'] .card-source")).to_have_text("存档缺 1 项")
                expect(page.locator(".work-card[data-work-id='2']")).to_contain_text("原件已存，动图预览待生成")
                expect(page.locator(".work-card[data-work-id='2'] .card-source")).to_have_count(0)
                expect(page.locator(".work-card[data-work-id='3'] .card-source")).to_have_text("主站内容有变化")
                expect(page.locator(".work-card[data-work-id='3'] img")).to_be_visible()
                page.wait_for_function("[...document.querySelectorAll('.work-card img')].some(e => e.complete && e.naturalWidth === 600)")
                expect(page.locator(".task-primary strong")).to_have_text("保存原件")
                page.get_by_role("button", name="检查新作品", exact=True).click()
                expect(page.locator(".task-secondary")).to_contain_text("手动检查（等待）")
                expect(page.locator(".task-primary strong")).to_have_text("保存原件")
                expect(page.locator(".task-track")).to_have_attribute("aria-valuemax", "2000")
                state["hidden_manual"] = False
                state["operations"][-1].update(state="running", phase=op("manual", "", "check", 3, 7)["phase"])
                expect(page.locator(".task-primary strong")).to_have_text("手动检查", timeout=6000)
                expect(page.locator(".task-track")).to_have_attribute("aria-valuenow", "3")
                state["operations"].reverse()
                state["operations"][0]["phase"]["total"] = None
                expect(page.locator(".task-phase")).to_contain_text("已处理 3", timeout=6000)
                expect(page.locator(".task-track")).to_have_count(0)
                state["offline"] = True
                expect(page.locator(".task-status")).to_contain_text("正在重连", timeout=6000)
                expect(page.locator(".task-track")).to_have_count(0)
                state["offline"] = False
                state["operations"][0]["phase"]["total"] = 7
                expect(page.locator(".task-track")).to_have_attribute("aria-valuemax", "7", timeout=6000)
                checks.append(f"{width}-lanes-manual-queue-stable-primary-honest-unknown-reconnect")

                pane = page.locator(".gallery-viewport")
                pane.hover()
                page.mouse.wheel(0, 650)
                page.wait_for_function("document.querySelector('.gallery-viewport').scrollTop > 100")
                page.wait_for_timeout(300)
                top = pane.evaluate("e => e.scrollTop")
                refreshes = calls["bookmark_refresh"]
                before_calls = calls["library"]
                before_events = page.evaluate("window.__pipelineUpdated")
                state["revision"] += 1
                state["repaired"] = True
                page.wait_for_function("n => window.__pipelineUpdated > n", arg=before_events, timeout=6500)
                page.wait_for_timeout(300)
                expect(page.locator(".work-card[data-work-id='1'] .card-source")).to_have_count(0, timeout=6500)
                assert calls["library"] > before_calls
                assert abs(pane.evaluate("e => e.scrollTop") - top) < 3
                assert "q=" in page.url and "%E5%90%88%E6%88%90" in page.url
                assert calls["bookmark_refresh"] == refreshes
                assert page.locator(".update-notice").count() == 0
                received_events = page.evaluate("window.__pipelineUpdated")
                page.wait_for_timeout(1700)
                assert page.evaluate("window.__pipelineUpdated") == received_events
                metrics[f"{width}_gallery_scroll"] = top
                page.screenshot(path=str(artifacts / f"gallery-{width}.png"))
                checks.append(f"{width}-revision-card-refresh-preserves-wheel-scroll-filter-bookmarks")

                state["repaired"] = False
                page.goto(origin + "/illust/1")
                expect(page.locator(".image-page[data-page='0'] img")).to_be_visible()
                expect(page.locator(".image-page[data-page='1'] .media-placeholder")).to_contain_text("本地缺少此页")
                expect(page.locator(".source-notice")).to_contain_text("本地存档缺 1 项")
                page.wait_for_timeout(300)
                top = page.locator(".page-viewport").evaluate("e => e.scrollTop")
                # Allow the existing initial same-account recheck to finish before comparing refresh counts.
                page.wait_for_timeout(2700)
                refreshes = calls["bookmark_refresh"]
                state.update(repaired=True, revision=state["revision"] + 1)
                expect(page.locator(".image-page[data-page='1'] img")).to_have_count(1, timeout=6500)
                page.wait_for_function("document.querySelector('.image-page[data-page=\"1\"] img').naturalWidth === 600")
                assert abs(page.locator(".page-viewport").evaluate("e => e.scrollTop") - top) < 3
                assert calls["bookmark_refresh"] == refreshes
                expect(page.locator(".source-notice")).to_have_count(0)
                checks.append(f"{width}-missing-p1-recovers-readable-p0-detail-in-place")

                if width == 1440:
                    old_revision = state["revision"] - 1
                    state.update(repaired=False, metadata_revision=old_revision, revision=state["revision"] + 1)
                    before_events = page.evaluate("window.__pipelineUpdated")
                    page.wait_for_function("n => window.__pipelineUpdated > n", arg=before_events, timeout=6500)
                    page.wait_for_timeout(300)
                    expect(page.locator(".image-page[data-page='1'] img")).to_have_count(1)
                    expect(page.locator(".source-notice")).to_have_count(0)
                    state.update(repaired=True, metadata_revision=None)
                    checks.append("1440-older-detail-response-cannot-replace-newer-visible-archive")

                page.goto(origin + "/illust/2")
                expect(page.locator(".image-page .media-placeholder")).to_contain_text("原件已存，动图预览待生成")
                page.goto(origin + "/illust/3")
                expect(page.locator(".image-page img")).to_be_visible()
                expect(page.locator(".source-notice")).to_contain_text("已有存档保留")
                page.wait_for_function("document.querySelector('.image-page img').naturalWidth === 600")
                checks.append(f"{width}-saved-original-preview-pending-old-source-update-nondestructive")

                page.goto(origin + "/flow")
                expect(page.locator(".page-viewport.feed .image-page").first).to_be_visible()
                page.locator(".page-viewport").hover()
                page.mouse.wheel(0, 350)
                page.wait_for_timeout(450)
                top = page.locator(".page-viewport").evaluate("e => e.scrollTop")
                assert top > 100
                state["revision"] += 1
                before_calls = calls["feed"]
                page.wait_for_timeout(1900)
                assert calls["feed"] > before_calls
                assert abs(page.locator(".page-viewport").evaluate("e => e.scrollTop") - top) < 3
                assert page.locator(".page-title").count() > 0
                assert page.locator(".reading-toolbar .bookmark-button").count() == 1
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(artifacts / f"feed-{width}.png"))
                checks.append(f"{width}-feed-wheel-title-bookmark-revision-position")
                for item in state["operations"]:
                    item.update(state="finished", result={"outcome": "succeeded"})
                expect(page.locator(".task-status")).to_have_count(0, timeout=6500)
                checks.append(f"{width}-idle-progress-layer-hidden")
                context.close()
                print(f"{width}px: pipeline UI checks passed", flush=True)

            for width in (1440, 390, 320):
                state.update(repaired=True, offline=False, hidden_manual=False, metadata_revision=None,
                             added=[], empty=False, hold_first_progress=False,
                             operations=[op("download", "保存原件", "download", 1, 2000)])
                context = browser.new_context(viewport={"width": width, "height": 900})
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("window.__pipelineUpdated = 0; window.addEventListener('archive-updated', () => window.__pipelineUpdated++)")
                page.route("**/api/**", route_api)
                page.route("**/fixture.png", lambda route: route.fulfill(body=media, content_type="image/png"))
                page.goto(origin + "/?q=合成&sort=added")
                expect(page.locator(".work-card[data-work-id='1']")).to_be_visible()
                page.evaluate("window.documentMarker='new-work-preserved'")
                state["added"].append(81)
                state["revision"] += 1
                expect(page.locator(".work-card[data-work-id='81'] img")).to_be_visible(timeout=7000)
                assert page.locator(".gallery-viewport").evaluate("e => e.scrollTop") < 4
                expect(page.locator(".new-work-entry")).to_have_count(0)
                assert parse_qs(urlsplit(page.url).query)["sort"] == ["added"]
                checks.append(f"{width}-new-work-at-top-appears-with-current-filter-sort")

                pane = page.locator(".gallery-viewport")
                pane.hover()
                page.mouse.wheel(0, 650)
                page.wait_for_timeout(450)
                top = pane.evaluate("e => e.scrollTop")
                assert top > 100
                state["added"].append(82)
                state["revision"] += 1
                expect(page.get_by_role("button", name="有新作品，查看", exact=True)).to_be_visible(timeout=7000)
                assert abs(pane.evaluate("e => e.scrollTop") - top) < 3
                assert parse_qs(urlsplit(page.url).query)["ceiling"] == ["81"]
                assert page.locator(".work-card[data-work-id='82']").count() == 0
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.screenshot(path=str(artifacts / f"new-work-pending-{width}.png"))
                page.get_by_role("button", name="有新作品，查看", exact=True).click()
                expect(page.locator(".work-card[data-work-id='82'] img")).to_be_visible(timeout=7000)
                assert pane.evaluate("e => e.scrollTop") < 4
                assert parse_qs(urlsplit(page.url).query)["q"] == ["合成"]
                assert parse_qs(urlsplit(page.url).query)["sort"] == ["added"]
                assert page.evaluate("window.documentMarker") == "new-work-preserved"
                before_events = page.evaluate("window.__pipelineUpdated")
                state["revision"] += 1  # A media publication, with no new work IDs.
                page.wait_for_function("n => window.__pipelineUpdated > n", arg=before_events, timeout=7000)
                page.wait_for_timeout(300)
                expect(page.locator(".new-work-entry")).to_have_count(0)
                checks.append(f"{width}-scrolled-new-work-entry-keeps-place-and-media-only-does-not-prompt")

                if width < 760:
                    page.get_by_role("button", name="打开菜单", exact=True).click()
                page.get_by_role("navigation", name="主导航").get_by_role("button", name="流式阅览", exact=True).click()
                expect(page.locator(".page-viewport.feed .image-page").first).to_be_visible()
                page.locator(".page-viewport").hover()
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(450)
                top = page.locator(".page-viewport").evaluate("e => e.scrollTop")
                assert top > 100
                page.wait_for_timeout(2700)
                refreshes = calls["bookmark_refresh"]
                state["added"].append(83)
                state["revision"] += 1
                before_events = page.evaluate("window.__pipelineUpdated")
                page.wait_for_function("n => window.__pipelineUpdated > n", arg=before_events, timeout=7000)
                page.wait_for_timeout(300)
                assert parse_qs(urlsplit(page.url).query)["ceiling"] == ["82"]
                assert abs(page.locator(".page-viewport").evaluate("e => e.scrollTop") - top) < 3
                assert page.locator(".image-page[data-work-id='83']").count() == 0
                assert calls["bookmark_refresh"] == refreshes
                expect(page.locator(".reading-counter small")).to_contain_text("/ 82 件")
                page.get_by_role("button", name="返回我的图库", exact=True).click()
                expect(page.get_by_role("button", name="有新作品，查看", exact=True)).to_be_visible(timeout=7000)
                page.get_by_role("button", name="有新作品，查看", exact=True).click()
                expect(page.locator(".work-card[data-work-id='83'] img")).to_be_visible(timeout=7000)
                checks.append(f"{width}-feed-keeps-range-and-logo-preserves-new-work-entry")
                context.close()
                print(f"{width}px: new work discovery checks passed", flush=True)

            # A numbered page also keeps its current range until the user chooses the new works.
            state.update(added=[], empty=False, repaired=True, hold_first_progress=False,
                         operations=[op("download", "保存原件", "download", 1, 2000)])
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.route("**/api/**", route_api)
            page.route("**/fixture.png", lambda route: route.fulfill(body=media, content_type="image/png"))
            page.add_init_script("localStorage.setItem('pixiv-local-page-size', '50')")
            page.goto(origin + "/?page=2&q=合成")
            expect(page.get_by_role("spinbutton", name="图库页码")).to_have_value("2")
            state["added"].append(81)
            state["revision"] += 1
            expect(page.get_by_role("button", name="有新作品，查看", exact=True)).to_be_visible(timeout=7000)
            expect(page.get_by_role("spinbutton", name="图库页码")).to_have_value("2")
            page.get_by_role("button", name="有新作品，查看", exact=True).click()
            expect(page.get_by_role("spinbutton", name="图库页码")).to_have_value("1")
            expect(page.locator(".work-card[data-work-id='81'] img")).to_be_visible()
            checks.append("1440-other-numbered-page-retained-until-new-work-action")
            context.close()

            for scenario in ("empty", "missing-page"):
                state.update(added=[], empty=scenario == "empty", repaired=False, revision=0, metadata_revision=None,
                             hold_first_progress=True, operations=[], hidden_manual=False)
                held_progress.clear()
                context = browser.new_context(viewport={"width": 390, "height": 900})
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("window.__pipelineUpdated = 0; window.addEventListener('archive-updated', () => window.__pipelineUpdated++)")
                page.route("**/api/**", route_api)
                page.route("**/fixture.png", lambda route: route.fulfill(body=media, content_type="image/png"))
                page.goto(origin + ("/" if scenario == "empty" else "/illust/1"))
                if scenario == "empty":
                    expect(page.locator(".gallery-viewport .empty")).to_contain_text("这个范围里还没有作品")
                else:
                    expect(page.locator(".image-page[data-page='1'] .media-placeholder")).to_contain_text("本地缺少此页")
                    page.wait_for_timeout(2700)
                assert held_progress and page.evaluate("window.__pipelineUpdated") == 0
                refreshes = calls["bookmark_refresh"]
                state.update(repaired=True, revision=2, hold_first_progress=False)
                if scenario == "empty":
                    state["added"].append(1)
                held_progress[0].fulfill(json=progress_snapshot())
                if scenario == "empty":
                    expect(page.locator(".work-card[data-work-id='1'] img")).to_be_visible(timeout=7000)
                    expect(page.locator(".new-work-entry")).to_have_count(0)
                else:
                    expect(page.locator(".image-page[data-page='1'] img")).to_have_count(1, timeout=7000)
                    page.wait_for_function("document.querySelector('.image-page[data-page=\"1\"] img').naturalWidth === 600")
                    assert calls["bookmark_refresh"] == refreshes
                assert page.evaluate("window.__pipelineUpdated") == 1
                checks.append(f"390-first-progress-after-old-{scenario}-data-coalesces-first-publish-refresh")
                context.close()
                held_progress.clear()
            browser.close()
        assert not errors, errors
    except Exception as error:
        errors.append(str(error))
        raise
    finally:
        server.shutdown()
        server.server_close()
        (artifacts / "results.json").write_text(json.dumps({"checks": checks, "errors": errors,
            "metrics": metrics, "calls": calls}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": len(checks), "errors": errors, "artifacts": str(artifacts)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
