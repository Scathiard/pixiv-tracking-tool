"""Verify the last work in an already generated synthetic large library."""
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
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    fixture = args.fixture.resolve(strict=True)
    safe_root = Path(__file__).resolve().parents[4] / ".devtools/test-runs"
    if not fixture.is_relative_to(safe_root.resolve()) or not (fixture / "data/pixiv_tracker.db").is_file():
        raise ValueError("只能复用 .devtools/test-runs 中的合成图库")
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(fixture)
    os.environ["TEMP"] = os.environ["TMP"] = str(artifacts)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    os.environ["PORT"] = str(port)
    import uvicorn
    from playwright.sync_api import sync_playwright
    from src.auth import _find_system_browser
    from src.core.runtime import Runtime
    from src.web import create_app

    runtime = Runtime(fixture / "data/pixiv_tracker.db", fixture / "images")
    total = runtime.library.query(limit=1)["total"]
    if total < 5000 or runtime.library.detail(1)["title"] != "本地作品 00001":
        runtime.close()
        raise ValueError("不是本次生成的 5000 件合成图库")
    server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True),
        host="127.0.0.1", port=port, loop="src.networking:http_loop_factory", log_level="error",
        timeout_graceful_shutdown=10, limit_concurrency=128))
    thread = threading.Thread(target=server.run, daemon=False)
    thread.start()
    checks, errors = [], []
    try:
        deadline = time.monotonic() + 20
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("隔离服务未启动")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}", wait_until="domcontentloaded")
            page.locator(".work-card").first.wait_for()
            page.get_by_label("图库浏览方式").select_option("500")
            page.get_by_role("spinbutton", name="图库页码").fill("10")
            page.get_by_role("spinbutton", name="图库页码").press("Tab")
            page.wait_for_function("document.querySelector('.gallery-viewport').getAttribute('aria-busy') === 'false' && document.querySelector('.gallery-window').dataset.start === '4500'")
            page.locator('.gallery-viewport').evaluate('(element) => element.scrollTop = element.scrollHeight')
            page.locator('.work-card[data-work-id="1"]').wait_for()
            assert page.locator(".work-card").count() < 60
            checks.append("last-work-accessible-on-page-ten-of-five-thousand")
            page.locator('.work-card[data-work-id="1"]').click()
            page.wait_for_url("**/illust/1?**")
            page.locator('.image-page[data-work-id="1"]').wait_for()
            checks.append("last-work-opens-even-with-missing-original")
            page.get_by_role("button", name="流式阅览", exact=True).click()
            page.locator('.page-viewport.feed').wait_for()
            page.locator('.image-page[data-work-id="1"]').wait_for()
            page.get_by_text("已到当前图库范围末尾", exact=True).wait_for()
            checks.append("feed-can-start-at-last-work-without-a-total-cap")
            page.go_back()
            page.wait_for_url("**/illust/1?**")
            page.go_back()
            page.locator('.work-card[data-work-id="1"]').wait_for()
            assert page.get_by_role("spinbutton", name="图库页码").input_value() == "10"
            checks.append("history-returns-to-last-gallery-page")
            page.locator('.work-card[data-work-id="1"]').click()
            page.locator('.image-page[data-work-id="1"]').wait_for()
            page.reload(wait_until="domcontentloaded")
            page.locator('.image-page[data-work-id="1"]').wait_for()
            assert "/illust/1?" in page.url
            checks.append("ordinary-position-survives-browser-reload")
            if errors:
                raise AssertionError(errors)
            page.screenshot(path=str(artifacts / "last-page.png"))
            browser.close()
    finally:
        server.should_exit = True
        thread.join(25)
        (artifacts / "result.json").write_text(json.dumps({"checks": checks, "page_errors": errors,
            "works": total, "server_stopped": not thread.is_alive(), "real_library_used": False},
            ensure_ascii=False, indent=2), encoding="utf-8")
        if thread.is_alive():
            raise RuntimeError("隔离服务未关闭")
    print(json.dumps({"checks": checks, "artifacts": str(artifacts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
