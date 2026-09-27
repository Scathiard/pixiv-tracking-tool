"""Large local client under a controlled download; no real account or library."""
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
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "fixture")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    os.environ["PORT"] = str(port)
    from client_fixture import make_library
    from playwright.sync_api import sync_playwright
    from sqlalchemy import select
    import uvicorn
    from src.core.models import Artist, Asset, TrackingPolicy
    runtime, remote = make_library(artifacts / "fixture", 5000)
    from src.auth import _find_system_browser
    from src.web import create_app

    class Download:
        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()
            self.calls = []

        def download(self, url, destination, report=None):
            self.calls.append(url)
            payload = (runtime.images / "fixture-0.png").read_bytes()
            with destination.open("xb") as output:
                middle = len(payload) // 2
                output.write(payload[:middle])
                if report:
                    report(middle, len(payload))
                self.started.set()
                if not self.release.wait(180):
                    raise TimeoutError("isolated download gate was not released")
                if url.endswith("/reject.png"):
                    raise OSError("隔离测试：磁盘写入失败")
                output.write(payload[middle:])
                if report:
                    report(len(payload), len(payload))

    download = Download()
    for handler in runtime.queue.handlers.values():
        handler.downloader = download
    # Preserve the original fixture files; only these synthetic DB entries point
    # at new, absent destinations so the real archive worker exercises publishing.
    with runtime.database.write() as session:
        artist = Artist(id=13, pixiv_user_id="13", name="自动检查隔离样本")
        artist.policy = TrackingPolicy(mode="active")
        session.add(artist)
        for asset in session.scalars(select(Asset).where(Asset.work_id.between(2, 21), Asset.page_index == 0)):
            asset.relative_path = f"missing/{asset.work_id}.png"
            asset.state = "missing"
            asset.source_url = "https://i.pximg.net/" + ("reject.png" if asset.work_id == 20 else f"{asset.work_id}.png")
    app = create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
        loop="src.networking:http_loop_factory", log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{port}"
    checks, errors, samples = [], [], []
    metrics = {"works": 5000, "long_work_pages": 2000, "stream_samples": samples,
        "frontend_index_sha256": hashlib.sha256((Path(__file__).resolve().parents[1] / "frontend/dist/index.html").read_bytes()).hexdigest()}
    browser = playwright = page = None

    def passed(label):
        checks.append(label)
        print(label, flush=True)

    def ready():
        page.locator(".image-page img").first.wait_for()
        page.wait_for_function("!document.querySelector('.reader-notice')")

    try:
        deadline = time.monotonic() + 25
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(origin + "/illust/4999")
        ready()
        page.evaluate("window.documentMarker='preserved'")
        blocked = "**/images/104998_p0.png"
        page.route(blocked, lambda route: route.abort())
        page.keyboard.press("ArrowRight")
        page.get_by_role("alert").get_by_text("目标图片未能载入，当前画面已保留", exact=False).wait_for()
        assert "/illust/4999" in page.url
        page.unroute(blocked)
        page.get_by_role("alert").get_by_role("button", name="重试", exact=True).click()
        page.wait_for_url("**/illust/4998*")
        assert page.evaluate("window.documentMarker") == "preserved"
        passed("failed-target-retains-current-work-and-retries-without-refresh")

        page.get_by_role("navigation", name="主导航").get_by_role("button", name="设置", exact=True).click()
        page.get_by_label("图库浏览方式").select_option("100")
        page.get_by_role("button", name="返回我的图库", exact=True).click()
        pager = page.get_by_role("spinbutton", name="图库页码")
        page.wait_for_function("document.querySelector('.gallery-viewport')?.getAttribute('aria-busy') === 'false' && document.querySelector('input[aria-label=\"图库页码\"]')?.max === '50'")
        pager.fill("50")
        pager.press("Tab")
        page.wait_for_function("document.querySelector('.gallery-window')?.dataset.start === '4900'")
        page.locator(".gallery-viewport").evaluate("e=>e.scrollTop=e.scrollHeight")
        page.locator(".work-card[data-work-id='1'] .card-picture").click()
        page.wait_for_url("**/illust/1*")
        page.get_by_role("navigation", name="主导航").get_by_role("button", name="流式", exact=False).click()
        ready()
        page.get_by_text("已到当前图库范围末尾", exact=True).wait_for()
        page.go_back()
        page.wait_for_url("**/illust/1*")
        page.get_by_role("button", name="返回我的图库", exact=True).click()
        page.locator(".work-card[data-work-id='1']").wait_for()
        assert page.get_by_role("spinbutton", name="图库页码").input_value() == "50"
        passed("five-thousandth-work-and-feed-end-accessible-with-restored-page")

        page.goto(origin + "/illust/5000")
        ready()
        page.get_by_role("navigation", name="主导航").get_by_role("button", name="流式", exact=False).click()
        # A fresh origin-wide feed can resume its previous end, so reload its
        # initial anchor from the newly opened 2000-page work in a fresh context.
        context.close()
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(15000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        image_requests = []
        page.on("request", lambda request: image_requests.append(request.url) if "/images/" in request.url else None)
        page.goto(origin + "/flow")
        ready()
        page.evaluate("window.documentMarker='download-preserved'")
        cdp = context.new_cdp_session(page)

        def value(expression):
            return cdp.send("Runtime.evaluate", {"expression": expression, "returnByValue": True})["result"].get("value")

        def wait_value(expression):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if value(expression):
                    return
                time.sleep(.025)
            raise TimeoutError(expression)

        operation_id = next_id = None
        frozen_total = None
        for step in range(60):
            if step == 12:
                response = context.request.post(origin + "/api/tasks", headers={"Origin": origin}, data={"kind": "repair", "work_ids": list(range(2, 22))})
                assert response.status == 200, response.text()
                operation_id = response.json()["operation_id"]
                assert download.started.wait(30)
                current = runtime.queue.snapshot()["round"]
                frozen_total = current["total"]
                assert frozen_total and frozen_total > 0
                page.get_by_role("progressbar", name="本轮处理进度").wait_for()
                page.wait_for_function("document.querySelector('[role=progressbar]')?.hasAttribute('aria-valuenow')")
                response = context.request.post(origin + "/api/tasks", headers={"Origin": origin}, data={"kind": "verify", "work_ids": [4999]})
                next_id = response.json()["operation_id"]
                assert runtime.queue.snapshot()["next_round_count"] == 1
                assert runtime.queue.snapshot()["round"]["total"] == frozen_total
                page.screenshot(path=str(artifacts / "active-download.png"))
            previous = value("Math.max(...[...document.querySelectorAll('.image-page')].map(n=>Number(n.dataset.globalIndex)))")
            value("(()=>{const e=document.querySelector('.page-viewport');e.scrollTop=e.scrollHeight-e.clientHeight-2;return true})()")
            # Playwright's repeated selector waits retained measured DOM in the
            # probe itself. Return primitive values through CDP for memory tests.
            wait_value(f"Math.max(...[...document.querySelectorAll('.image-page')].map(n=>Number(n.dataset.globalIndex)))>{previous} && !document.querySelector('.reader-notice')")
            if step % 5 == 4:
                cdp.send("HeapProfiler.collectGarbage")
                rendered = value("({images:document.querySelectorAll('.image-page img').length,pages:Number(document.querySelector('.page-window').dataset.windowSize)})")
                sample = {"step": step + 1, "heap_bytes": cdp.send("Runtime.getHeapUsage")["usedSize"],
                    "nodes": cdp.send("Memory.getDOMCounters")["nodes"], "images": rendered["images"],
                    "window_pages": rendered["pages"],
                    "image_requests": len(image_requests), "downloading": download.started.is_set()}
                samples.append(sample)
                assert sample["nodes"] < 2500 and sample["window_pages"] <= 120 and sample["images"] < 16
        assert samples[-1]["heap_bytes"] < samples[0]["heap_bytes"] * 1.8 + 8 * 1024 ** 2
        assert samples[-1]["nodes"] <= samples[2]["nodes"] + 200, "detached reading nodes keep accumulating"
        assert any(sample["downloading"] for sample in samples)
        assert runtime.queue.snapshot()["round"]["total"] == frozen_total
        passed("large-feed-bounded-during-download-and-next-round-keeps-fixed-total")

        download.release.set()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if all(runtime.queue.details(item)["state"] == "finished" for item in (operation_id, next_id)):
                break
            page.wait_for_timeout(200)
        result = runtime.queue.details(operation_id)
        assert result["state"] == "finished" and result["result"]["outcome"] == "partial_failure"
        metrics["download_result"] = {"counts": result["counts"], "outcome": result["result"]["outcome"], "calls": len(download.calls)}
        page.get_by_role("progressbar", name="本轮处理进度").wait_for(state="hidden")
        assert page.evaluate("window.documentMarker") == "download-preserved"
        passed("partial-failure-persists-progress-retracts-without-reader-refresh")

        response = context.request.patch(origin + "/api/settings", headers={"Origin": origin}, data={"automatic_paused": True})
        assert response.status == 200
        automatic = runtime.queue.submit("check", {"artist_ids": [13]}, "隔离自动检查", source="automatic")
        page.get_by_text("等待恢复自动检查", exact=False).wait_for()
        assert not page.get_by_role("progressbar").is_visible()
        assert runtime.queue.details(automatic["operation_id"])["state"] == "queued"
        passed("paused-automatic-queue-not-labelled-running-or-old-completion")
        response = context.request.patch(origin + "/api/settings", headers={"Origin": origin}, data={"automatic_paused": False})
        assert response.status == 200
        deadline = time.monotonic() + 30
        while runtime.queue.details(automatic["operation_id"])["state"] != "finished" and time.monotonic() < deadline:
            page.wait_for_timeout(150)
        assert runtime.queue.details(automatic["operation_id"])["state"] == "finished"
        page.get_by_role("navigation", name="主导航").get_by_role("button", name="设置", exact=True).click()
        page.get_by_role("button", name="任务记录", exact=True).click()
        page.locator(".task-list").get_by_text("部分失败", exact=True).wait_for()
        page.screenshot(path=str(artifacts / "retained-failure.png"))
        passed("settings-task-history-retains-earlier-failed-round")

        runtime.client = None
        page.reload()
        page.get_by_text("离线，本地图库仍可阅读", exact=True).wait_for()
        page.get_by_role("button", name="返回我的图库", exact=True).click()
        page.locator(".work-card").first.wait_for()
        page.get_by_label("搜索作品、画师或标签").fill("本地作品 04999")
        page.locator(".work-card[data-work-id='4999'] .card-picture").click()
        ready()
        assert "/illust/4999" in page.url
        passed("offline-settings-search-and-local-reading-remain-usable")
        if errors:
            raise AssertionError(errors)
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
        download.release.set()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        server.should_exit = True
        thread.join(30)
        (artifacts / "result.json").write_text(json.dumps({"checks": checks, "errors": errors, "metrics": metrics,
            "real_account_used": False, "real_library_used": False, "real_phone_tested": False,
            "server_stopped": not thread.is_alive()}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
