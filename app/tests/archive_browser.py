"""Run with --artifacts NEW_DIRECTORY; no real account, config or library is opened."""
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
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    root = artifacts / "fixture"
    os.environ["DATA_ROOT"] = str(root)
    os.environ["TEMP"] = str(artifacts)
    os.environ["TMP"] = str(artifacts)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    os.environ["PORT"] = str(port)
    import uvicorn
    from PIL import Image, ImageDraw
    from playwright.sync_api import sync_playwright
    from src.auth import _find_system_browser
    from src.core.models import ArchiveIntent, Artist, Asset, TrackingPolicy, Work, WorkTag
    from src.core.runtime import Runtime
    from src.web import create_app
    from src import config
    config.PAGE_SIZE = 50
    config.PAGINATION_MODE = "paginate"
    runtime = Runtime(root / "data/pixiv_tracker.db", root / "images", create=True)
    with runtime.database.write() as session:
        artist = Artist(id=1, pixiv_user_id="1", name="隔离测试画师")
        artist.policy = TrackingPolicy(mode="paused")
        session.add(artist)
        for index in range(1, 61):
            page_count = 3 if index % 5 == 0 else 1
            work = Work(id=index, pixiv_illust_id=str(index + 100), artist=artist,
                        title=f"测试作品 {index}", type="manga" if page_count > 1 else "illust", page_count=page_count,
                        caption="<p>仅使用程序生成的测试图片。</p>", caption_state="available")
            work.tags = [WorkTag(tag="隔离测试")]
            for page in range(page_count):
                path = f"{index + 100}_p{page}.png"
                image = Image.new("RGB", (640, 860), (index * 17 % 190 + 25, 60 + page * 35, 115))
                drawing = ImageDraw.Draw(image)
                drawing.rectangle((60, 60, 580, 800), outline="white", width=4)
                drawing.text((90, 100), f"WORK {index} / PAGE {page + 1}", fill="white", font_size=28)
                image.save(root / "images" / path)
                image.close()
                work.assets.append(Asset(role="image", page_index=page, relative_path=path))
            session.add_all([work, ArchiveIntent(pixiv_illust_id=str(index + 100))])
    app = create_app(lambda: runtime, authenticate=False, schedule=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=False)
    thread.start()
    failures, checks = [], []
    browser = None
    try:
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("隔离服务启动失败")
        with sync_playwright() as playwright:
            browser_path = _find_system_browser()
            browser = playwright.chromium.launch(headless=True, executable_path=browser_path) if browser_path else playwright.chromium.launch(headless=True)
            desktop = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = desktop.new_page()
            page.on("pageerror", lambda error: failures.append(str(error)))
            response = page.goto(f"http://localhost:{port}/", wait_until="domcontentloaded")
            assert response.status == 200
            assert page.locator(".illust-card").count() == 50
            assert page.get_by_text("登录 Pixiv", exact=True).is_visible()
            checks.append("offline-gallery-and-pagination")
            page.locator(".illust-card a").first.click()
            page.wait_for_selector(".page-image")
            assert page.locator(".page-image").count() == 3
            page.evaluate("window.archiveReadingMarker = 'keep'")
            page.evaluate("window.scrollTo(0, 420)")
            reading_url = page.url
            manager = desktop.new_page()
            manager.on("pageerror", lambda error: failures.append(str(error)))
            manager.goto(f"http://localhost:{port}/settings", wait_until="domcontentloaded")
            manager.get_by_role("button", name="核验本地存档", exact=True).click()
            manager.get_by_text("已排队 · 查看任务", exact=True).wait_for()
            page.wait_for_function("document.getElementById('prog-phase').textContent === '无需处理'", timeout=60000)
            assert page.url == reading_url and page.evaluate("window.archiveReadingMarker") == "keep"
            checks.append("background-completion-preserves-reader-document")
            manager.get_by_text("已排队 · 查看任务", exact=True).click()
            manager.locator("#progress-box details[open]").wait_for()
            checks.append("submission-opens-own-persistent-record")
            manager.request.post(f"http://localhost:{port}/api/tasks", data={"kind": "thumbnails"}, headers={"Origin": f"http://localhost:{port}"})
            page.wait_for_function("Number(document.querySelector('[role=progressbar]').getAttribute('aria-valuenow')) > 0 && Number(document.querySelector('[role=progressbar]').getAttribute('aria-valuenow')) < 100", timeout=60000)
            manager.request.post(f"http://localhost:{port}/api/tasks", data={"kind": "verify"}, headers={"Origin": f"http://localhost:{port}"})
            page.get_by_text("下一轮等待", exact=False).wait_for(timeout=15000)
            checks.append("real-manifest-percentage-and-next-round")
            page.wait_for_function("document.getElementById('prog-phase').textContent === '无需处理'", timeout=120000)
            assert page.url == reading_url and page.evaluate("window.archiveReadingMarker") == "keep"
            page.get_by_role("button", name="隐藏", exact=True).click()
            page.get_by_role("button", name="永久删除", exact=True).wait_for()
            page.get_by_role("button", name="取消隐藏", exact=True).click()
            checks.append("local-controls-update-without-refresh")
            page.screenshot(path=str(artifacts / "desktop-reader.png"), full_page=False)
            manager.goto(f"http://localhost:{port}/artists", wait_until="domcontentloaded")
            manager.get_by_role("button", name="解除暂停同步", exact=True).click()
            manager.get_by_text("当前已启动批次继续完成；新设置对下一次自动检查生效", exact=True).wait_for()
            manager.once("dialog", lambda dialog: dialog.accept())
            manager.get_by_role("button", name="停止追踪", exact=True).click()
            manager.get_by_text("已停止追踪，画师与已有作品保留在图库", exact=True).wait_for()
            assert manager.request.get(f"http://localhost:{port}/api/library?artist_ids=1").json()["total"] == 60
            checks.append("stop-tracking-retains-gallery")
            mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=1)
            phone = mobile.new_page()
            phone.on("pageerror", lambda error: failures.append(str(error)))
            phone.goto(reading_url, wait_until="domcontentloaded")
            phone.wait_for_selector(".page-image")
            assert phone.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            phone.screenshot(path=str(artifacts / "mobile-reader.png"), full_page=False)
            checks.append("mobile-sized-reading-and-feedback-layout")
            # Drop only this context's status responses, leaving image requests intact.
            phone.route("**/api/progress", lambda route: route.abort())
            phone.get_by_text("暂时无法获取状态", exact=True).wait_for(timeout=10000)
            phone.unroute("**/api/progress")
            phone.wait_for_function("document.getElementById('prog-phase').textContent !== '暂时无法获取状态'", timeout=10000)
            checks.append("status-disconnect-and-reconnect")
            if failures:
                raise AssertionError(failures)
            mobile.close()
            desktop.close()
            browser.close()
            browser = None
    finally:
        server.should_exit = True
        thread.join(15)
        (artifacts / "result.json").write_text(json.dumps({"checks": checks, "page_errors": failures,
            "server_stopped": not thread.is_alive(), "real_phone_tested": False}, ensure_ascii=False, indent=2), encoding="utf-8")
        if thread.is_alive():
            raise RuntimeError("本任务的隔离服务尚未停止")
    print(json.dumps({"passed": checks, "artifacts": str(artifacts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
