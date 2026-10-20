"""R-073: one account-free EXE lifecycle plus focused real-component replay."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    exe = args.exe.resolve(strict=True)
    env = dict(os.environ, TEMP=str(artifacts), TMP=str(artifacts), PYTHONIOENCODING="utf-8")
    os.environ.update(TEMP=str(artifacts), TMP=str(artifacts))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    stop, smoke = artifacts / "stop.signal", artifacts / "exe-smoke.json"
    result = {"checks": [], "errors": [], "real_library_used": False, "real_account_used": False,
              "real_phone_tested": False}
    with (artifacts / "exe-output.txt").open("xb") as output:
        process = subprocess.Popen([str(exe), "--smoke-test", "--smoke-output", str(smoke),
            "--smoke-serve-port", str(port), "--smoke-stop-file", str(stop)], cwd=artifacts,
            env=env, stdout=output, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"EXE stopped before ready: {process.returncode}")
                try:
                    with urlopen(origin + "/api/status", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(.15)
            else:
                raise TimeoutError("Isolated EXE not ready")
            from playwright.sync_api import expect, sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True,
                    executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe")
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 900})
                    page.on("pageerror", lambda error: result["errors"].append(str(error)))
                    page.goto(origin, wait_until="domcontentloaded")
                    page.locator('.work-card[data-work-id="2"] .card-picture').click()
                    detail = page.request.get(origin + "/api/works/2").json()
                    expect(page.locator(".detail-title-row h1")).to_have_text(detail["title"])
                    page.wait_for_function("""() => {
                        const image = document.querySelector('.image-page img');
                        return image?.complete && image.naturalWidth > 0;
                    }""")
                    nearby = page.request.get(origin + "/api/works/2/cursor-neighbors").json()
                    assert nearby["previous"] and nearby["next"]
                    next_work = page.request.get(origin + f"/api/works/{nearby['next']['work_id']}").json()
                    page.locator(".reading-toolbar").get_by_role("button", name="下一件作品", exact=True).click()
                    expect(page.locator(".detail-title-row h1")).to_have_text(next_work["title"])
                    page.locator(".reading-toolbar").get_by_role("button", name="上一件作品", exact=True).click()
                    expect(page.locator(".detail-title-row h1")).to_have_text(detail["title"])
                    result["checks"].append("Final EXE: real synthetic HTTP gallery -> detail -> next -> previous")
                    page.screenshot(path=str(artifacts / "actual-exe-reader.png"))
                finally:
                    browser.close()
            replay = Path(__file__).resolve().parents[1] / "frontend/tests/reading-interactions-browser.py"
            with (artifacts / "browser-output.txt").open("xb") as output:
                subprocess.run([sys.executable, "-B", str(replay), "--url", origin, "--bundle",
                    "--artifacts", str(artifacts / "browser")], check=True, env=env,
                    stdout=output, stderr=subprocess.STDOUT)
            result["checks"].append("Final EXE bundle: controlled slow/failing resources, cursor changes and bookmark click")
            assert not result["errors"], result["errors"]
        finally:
            stop.write_text("stop", encoding="utf-8")
            try:
                result["exit_code"] = process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                # This process is the fixture we created above, never a daily instance.
                process.terminate()
                result["exit_code"] = process.wait(timeout=10)
            (artifacts / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8")
    assert result["exit_code"] == 0 and smoke.is_file()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
