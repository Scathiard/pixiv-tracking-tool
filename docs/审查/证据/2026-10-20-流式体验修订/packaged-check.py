"""Small final-EXE check; its smoke fixture never opens the user's library."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import socket
import subprocess
import time
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--exe", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
out = args.out.resolve()
out.mkdir(parents=True, exist_ok=False)
exe = args.exe.resolve()
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
origin = f"http://127.0.0.1:{port}"
stop = out / "stop.signal"
os.environ.update(TEMP=str(out), TMP=str(out))
result = {
    "checks": [], "errors": [], "real_account_used": False,
    "real_library_used": False, "real_phone_tested": False,
    "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
}
with (out / "output.txt").open("wb") as output:
    process = subprocess.Popen(
        [str(exe), "--smoke-test", "--smoke-output", str(out / "smoke.json"),
         "--smoke-serve-port", str(port), "--smoke-stop-file", str(stop)],
        cwd=out, stdout=output, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        for _ in range(180):
            if process.poll() is not None:
                raise RuntimeError("EXE exited before ready")
            try:
                with urlopen(origin + "/api/status", timeout=1) as response:
                    assert response.status == 200
                break
            except OSError:
                time.sleep(.5)
        else:
            raise TimeoutError("EXE not ready")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: result["errors"].append(str(error)))
            page.goto(origin, wait_until="domcontentloaded")
            page.locator(".work-card").first.wait_for()
            result["checks"].append("packaged-gallery-actual-api")
            page.locator(".work-card .card-open").first.click()
            page.wait_for_function("document.querySelector('.reader-stage img')?.naturalWidth > 0")
            assert page.locator(".stream-reader").count() == 0
            detail_id = page.url.split("/illust/")[1].split("?")[0]
            page.get_by_title("流式阅览", exact=True).click()
            expect(page.locator(".stream-reader")).to_have_attribute("data-stream-work", detail_id)
            assert page.locator(".stream-navigation").count() == 0
            result["checks"].append("packaged-current-detail-to-stream-without-navigation-buttons")
            page.locator(".stream-viewport img").first.dblclick()
            expect(page.locator(".inline-zoom-media").first).to_have_attribute("data-inline-zoom-scale", "2.25")
            assert page.locator(".zoom-overlay").count() == 0
            page.locator(".stream-viewport img").first.dblclick()
            expect(page.locator(".inline-zoom-media").first).to_have_attribute("data-inline-zoom-active", "false")
            result["checks"].append("packaged-animated-inline-zoom-and-reset")
            trigger = page.locator(".stream-caption-trigger").bounding_box()
            x, y = trigger["x"] + trigger["width"] / 2, trigger["y"] + 15
            page.mouse.move(x, y)
            page.mouse.down()
            page.mouse.move(x, y - 320, steps=12)
            page.mouse.up()
            expect(page.get_by_role("dialog", name="作品简介", exact=True)).to_be_visible()
            page.get_by_role("button", name="关闭作品简介", exact=True).click()
            result["checks"].append("packaged-caption-drag")
            page.keyboard.press("ArrowRight")
            page.wait_for_function(
                "old => document.querySelector('.stream-reader')?.dataset.streamWork !== old",
                arg=detail_id,
            )
            result["checks"].append("packaged-stream-keyboard-next")
            for width in (390, 320):
                page.set_viewport_size({"width": width, "height": 844})
                page.wait_for_timeout(250)
                geometry = page.evaluate("""() => ({
                    width: innerWidth, height: innerHeight,
                    overflow: document.documentElement.scrollWidth > innerWidth,
                    dock: (() => {const r=document.querySelector('.stream-dock').getBoundingClientRect();
                        return {left:r.left,right:r.right,bottom:r.bottom}})()
                })""")
                assert not geometry["overflow"] and geometry["dock"]["bottom"] <= 845
                assert geometry["dock"]["left"] >= 0 and geometry["dock"]["right"] <= width + 1
                page.screenshot(path=str(out / f"packaged-stream-{width}.png"))
            result["checks"].append("packaged-390-and-320-geometry")
            with urlopen(origin + "/api/artists?compact=1&limit=100", timeout=10) as response:
                artists = json.load(response)
                assert artists["items"] and "recent_works" not in artists["items"][0]
            with urlopen(origin + "/api/library/tags?show_hidden=1", timeout=10) as response:
                tags = json.load(response)
                assert isinstance(tags["tags"], list) and isinstance(tags["tag_details"], list)
            result["checks"].append("packaged-compact-filter-contract")
            browser.close()
    except Exception as error:
        result["errors"].append(str(error))
        raise
    finally:
        stop.write_text("stop", encoding="utf-8")
        process.wait(timeout=25)
        result["exit_code"] = process.returncode
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
assert process.returncode == 0 and not result["errors"], result
assert json.loads((out / "smoke.json").read_text(encoding="utf-8"))["status"] == "SMOKE_OK"
print(json.dumps(result, ensure_ascii=False))
