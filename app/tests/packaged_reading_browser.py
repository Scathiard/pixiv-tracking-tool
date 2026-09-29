"""Drive the packaged EXE against its own synthetic, account-free HTTP service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    exe = args.exe.resolve(strict=True)
    os.environ["DATA_ROOT"] = str(artifacts / "driver-root")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from playwright.sync_api import sync_playwright
    from src.auth import _find_system_browser

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    stop = artifacts / "stop.signal"
    package_report = artifacts / "exe-smoke.json"
    env = os.environ.copy()
    env["TEMP"] = env["TMP"] = str(artifacts)
    env["PYTHONIOENCODING"] = "utf-8"
    checks, page_errors = [], []
    output = (artifacts / "exe-output.txt").open("xb")
    process = subprocess.Popen([str(exe), "--smoke-test", "--smoke-output", str(package_report),
        "--smoke-serve-port", str(port), "--smoke-stop-file", str(stop)], cwd=artifacts,
        env=env, stdout=output, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"打包程序提前退出，退出码 {process.returncode}")
            try:
                with urlopen(origin + "/api/status", timeout=1) as response:
                    if response.status == 200:
                        break
            except (OSError, URLError):
                time.sleep(0.15)
        else:
            raise TimeoutError("打包程序未启动隔离服务")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=_find_system_browser())
            desktop = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = desktop.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(origin, wait_until="domcontentloaded")
            page.locator('.work-card[data-work-id="1"]').wait_for()
            assert '.webp' in page.locator('.card-picture img').first.get_attribute('src').split('?')[0]
            checks.append("exe-opens-new-local-gallery-and-thumbnail")
            page.get_by_role("button", name="收起侧栏", exact=True).click()
            page.locator(".app-shell.sidebar-collapsed").wait_for()
            page.get_by_role("button", name="展开侧栏", exact=True).click()
            page.get_by_role("button", name="检查新作品", exact=True).wait_for()
            checks.append("exe-b-sidebar-and-manual-check-entry")
            page.locator('.work-card[data-work-id="1"] .card-picture').click()
            page.locator('.image-page img').first.wait_for()
            page.wait_for_function("() => { const img = document.querySelector('.image-page img'); return img.complete && img.naturalWidth > 0; }")
            assert page.locator('.image-page img').first.evaluate("img => [img.naturalWidth, img.naturalHeight]") == [32, 48]
            assert page.locator('.image-page img').first.get_attribute('src').endswith('/images/1_p0.png')
            page.screenshot(path=str(artifacts / "exe-ordinary.png"))
            checks.append("exe-ordinary-reader-loads-original")
            page.get_by_role("button", name="放大此页", exact=True).first.click()
            page.get_by_role("dialog", name="放大查看").wait_for()
            page.get_by_role("button", name="关闭放大", exact=False).click()
            page.get_by_role("navigation", name="主导航").get_by_role("button", name="流式", exact=False).click()
            page.locator('.page-viewport.feed').wait_for()
            page.wait_for_function("!document.querySelector('.reader-notice')")
            page.screenshot(path=str(artifacts / "exe-feed.png"))
            checks.append("exe-zoom-and-feed-share-synthetic-library")
            page.goto(origin + "/settings", wait_until="domcontentloaded")
            page.locator(".settings-page").wait_for()
            page.get_by_text("离线，本地图库仍可阅读", exact=True).wait_for()
            checks.append("exe-real-settings-remain-usable-offline")
            from src.version import __version__, __build__
            settings = page.request.get(origin + "/api/settings").json()
            assert settings["version"] == __version__ and settings["build"] == __build__
            page.get_by_text(f"版本 {__version__} · 内部构建 {__build__}", exact=True).wait_for()
            page.mouse.move(1100, 600)
            page.mouse.wheel(0, 10000)
            page.wait_for_function("""() => {const pane=document.querySelector('.settings-page');
                const bottom=document.querySelector('.settings-version').getBoundingClientRect();
                return pane.scrollTop > 0 && bottom.bottom <= innerHeight && bottom.top > 0;}""")
            checks.append("exe-settings-bottom-reachable-by-wheel")
            page.get_by_role("button", name="查看日志", exact=True).click()
            page.locator(".settings-log pre").wait_for()
            assert page.request.get(origin + "/api/logs").ok
            checks.append("exe-public-version-build-and-read-only-log")
            for label in ("浅色", "深色", "浅色", "深色"):
                page.get_by_role("button", name=label, exact=True).click()
                assert page.locator("body").get_attribute("data-theme") == ("light" if label == "浅色" else "dark")
            page.reload()
            page.locator(".settings-page").wait_for()
            assert page.locator("body").get_attribute("data-theme") == "dark"
            checks.append("exe-theme-rapid-clicks-and-reload")
            page.goto(origin + "/artist/1")
            page.locator(".artist-profile").wait_for()
            checks.append("exe-direct-artist-route-and-local-profile")
            phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            mobile = phone.new_page()
            mobile.on("pageerror", lambda error: page_errors.append(str(error)))
            mobile.goto(origin + "/illust/1", wait_until="domcontentloaded")
            mobile.locator('.image-page img').first.wait_for()
            mobile.wait_for_function("() => { const img = document.querySelector('.image-page img'); return img.complete && img.naturalWidth > 0; }")
            assert mobile.locator('.image-page img').first.evaluate("img => [img.naturalWidth, img.naturalHeight]") == [32, 48]
            assert mobile.evaluate("document.documentElement.scrollWidth <= innerWidth")
            mobile.screenshot(path=str(artifacts / "exe-mobile.png"))
            checks.append("exe-mobile-viewport-fits-reader")
            mobile.get_by_role("button", name="打开菜单", exact=True).tap()
            mobile.get_by_role("navigation", name="主导航").get_by_role("button", name="设置", exact=True).tap()
            mobile.locator(".settings-page").wait_for()
            assert mobile.get_by_role("button", name="打开菜单", exact=True).is_visible()
            mobile.mouse.move(270, 650)
            mobile.mouse.wheel(0, 10000)
            mobile.wait_for_function("""() => {const pane=document.querySelector('.settings-page');
                const bottom=document.querySelector('.settings-version')?.getBoundingClientRect();
                return pane.scrollTop > 0 && bottom && bottom.bottom <= innerHeight && bottom.top > 50;}""")
            mobile.screenshot(path=str(artifacts / "exe-mobile-settings-bottom.png"))
            checks.append("exe-mobile-settings-scroll-container-reaches-bottom")
            checks.append("exe-mobile-drawer-navigation")
            if page_errors:
                raise AssertionError(page_errors)
            phone.close(); desktop.close(); browser.close()
    finally:
        stop.write_text("stop", encoding="utf-8")
        try:
            code = process.wait(timeout=35)
        except subprocess.TimeoutExpired:
            process.terminate()
            code = process.wait(timeout=10)
        output.close()
        (artifacts / "result.json").write_text(json.dumps({"checks": checks,
            "page_errors": page_errors, "exe_exit_code": code, "package_report": str(package_report),
            "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
            "real_account_used": False, "real_library_used": False, "real_phone_tested": False},
            ensure_ascii=False, indent=2), encoding="utf-8")
    if code != 0 or not package_report.is_file():
        raise RuntimeError(f"打包程序隔离服务退出异常，退出码 {code}")
    print(json.dumps({"checks": checks, "artifacts": str(artifacts)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
