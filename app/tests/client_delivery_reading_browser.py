"""Integrated reader against a real isolated backend; no Pixiv or real gallery."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--artifacts", type=Path, required=True)
    out = parser.parse_args().artifacts.resolve(); out.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(out / "configuration")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from client_fixture import make_library
    from src.auth import _find_system_browser
    from src.core.models import Work
    from src.core.revisions import ensure_legacy_revision, observe_revision
    from src.web import create_app
    from playwright.sync_api import sync_playwright
    import uvicorn
    runtime, remote = make_library(out / "fixture", works=72)
    with runtime.database.write() as session:
        work = session.get(Work, 71)
        revision = ensure_legacy_revision(session, work)
        old_id = revision.id
        snapshot = {name: getattr(revision, name) for name in ("title", "caption", "caption_state", "type", "page_count", "tags", "source_metadata")}
        snapshot.update(title="新版本标题", caption="<p>新版完整简介</p>")
        observe_revision(session, work, snapshot)
    with socket.socket() as probe: probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True), host="127.0.0.1", port=port, log_level="error", loop="src.networking:http_loop_factory"))
    worker = threading.Thread(target=server.run, daemon=True); worker.start()
    result = {"checks": [], "errors": [], "failures": [], "real_account_used": False, "real_phone_tested": False, "backend": "real isolated runtime"}
    page = None
    try:
        deadline = time.monotonic() + 25
        while not server.started and time.monotonic() < deadline: time.sleep(.05)
        assert server.started
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            context = browser.new_context(viewport={"width":1440,"height":960})
            page = context.new_page(); page.set_default_timeout(15000)
            detail_requests = []
            page.on("request", lambda request: detail_requests.append(request.url) if "/api/works/" in request.url and "page_limit=" in request.url else None)
            page.on("pageerror", lambda error: result["errors"].append(str(error)))
            page.goto(origin + "/illust/71")
            page.get_by_role("heading", name="新版本标题", exact=True).last.wait_for()
            page.wait_for_function("Number(document.querySelector('.sidebar-scopes button small')?.textContent)>0")
            page.get_by_label("存档版本").select_option(old_id)
            page.get_by_role("heading", name="山与海 · 本地作品 00071", exact=True).last.wait_for()
            assert "revision_id=" in page.url
            page.reload(); page.get_by_role("heading", name="山与海 · 本地作品 00071", exact=True).last.wait_for()
            result["checks"].append("history-selector-and-direct-refresh-pin")
            page.get_by_role("button", name="下一件作品", exact=True).click()
            page.get_by_role("heading", name="山与海 · 本地作品 00070", exact=True).last.wait_for()
            page.get_by_role("button", name="上一件作品", exact=True).click()
            page.get_by_role("heading", name="新版本标题", exact=True).last.wait_for()
            # This first return changes to the session's default version. A
            # second A-B-A reuses that exact pinned version's cached response.
            page.get_by_role("button", name="下一件作品", exact=True).click()
            page.get_by_role("heading", name="山与海 · 本地作品 00070", exact=True).last.wait_for()
            before_cached = len([request for request in detail_requests if "/api/works/71?" in request])
            page.get_by_role("button", name="上一件作品", exact=True).click()
            page.get_by_role("heading", name="新版本标题", exact=True).last.wait_for()
            assert len([request for request in detail_requests if "/api/works/71?" in request]) == before_cached
            result["checks"].append("same-version-one-page-detail-roundtrip-reuses-cache")
            page.get_by_role("button", name="下一件作品", exact=True).click()
            page.get_by_role("heading", name="山与海 · 本地作品 00070", exact=True).last.wait_for()
            page.get_by_role("button", name="整理作品", exact=True).last.click()
            page.get_by_role("button", name="隐藏此作品", exact=True).click()
            page.get_by_role("heading", name="山与海 · 本地作品 00069", exact=True).last.wait_for()
            result["checks"].append("hide-current-continues-exact-next-identity")
            page.get_by_role("button", name="返回我的图库", exact=True).last.click()
            page.locator(".work-card").first.wait_for()
            assert page.locator(".gallery-viewport").evaluate("e=>e.scrollTop") == 0
            page.goto(origin + "/flow?types=illust")
            page.locator(".image-page img").first.wait_for()
            first = page.locator(".image-page").first.get_attribute("data-work-id")
            page.get_by_role("button", name="下一件作品", exact=True).click(); page.wait_for_timeout(500)
            page.get_by_role("button", name="上一件作品", exact=True).click(); page.wait_for_timeout(500)
            assert page.locator(".reading-counter strong").inner_text().endswith(f"{int(first):05}") or int(first) == 71
            page.get_by_label("流式阅览顺序").select_option("random")
            page.wait_for_timeout(500)
            assert page.get_by_label("流式阅览顺序").input_value() == "random"
            assert page.locator(".page-window").evaluate("e=>+e.dataset.windowSize") <= 120
            result["checks"].append("stream-previous-next-and-random-mode")
            page.get_by_role("button", name="作品信息与整理", exact=True).click()
            page.get_by_role("button", name="隐藏此作品", exact=True).wait_for()
            page.get_by_role("button", name="关闭作品整理", exact=True).click()
            result["checks"].append("stream-current-management-accessible-from-toolbar")
            for width in (1440, 390, 320):
                page.set_viewport_size({"width":width,"height":900}); page.wait_for_timeout(300)
                toolbar = page.locator(".reading-toolbar").bounding_box()
                assert toolbar and toolbar["x"] >= 0 and toolbar["x"] + toolbar["width"] <= width + .5
                for button in page.locator(".reading-toolbar button").all():
                    box = button.bounding_box(); assert box and box["width"] >= 40 and box["height"] >= 40
                page.screenshot(path=str(out / f"flow-{width}.png"))
            result["checks"].append("toolbar-visible-1440-390-320")
            page.set_viewport_size({"width":390,"height":900})
            # Choose the visible part of a page, not an overscan row above the
            # viewport; automatic locator scrolling would change the target.
            point = None
            for candidate in page.locator(".image-page img").all():
                box = candidate.bounding_box()
                if box and box["y"] < 650 and box["y"] + box["height"] > 230:
                    point = (box["x"] + box["width"] / 2, min(600, max(240, box["y"] + 160)))
                    break
            assert point
            page.mouse.click(*point); page.get_by_role("dialog", name="放大查看").wait_for()
            canvas = page.locator(".zoom-canvas img")
            canvas.dblclick(position={"x":100,"y":100})
            page.wait_for_timeout(100)
            assert page.locator(".zoom-toolbar").inner_text().find("250%") >= 0
            page.get_by_role("button", name="关闭放大", exact=False).click()
            assert page.get_by_role("dialog", name="放大查看").count() == 0
            before = remote.write_count
            page.mouse.dblclick(*point)
            page.locator(".tap-heart").wait_for()
            deadline = time.monotonic()+5
            while remote.write_count == before and time.monotonic()<deadline: page.wait_for_timeout(50)
            assert remote.write_count == before + 1
            assert page.get_by_role("dialog", name="放大查看").count() == 0
            page.wait_for_timeout(3200)
            page.mouse.dblclick(*point); page.wait_for_timeout(500)
            assert remote.write_count == before + 1
            result["checks"].append("single-zoom-double-add-only-and-zoom-double-scale")
            page.screenshot(path=str(out / "flow-heart-mobile.png"))
            assert not result["errors"], result["errors"]
            context.close()
            entered, release = threading.Event(), threading.Event()
            original_read = remote.read_bookmark
            def delayed_read(remote_id):
                entered.set(); assert release.wait(10)
                return original_read(remote_id)
            remote.read_bookmark = delayed_read
            context = browser.new_context(viewport={"width":390,"height":900})
            page = context.new_page(); page.set_default_timeout(15000)
            page.on("pageerror", lambda error: result["errors"].append(str(error)))
            page.goto(origin + "/flow?types=illust&q=00068")
            page.locator(".image-page img").first.wait_for()
            deadline = time.monotonic() + 5
            while not entered.is_set() and time.monotonic() < deadline: page.wait_for_timeout(50)
            assert entered.is_set()
            before = remote.write_count
            page.locator(".image-page img").first.dblclick(position={"x":80,"y":100})
            page.locator(".tap-heart").wait_for()
            assert remote.write_count == before
            release.set()
            deadline = time.monotonic() + 6
            while remote.write_count == before and time.monotonic() < deadline: page.wait_for_timeout(50)
            assert remote.write_count == before + 1
            result["checks"].append("background-read-does-not-swallow-double-tap-intent")
            assert not result["errors"], result["errors"]
            context.close(); browser.close()
    except Exception:
        result["failures"].append(traceback.format_exc())
        if page:
            try: page.screenshot(path=str(out / "failure.png"))
            except Exception: pass
        raise
    finally:
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        server.should_exit=True; worker.join(15)
        if worker.is_alive(): raise RuntimeError("Isolated server failed to stop")


if __name__ == "__main__": main()
