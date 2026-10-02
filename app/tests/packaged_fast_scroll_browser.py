"""Verify final EXE frontend with three synthetic DB works and 800 browser-only clones."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import traceback
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    exe = args.exe.resolve(strict=True)
    artifacts = args.artifacts.resolve()
    workspace = Path(__file__).resolve().parents[2]
    repository = workspace.parent.parent if workspace.parent.name == ".worktrees" else workspace
    test_root = repository / ".devtools" / "test-runs"
    if not artifacts.is_relative_to(test_root.resolve()) or not artifacts.name.startswith("b10-packaged-fast-scroll-"):
        parser.error("artifacts must be a new main-repository .devtools/test-runs/b10-packaged-fast-scroll-* directory")
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "driver-root")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from playwright.sync_api import sync_playwright
    from src.auth import _find_system_browser

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    stop = artifacts / "stop.signal"
    smoke_report = artifacts / "exe-smoke.json"
    env = os.environ.copy()
    env["TEMP"] = env["TMP"] = str(artifacts)
    env["PYTHONIOENCODING"] = "utf-8"
    result = {"checks": [], "failures": [], "errors": [], "metrics": {},
        "exe": str(exe), "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        "real_library_used": False, "real_account_used": False, "real_phone_tested": False,
        "actual_exe_database_works": 3, "browser_only_cloned_cards": 800,
        "coverage": "packaged frontend fault replay; 800 items are browser metadata, not EXE database rows",
        "mobile_interaction": "trusted CDP touch checks native scrolling; real wheel drives complete 800-card traversal"}
    output = (artifacts / "exe-output.txt").open("xb")
    process = subprocess.Popen([str(exe), "--smoke-test", "--smoke-output", str(smoke_report),
        "--smoke-serve-port", str(port), "--smoke-stop-file", str(stop)], cwd=artifacts,
        env=env, stdout=output, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    started_at = time.monotonic()
    page = None

    def read_json(path):
        with urlopen(origin + path, timeout=3) as response:
            return json.load(response)

    def check(name, passed, details=None):
        result["checks" if passed else "failures"].append({"name": name, "details": details})
        if not passed:
            print(json.dumps({"failed_check": name, "details": details}, ensure_ascii=False), flush=True)

    def state(current):
        return current.evaluate("""()=>{const root=document.querySelector('.gallery-viewport');
          const win=document.querySelector('.gallery-window'),area=root.getBoundingClientRect();
          const cards=[...document.querySelectorAll('.work-card')].filter(e=>{const r=e.getBoundingClientRect();
            return r.bottom>area.top&&r.top<area.bottom});return {offset:Number(win.dataset.start),
            window_size:Number(win.dataset.windowSize),scroll_top:root.scrollTop,visible_ids:cards.map(e=>Number(e.dataset.workId)),
            readable_images:cards.filter(e=>{const i=e.querySelector('img');return i&&i.complete&&i.naturalWidth>0}).length,
            error:document.querySelector('.gallery-view>.notice.error')?.textContent||'',
            horizontal_overflow:document.documentElement.scrollWidth>innerWidth};}""")

    def readable(current, work):
        return current.locator(f".work-card[data-work-id='{work}'] img").evaluate_all(
            "items=>items.some(e=>e.complete&&e.naturalWidth>0)")

    try:
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"EXE isolated service exited early: {process.returncode}")
            try:
                status = read_json("/api/status")
                break
            except OSError:
                time.sleep(.15)
        else:
            raise TimeoutError("EXE isolated service did not start")
        baseline = read_json("/api/library?compact=1&offset=0&limit=20")
        settings = read_json("/api/settings")
        assert baseline["total"] == len(baseline["items"]) == 3
        originals = {work["id"]: work for work in baseline["items"]}
        static = originals[1]
        animated = originals[2]
        assert static["thumbnail"] and static["preview"] and animated["preview"]
        result["package_identity"] = {"version": settings["version"], "build": settings["build"],
            "actual_db_work_ids": sorted(originals)}
        (artifacts / "exe-library-baseline.json").write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
        with urlopen(origin, timeout=3) as response:
            html = response.read()
        assets = re.findall(r'/reader/assets/[^"\']+', html.decode("utf-8"))
        result["served_reader_sha256"] = {"index.html": hashlib.sha256(html).hexdigest()}
        for asset in assets:
            with urlopen(origin + asset, timeout=3) as response:
                result["served_reader_sha256"][asset] = hashlib.sha256(response.read()).hexdigest()
        assert assets
        clones = []
        for clone_id in range(800, 0, -1):
            work = deepcopy(animated if clone_id == 799 else static)
            work.update(id=clone_id, title=f"EXE local UI replay {clone_id:04}")
            clones.append(work)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            for width, height in ((1440, 900), (390, 844), (320, 720)):
                context = browser.new_context(viewport={"width": width, "height": height},
                    is_mobile=width < 500, has_touch=width < 500)
                page = context.new_page()
                label = str(width)
                page.on("pageerror", lambda error, label=label: result["errors"].append({"width": label, "message": str(error)}))
                batch_requests, media_attempts, windows = [], Counter(), []
                transient = {"offset": None}
                static_targets = {urlsplit(static["thumbnail"]).path: "thumbnail",
                    urlsplit(static["preview"]).path: "original"}
                def library(route):
                    params = parse_qs(urlsplit(route.request.url).query)
                    offset = int(params.get("offset", [0])[0])
                    limit = int(params.get("limit", [240])[0])
                    status = 200
                    if offset > 0 and transient["offset"] is None:
                        transient["offset"] = offset
                        status = 503
                        route.fulfill(status=503, content_type="text/plain", body="Service Unavailable")
                    else:
                        data = deepcopy(baseline)
                        data.update(total=800, offset=offset, limit=limit, items=clones[offset:offset+limit])
                        route.fulfill(content_type="application/json", body=json.dumps(data, ensure_ascii=False))
                    batch_requests.append({"offset": offset, "limit": limit, "status": status})
                def image(route):
                    name = static_targets.get(urlsplit(route.request.url).path)
                    if name:
                        media_attempts[name] += 1
                        if media_attempts[name] == 1:
                            route.fulfill(status=503, content_type="text/plain", body="Service Unavailable")
                            return
                    route.continue_()
                page.route("**/api/library?*", library)
                page.route("**/images/**", image)
                page.goto(origin, wait_until="domcontentloaded")
                page.locator(".work-card[data-work-id='800']").wait_for()
                deadline = time.monotonic() + 8
                while not readable(page, 800) and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                check(label + "-exe-static-recovers-same-url-after-first-503",
                    readable(page, 800) and media_attempts["thumbnail"] >= 2
                        and media_attempts["original"] >= 1,
                    {"attempts": dict(media_attempts), "metadata_changed": False, "url_changed": False})
                check(label + "-exe-lightweight-animation-loads", readable(page, 799)
                    and "_card_motion_v1.webp" in animated["preview"], animated["preview"])
                page.screenshot(path=str(artifacts / f"exe-initial-recovered-{width}.png"))
                touch_state = None
                if width < 500:
                    cdp = context.new_cdp_session(page)
                    before = state(page)
                    for _ in range(3):
                        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart",
                            "touchPoints": [{"x": width/2, "y": height-120}]})
                        for y in (height-200, height-350, height-500, 120):
                            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove",
                                "touchPoints": [{"x": width/2, "y": max(100, y)}]})
                        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                        page.wait_for_timeout(80)
                    after = state(page)
                    touch_state = {"before": before, "after": after}
                    check(label + "-exe-trusted-cdp-touch-scrolls-gallery", after["scroll_top"] > before["scroll_top"], touch_state)
                    cdp.detach()
                page.mouse.move(width-40, height*.65)
                syntax_notices = []
                for _ in range(28):
                    page.mouse.wheel(0, 100000)
                    page.wait_for_timeout(25)
                    sample = state(page)
                    windows.append(sample["window_size"])
                    if "Unexpected token" in sample["error"]:
                        syntax_notices.append(sample["error"])
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    sample = state(page)
                    if 1 in sample["visible_ids"] and sample["readable_images"] > 0:
                        break
                    page.mouse.wheel(0, 100000)
                    page.wait_for_timeout(100)
                    windows.append(sample["window_size"])
                final = state(page)
                check(label + "-exe-append-plain-text-503-auto-recovers-without-manual-retry",
                    transient["offset"] is not None and any(r["offset"] == transient["offset"] and r["status"] == 200
                        for r in batch_requests) and not final["error"] and not syntax_notices,
                    {"injected_offset": transient["offset"], "requests": batch_requests, "final": final})
                check(label + "-exe-fast-scroll-reaches-readable-last-clone-with-bounded-window",
                    1 in final["visible_ids"] and final["readable_images"] > 0
                        and max(windows) <= 480 and not final["horizontal_overflow"], final)
                result["metrics"][label] = {"library_requests": batch_requests, "static_media_attempts": dict(media_attempts),
                    "window_samples": windows, "final": final, "touch": touch_state, "syntax_notices": syntax_notices}
                page.screenshot(path=str(artifacts / f"exe-last-readable-{width}.png"))
                context.close()
            browser.close()
    except Exception:
        result["errors"].append({"scenario": "driver", "message": traceback.format_exc()})
        if page is not None:
            try:
                page.screenshot(path=str(artifacts / "failure.png"))
            except Exception:
                pass
    finally:
        stop.write_text("stop", encoding="utf-8")
        try:
            result["exe_exit_code"] = process.wait(timeout=35)
        except subprocess.TimeoutExpired:
            # The smoke service expires by itself after 180 s. Only this newly created child is owned here.
            result["errors"].append({"scenario": "cleanup", "message": "isolated EXE did not stop within 35 seconds"})
            expiry = time.monotonic() + 155
            result["exe_exit_code"] = None
            while time.monotonic() < expiry:
                try:
                    result["exe_exit_code"] = process.wait(timeout=min(30, expiry-time.monotonic()))
                    break
                except subprocess.TimeoutExpired:
                    continue
        output.close()
        result["duration_seconds"] = round(time.monotonic()-started_at, 2)
        result["isolated_exe_stopped"] = process.poll() is not None
        result["smoke_report_exists"] = smoke_report.is_file()
        if smoke_report.is_file():
            report = json.loads(smoke_report.read_text(encoding="utf-8"))
            result["smoke_identity"] = {"status": report["status"], "version": report["version"], "build": report["build"]}
        result["passed"] = not result["failures"] and not result["errors"] and result["exe_exit_code"] == 0 and result["smoke_report_exists"]
        (artifacts / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "checks": len(result["checks"]), "failures": result["failures"],
        "errors": result["errors"], "artifacts": str(artifacts)}, ensure_ascii=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
