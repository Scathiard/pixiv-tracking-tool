"""运行：python tests/browser_smoke.py。退出时保留临时夹具目录供核对。"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    data_root = Path(tempfile.mkdtemp(prefix="pixiv-tracker-browser-"))
    os.environ["DATA_ROOT"] = str(data_root)
    port = _free_port()
    os.environ["PORT"] = str(port)

    import uvicorn
    from playwright.sync_api import sync_playwright
    from src.auth import _find_system_browser
    from browser_fixture import build_app

    server = uvicorn.Server(uvicorn.Config(
        build_app(), host="127.0.0.1", port=port, log_level="error"
    ))
    thread = threading.Thread(target=server.run, daemon=False)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("浏览器夹具服务启动超时")

    browser_path = _find_system_browser()
    if not browser_path:
        raise RuntimeError("未找到 Chrome/Edge")
    page_errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=browser_path
            )
            desktop = browser.new_context(viewport={"width": 1440, "height": 900})
            page = desktop.new_page()
            page.on(
                "pageerror",
                lambda error: page_errors.append(error.stack or str(error)),
            )
            response = page.goto(f"http://localhost:{port}/", wait_until="networkidle")
            assert response.status == 200
            assert response.headers["x-content-type-options"] == "nosniff"
            assert page.locator(".illust-card").count() == 24

            page.goto(f"http://localhost:{port}/?types=manga", wait_until="networkidle")
            assert page.locator(".illust-card").count() == 6
            page.locator(".illust-card a").first.click()
            page.wait_for_load_state("networkidle")
            assert page.locator(".detail-header").count() == 1
            assert "types=manga" in page.locator(".back-link").get_attribute("href")

            blocked = page.request.post(
                f"http://localhost:{port}/settings/check-now",
                headers={"Origin": "https://evil.example"},
            )
            assert blocked.status == 403

            page.goto(f"http://localhost:{port}/settings", wait_until="networkidle")
            assert page.get_by_role("heading", name="访问地址").count() == 1
            assert page.locator(
                f'a[href="http://localhost:{port}"] > code'
            ).count() == 1

            page.goto(f"http://localhost:{port}/artists", wait_until="networkidle")
            assert page.locator(".artist-card").count() == 2
            assert page.get_by_text("12 件作品", exact=False).count() == 2
            desktop.close()

            mobile = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
            )
            mobile_page = mobile.new_page()
            mobile_page.on(
                "pageerror",
                lambda error: page_errors.append(error.stack or str(error)),
            )
            mobile_page.goto(f"http://localhost:{port}/", wait_until="networkidle")
            overflow = mobile_page.evaluate(
                "document.documentElement.scrollWidth > document.documentElement.clientWidth"
            )
            assert not overflow
            assert mobile_page.locator(".illust-card").count() == 24
            mobile.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    if page_errors:
        raise AssertionError(f"浏览器页面错误: {page_errors}")
    print(f"BROWSER_SMOKE_OK data={data_root} port={port}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
