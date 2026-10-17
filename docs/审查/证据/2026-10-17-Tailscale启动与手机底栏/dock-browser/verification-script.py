"""Small final-EXE dock check; only the EXE's isolated three-work smoke library."""
from __future__ import annotations

import argparse
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
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app/tests"))
from client_mobile_chrome_browser import VIEWPORT_REPLAY

GEOMETRY = """() => {
  const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom}};
  const dock=document.querySelector('.reading-toolbar'), counter=dock.querySelector('.reading-counter');
  const buttons=[...dock.querySelectorAll('button')].map(e=>({name:e.getAttribute('aria-label'),...rect(e),disabled:e.disabled,
    background:getComputedStyle(e).backgroundColor,hit:e.contains(document.elementFromPoint(e.getBoundingClientRect().x+e.clientWidth/2,e.getBoundingClientRect().y+e.clientHeight/2))}));
  const counts=[...counter.querySelectorAll('small span')].map(e=>{const r=document.createRange();r.selectNodeContents(e);return {text:e.textContent,...rect(e),textWidth:r.getBoundingClientRect().width,clientWidth:e.clientWidth,scrollWidth:e.scrollWidth}});
  const vv=visualViewport;
  return {dock:rect(dock),counter:rect(counter),buttons,counts,grid:getComputedStyle(dock.firstElementChild).gridTemplateColumns,
    overflow:document.documentElement.scrollWidth>innerWidth,visibleBottom:vv.offsetTop+vv.height,
    nextRightGap:dock.getBoundingClientRect().right-buttons.at(-1).right,
    previousLeftGap:buttons[0].x-dock.getBoundingClientRect().x,
    heartNextGap:buttons.at(-1).x-(buttons.at(-2).x+buttons.at(-2).width),
    toolbarCount:document.querySelectorAll('.reading-toolbar').length};
}"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    exe = args.exe.resolve(strict=True)
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    run = Path(__file__).resolve().parent / "run"
    run.mkdir(exist_ok=False)
    os.environ["DATA_ROOT"] = str(run / "driver-root")
    os.environ["TEMP"] = os.environ["TMP"] = str(run)
    from src.auth import _find_system_browser
    from playwright.sync_api import sync_playwright, expect
    result = {"checks": [], "failures": [], "page_errors": [], "metrics": {},
              "exe": str(exe), "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
              "real_library_used": False, "real_account_used": False, "real_phone_tested": False,
              "layout_stress": "DOM count text replacement only; not a 16612-work/2000-page database",
              "bookmark_scope": "Browser-intercepted synthetic state and POST; no external account",
              "viewport_scope": "Chrome emulation and synthetic VisualViewport signal replay"}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))
    def fetch(path):
        with opener.open(origin + path, timeout=2) as response:
            return response.read()
    def read(path):
        return json.loads(fetch(path))
    stop = run / "stop.signal"
    smoke = run / "exe-smoke.json"
    output = (run / "exe-output.txt").open("xb")
    started = time.monotonic()
    process = subprocess.Popen([str(exe), "--smoke-test", "--smoke-output", str(smoke),
        "--smoke-serve-port", str(port), "--smoke-stop-file", str(stop)],
        cwd=run, env=os.environ.copy(), stdout=output, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW)
    page = None
    try:
        deadline = time.monotonic() + 65
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"smoke EXE exited: {process.returncode}")
            try:
                read("/api/status")
                break
            except OSError:
                time.sleep(.2)
        else:
            raise TimeoutError("EXE smoke service did not start")
        library = read("/api/library?compact=1&limit=3&sort=oldest")
        ids = [item["id"] for item in library["items"]]
        assert len(ids) == 3, ids
        result["actual_database_works"] = len(ids)
        result["actual_gallery_ids"] = ids
        epoch = read(f"/api/works/{ids[0]}?page_limit=1")["bookmark"]["account_epoch"] + 1
        html = fetch("/")
        assets = re.findall(r'/reader/assets/[^"\']+', html.decode("utf-8"))
        result["served_frontend_sha256"] = {"index.html": hashlib.sha256(html).hexdigest(),
            **{asset: hashlib.sha256(fetch(asset)).hexdigest() for asset in assets}}
        assert assets
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            for width, height in ((320, 844), (390, 844), (568, 320), (1440, 900)):
                context = browser.new_context(viewport={"width": width, "height": height},
                    is_mobile=width < 760, has_touch=width < 760)
                page = context.new_page()
                page.set_default_timeout(8000)
                page.add_init_script("localStorage.setItem('pixiv-local-theme','dark')")
                page.add_init_script(VIEWPORT_REPLAY)
                page.on("pageerror", lambda error: result["page_errors"].append(str(error)))
                facts = {}
                writes = []
                def fact(work_id):
                    return facts.setdefault(work_id, {"work_id": work_id, "status": "confirmed",
                        "value": False, "desired": None, "version": 100, "account_epoch": epoch,
                        "verified": True, "current_account_id": "synthetic-dock",
                        "fact_account_id": "synthetic-dock", "error": None, "cached": False,
                        "operation_id": None, "confirmed_at": "2026-10-17T00:00:00Z"})
                def block_external(route):
                    if route.request.url.startswith(origin + "/"):
                        route.continue_()
                    else:
                        route.abort("blockedbyclient")
                page.route("**/*", block_external)
                def route_api(route):
                    path = urlsplit(route.request.url).path
                    match = re.fullmatch(r"/api/works/(\d+)(/bookmark(?:/refresh)?)?", path)
                    if match and match[2]:
                        state = fact(int(match[1]))
                        if route.request.method == "POST" and match[2] == "/bookmark":
                            body = route.request.post_data_json
                            writes.append({"path": path, "body": body})
                            state.update(value=body["desired"], desired=body["desired"], version=state["version"] + 1)
                        route.fulfill(json=deepcopy(state))
                    elif match:
                        response = route.fetch()
                        data = response.json()
                        data["bookmark"] = deepcopy(fact(int(match[1])))
                        route.fulfill(response=response, json=data)
                    elif re.fullmatch(r"/api/reading-sessions/[^/]+/feed", path):
                        response = route.fetch()
                        data = response.json()
                        for item in data["items"]:
                            item["bookmark"] = deepcopy(fact(item["work_id"]))
                        route.fulfill(response=response, json=data)
                    else:
                        route.fallback()
                page.route(origin + "/api/**", route_api)
                def loaded():
                    page.locator(".reading-toolbar").wait_for()
                    page.wait_for_function("document.querySelector('.image-page img')?.naturalWidth > 0")
                def measure(label, screenshot=True):
                    page.wait_for_timeout(100)
                    metric = page.evaluate(GEOMETRY)
                    result["metrics"][label] = metric
                    if screenshot:
                        page.screenshot(path=str(evidence / (label + ".png")))
                    assert not metric["overflow"] and metric["toolbarCount"] == 1, metric
                    assert metric["dock"]["bottom"] <= metric["visibleBottom"] + .5, metric
                    assert metric["previousLeftGap"] <= 6.1 and metric["nextRightGap"] <= 6.1, metric
                    assert metric["heartNextGap"] <= 6.1, metric
                    for button in metric["buttons"]:
                        assert button["width"] >= 44 and button["height"] >= 44 and button["hit"], button
                        assert button["background"] == "rgba(0, 0, 0, 0)", button
                    for count in metric["counts"]:
                        assert count["textWidth"] <= count["width"] + .5 and count["scrollWidth"] <= count["clientWidth"], count
                    if width <= 520:
                        assert metric["counts"][1]["y"] > metric["counts"][0]["y"], metric
                    else:
                        assert abs(metric["counts"][1]["y"] - metric["counts"][0]["y"]) < 1, metric
                    result["checks"].append(label)
                    print("PASS " + label, flush=True)
                page.goto(origin + f"/illust/{ids[0]}?sort=oldest", wait_until="domcontentloaded")
                loaded()
                expect(page.get_by_role("button", name="上一件作品", exact=True)).to_be_disabled()
                measure(f"dock-{width}-detail")
                if width in (320, 390):
                    page.evaluate("""() => {document.querySelector('.reading-work-count').textContent='16612 / 16612 件'; document.querySelector('.reading-page-count').textContent='第 1999 / 2000 页'}""")
                    measure(f"dock-{width}-layout-stress")
                if width == 320:
                    # One real next/previous pair, using only the EXE's three-work library.
                    page.get_by_role("button", name="下一件作品", exact=True).tap()
                    page.wait_for_url(re.compile(rf"/illust/{ids[1]}(?:\?|$)"))
                    loaded()
                    page.get_by_role("button", name="上一件作品", exact=True).tap()
                    page.wait_for_url(re.compile(rf"/illust/{ids[0]}(?:\?|$)"))
                    result["checks"].append("actual-next-previous-work-pair")
                    page.goto(origin + f"/illust/{ids[-1]}?sort=oldest", wait_until="domcontentloaded")
                    loaded()
                    expect(page.get_by_role("button", name="下一件作品", exact=True)).to_be_disabled()
                    result["checks"].append("first-previous-and-last-next-disabled")
                if width == 390:
                    heart = page.locator(".reading-toolbar .bookmark-button")
                    expect(heart).to_have_attribute("aria-pressed", "false")
                    heart.tap()
                    expect(heart).to_have_class(re.compile(r"\backnowledging\b"))
                    page.screenshot(path=str(evidence / "dock-390-bookmark-feedback.png"))
                    expect(heart).to_have_attribute("aria-pressed", "true")
                    expect(heart).not_to_have_class(re.compile(r"\backnowledging\b"), timeout=6000)
                    assert len(writes) == 1 and writes[0]["body"]["desired"] is True, writes
                    result["synthetic_bookmark_writes"] = writes
                    result["checks"].append("mobile-bookmark-one-synthetic-post-feedback-confirmed")
                page.goto(origin + "/flow?sort=oldest", wait_until="domcontentloaded")
                loaded()
                measure(f"dock-{width}-flow")
                if width == 320:
                    page.get_by_role("button", name="作品信息与整理", exact=True).tap()
                    page.get_by_role("complementary", name="作品整理").wait_for()
                    page.get_by_role("button", name="关闭作品整理", exact=True).tap()
                    expect(page.get_by_role("complementary", name="作品整理")).to_have_count(0)
                    result["checks"].append("mobile-flow-counter-opens-and-closes-info")
                if width == 390:
                    cdp = context.new_cdp_session(page)
                    cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": {"top": 0, "left": 0, "right": 0, "bottom": 24}})
                    page.evaluate("window.__replayVisualViewport({height:620,width:390,offsetTop:0,offsetLeft:0,pageTop:0,pageLeft:0,scale:1})")
                    measure("dock-390-viewport-safe", screenshot=False)
                    cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": {"top": 0, "left": 0, "right": 0, "bottom": 0}})
                    page.evaluate("window.__replayVisualViewport(null)")
                    measure("dock-390-viewport-restored", screenshot=False)
                page.unroute_all(behavior="wait")
                context.close()
            browser.close()
    except Exception as error:
        result["failures"].append({"error": str(error), "traceback": traceback.format_exc()})
        if page:
            try:
                page.screenshot(path=str(evidence / "dock-failure.png"), timeout=2000)
            except Exception:
                pass
        raise
    finally:
        stop.write_text("stop", encoding="utf-8")
        try:
            result["exe_exit_code"] = process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            process.terminate()
            result["exe_exit_code"] = process.wait(timeout=10)
            result["failures"].append({"error": "isolated EXE needed terminate after stop signal"})
        output.close()
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        if smoke.is_file():
            result["smoke_result"] = json.loads(smoke.read_text(encoding="utf-8"))
        (evidence / "dock-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not result["page_errors"] and not result["failures"] and result["exe_exit_code"] == 0, result
    print(json.dumps({"passed": len(result["checks"]), "evidence": str(evidence)}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
