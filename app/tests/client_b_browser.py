"""B interface acceptance with an isolated library and synthetic task responses."""
from __future__ import annotations

import argparse
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
    parser.add_argument("--works", type=int, default=500)
    parser.add_argument("--remaining-only", action="store_true", help="Only check direct settings to flow and phone drawers")
    parser.add_argument("--expected-js", help="Assert the named built JavaScript asset is loaded")
    args = parser.parse_args()
    if args.works < 100:
        parser.error("--works must be at least 100 for the search and anchor checks")
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    fixture = artifacts / "fixture"
    os.environ["DATA_ROOT"] = str(fixture)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    os.environ["PORT"] = str(port)

    from playwright.sync_api import expect, sync_playwright
    import uvicorn
    from client_fixture import make_library
    from src.core.models import Operation, OperationItem

    started = time.monotonic()
    runtime, remote = make_library(fixture, args.works)
    from src.auth import _find_system_browser
    from src.web import create_app
    with runtime.database.write() as session:
        operation = Operation(kind="verify", label="synthetic-check", dedupe_key="browser-log",
                              state="finished", phase="verifying", phase_unit="files",
                              phase_done=1, phase_total=2, result={"outcome": "partial_failure"},
                              needs_attention=True)
        session.add(operation)
        session.flush()
        session.add(OperationItem(operation_id=operation.id, item_key="synthetic-missing",
                                  kind="test", target={}, phase="verifying", state="failed",
                                  result={"message": "原件缺失或损坏，可选择补下载"}))

    app = create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
        log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{port}"
    checks, errors, metrics = [], [], {"works": args.works}
    metrics["frontend_index_sha256"] = hashlib.sha256(
        (Path(__file__).resolve().parents[1] / "frontend/dist/index.html").read_bytes()).hexdigest()
    playwright = browser = page = None

    def passed(label):
        checks.append(label)
        print(label, flush=True)

    def anchor_visible(target, work_id):
        return target.evaluate("""id => {
          const card = document.querySelector(`.work-card[data-work-id='${id}']`);
          const viewport = document.querySelector('.gallery-viewport');
          if (!card || !viewport) return false;
          const item = card.getBoundingClientRect(), area = viewport.getBoundingClientRect();
          return item.bottom > area.top + 8 && item.top < area.bottom - 8;
        }""", work_id)

    def snapshot(state, total=None, done=0):
        if state == "idle":
            return {"round": None, "operations": [], "worker_error": None, "automatic_paused": False}
        phase = {"name": "verifying", "done": done, "total": total,
                 "unit": "files", "detail": "synthetic phase"}
        op = {"id": "synthetic-ui-check", "label": "检查新作品", "state": state,
              "source": "manual", "phase": phase,
              "result": {"outcome": "succeeded" if state == "finished" else None}}
        return {"round": {"id": "synthetic-ui-round",
                           "state": "finished" if state == "finished" else "running"},
                "operations": [op], "worker_error": None, "automatic_paused": False}

    def check_remaining():
        direct = browser.new_context(viewport={"width": 1280, "height": 800})
        direct_page = direct.new_page()
        direct_page.on("pageerror", lambda error: errors.append(str(error)))
        direct_page.goto(origin + "/settings")
        direct_page.locator(".settings-page").wait_for()
        scripts = direct_page.locator("script[src]").evaluate_all(
            "elements => elements.map(element => element.getAttribute('src'))")
        metrics["direct_settings_scripts"] = scripts
        if args.expected_js:
            assert any(args.expected_js in script for script in scripts), f"expected {args.expected_js}; loaded {scripts}"
        direct_page.get_by_role("navigation", name="主导航").get_by_role("button", name="流式阅览").click()
        direct_page.wait_for_url("**/flow*")
        direct_page.locator(".page-viewport.feed .image-page").first.wait_for()
        direct_page.screenshot(path=str(artifacts / "direct-settings-to-flow.png"))
        direct.close()
        passed("direct-settings-route-can-enter-flow")

        for width in (390, 320):
            phone = browser.new_context(viewport={"width": width, "height": 844}, is_mobile=True, has_touch=True)
            mobile = phone.new_page()
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            mobile.goto(origin)
            mobile.locator(".work-card").first.wait_for()
            mobile.get_by_role("button", name="打开菜单").click()
            expect(mobile.locator(".app-header")).to_have_class("app-header is-open")
            assert mobile.locator(".gallery-layout").evaluate("element => element.inert")
            mobile.keyboard.press("Escape")
            expect(mobile.locator(".app-header")).to_have_class("app-header")
            assert not mobile.locator(".gallery-layout").evaluate("element => element.inert")
            mobile.get_by_role("button", name="打开菜单").click()
            mobile.locator(".sidebar-scrim").click(position={"x": width - 8, "y": 200})
            expect(mobile.locator(".app-header")).to_have_class("app-header")
            mobile.get_by_role("button", name="打开菜单").click()
            mobile.get_by_label("搜索作品、画师或标签").fill("本地作品 00035")
            mobile.keyboard.press("Escape")
            mobile.locator(".work-card[data-work-id='35']").wait_for()
            expect(mobile.locator(".work-card")).to_have_count(1)
            assert mobile.evaluate("document.documentElement.scrollWidth <= innerWidth")
            mobile.screenshot(path=str(artifacts / f"phone-{width}-search.png"))
            phone.close()
            passed(f"mobile-{width}-drawer-escape-scrim-inert-search")

    try:
        deadline = time.monotonic() + 25
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        if not server.started:
            raise RuntimeError("isolated server did not start")
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
        desktop = browser.new_context(viewport={"width": 1440, "height": 900})
        page = desktop.new_page()
        page.set_default_timeout(12000)
        page.on("pageerror", lambda error: errors.append(str(error)))

        progress = {"state": "idle", "total": None, "done": 0}
        submissions = []

        def mock_progress(route):
            if progress["state"] == "stale":
                route.abort("failed")
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(snapshot(progress["state"], progress["total"], progress["done"])))

        def mock_submission(route):
            body = route.request.post_data_json or {}
            if body.get("kind") == "check":
                submissions.append(body)
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"operation_id": "synthetic-ui-check", "created": True}))
            else:
                route.continue_()

        page.route("**/api/progress", mock_progress)
        page.route("**/api/tasks", mock_submission)
        if args.remaining_only:
            check_remaining()
            if errors:
                raise AssertionError(errors)
            desktop.close()
            browser.close()
            browser = None
            return
        page.goto(origin)
        page.locator(".work-card").first.wait_for()
        expect(page.locator(".task-status")).to_have_count(0)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator(".gallery-viewport").evaluate("element => element.scrollTop = 680")
        page.wait_for_timeout(250)
        anchor = page.evaluate("""() => {
          const area = document.querySelector('.gallery-viewport').getBoundingClientRect();
          return [...document.querySelectorAll('.work-card')]
            .find(card => { const box = card.getBoundingClientRect();
              return box.top > area.top + 35 && box.bottom < area.bottom - 35; })?.dataset.workId;
        }""")
        assert anchor, "no visible gallery anchor after scrolling"
        page.get_by_role("button", name="收起侧栏").click()
        expect(page.locator(".app-shell")).to_have_class("app-shell sidebar-collapsed")
        page.wait_for_timeout(250)
        page.wait_for_function("id => { const c=document.querySelector(`.work-card[data-work-id='${id}']`), v=document.querySelector('.gallery-viewport'); if(!c||!v)return false; const a=c.getBoundingClientRect(),b=v.getBoundingClientRect();return a.bottom>b.top+8&&a.top<b.bottom-8 }", arg=anchor)
        page.get_by_role("button", name="展开侧栏", exact=True).click()
        page.wait_for_timeout(250)
        page.wait_for_function("id => { const c=document.querySelector(`.work-card[data-work-id='${id}']`), v=document.querySelector('.gallery-viewport'); if(!c||!v)return false; const a=c.getBoundingClientRect(),b=v.getBoundingClientRect();return a.bottom>b.top+8&&a.top<b.bottom-8 }", arg=anchor)
        assert anchor_visible(page, anchor)
        page.screenshot(path=str(artifacts / "desktop-sidebar.png"))
        passed("desktop-sidebar-collapse-expand-preserves-work-anchor")

        viewport_before = page.locator(".gallery-viewport").bounding_box()
        progress.update(state="queued", total=None, done=0)
        page.get_by_role("button", name="检查新作品").click()
        expect(page.locator(".task-status")).to_contain_text("已排队，等待处理")
        expect(page.locator(".toast")).to_contain_text("检查新作品已提交")
        assert submissions == [{"kind": "check"}], "homepage check must submit kind=check"
        assert not page.locator(".toast").get_attribute("class").endswith(" error")
        expect(page.locator(".toast")).to_be_hidden(timeout=6500)
        passed("homepage-check-submission-and-success-notice-expires")

        progress.update(state="running", total=100, done=20)
        expect(page.locator(".task-status")).to_contain_text("核验 · 20 / 100 个文件")
        track = page.get_by_role("progressbar")
        expect(track).to_have_attribute("aria-valuenow", "20")
        expect(track).to_have_attribute("aria-valuemax", "100")
        assert page.locator(".task-status").evaluate("element => getComputedStyle(element).position") == "fixed"
        viewport_after = page.locator(".gallery-viewport").bounding_box()
        assert all(abs(viewport_before[key] - viewport_after[key]) < 1 for key in ("x", "y", "width", "height"))
        assert "%" not in page.locator(".task-status").inner_text()
        progress.update(state="running", total=None, done=20)
        expect(page.locator(".task-status")).to_contain_text("已处理 20 个文件")
        assert page.get_by_role("progressbar").get_attribute("aria-valuemax") is None
        progress.update(state="running", total=0, done=0)
        expect(page.locator(".task-status")).to_contain_text("0 / 0 个文件")
        expect(page.get_by_role("progressbar")).to_have_count(0)
        progress.update(state="stale")
        expect(page.locator(".task-status")).to_contain_text("状态暂不可得")
        expect(page.get_by_role("progressbar")).to_have_count(0)
        progress.update(state="finished", total=100, done=100)
        expect(page.locator(".task-status")).to_have_count(0)
        progress.update(state="idle", total=None, done=0)
        page.screenshot(path=str(artifacts / "desktop-progress-finished.png"))
        passed("phase-counts-unknown-zero-stale-fixed-overlay-and-idle-hide")

        page.get_by_role("navigation", name="主导航").get_by_role("button", name="设置").click()
        page.locator(".settings-page").wait_for()
        expect(page.locator(".settings-version")).to_contain_text("版本 0.2.0 · 内部构建 20260929.1")
        expect(page.get_by_role("button", name="全量核验本地文件")).to_have_count(1)
        expect(page.locator(".settings-page")).to_contain_text("不会自动补下载")
        expect(page.get_by_role("button", name="任务记录")).to_have_count(0)
        page.get_by_role("button", name="查看日志").click()
        expect(page.locator(".settings-log pre")).to_contain_text("原件缺失或损坏，可选择补下载")
        expect(page.locator(".settings-log pre")).to_contain_text("部分失败")
        page.screenshot(path=str(artifacts / "desktop-settings-log.png"))
        passed("settings-version-and-readonly-synthetic-failure-log")

        check_remaining()

        if errors:
            raise AssertionError(errors)
        desktop.close()
        browser.close()
        browser = None
    except Exception:
        errors.append(traceback.format_exc())
        if page is not None:
            try:
                page.screenshot(path=str(artifacts / "failure.png"))
                (artifacts / "failure.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
        raise
    finally:
        remote.release.set()
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if playwright is not None:
            playwright.stop()
        server.should_exit = True
        thread.join(20)
        metrics["elapsed_seconds"] = round(time.monotonic() - started, 2)
        (artifacts / "result.json").write_text(json.dumps({"checks": checks, "errors": errors,
            "metrics": metrics, "real_account_used": False, "real_library_used": False,
            "real_phone_tested": False}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
