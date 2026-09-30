"""Scroll reachability and icon evidence on the real UI, using synthetic data only."""
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
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--wide-only", action="store_true", help="Only reproduce wide desktop gutters")
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "fixture")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    os.environ["PORT"] = str(port)

    import uvicorn
    from playwright.sync_api import sync_playwright
    from client_fixture import make_library
    runtime, _ = make_library(artifacts / "fixture", 60)
    from src.auth import _find_system_browser
    from src.web import create_app
    server = uvicorn.Server(uvicorn.Config(
        create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True),
        host="127.0.0.1", port=port, log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    metrics, issues, checks, errors = [], [], [], []
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started, "synthetic service did not start"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=_find_system_browser(), headless=True)
            sizes = ((2560, 900, 1),) if args.wide_only else ((2560, 900, 1), (1440, 900, 1), (390, 844, 2), (320, 640, 2))
            for width, height, scale in sizes:
                phone = width < 760
                context = browser.new_context(viewport={"width": width, "height": height},
                    device_scale_factor=scale, is_mobile=phone, has_touch=phone)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{port}/settings")
                page.locator(".settings-version").wait_for(state="attached")
                page.screenshot(path=str(artifacts / f"settings-{width}-top.png"))
                if not phone:
                    metrics.append({"width": width, "icons": page.locator(".sidebar-nav button").filter(
                        has_text="设置").locator("svg path").evaluate_all("""elements => elements.map(e => {
                        const b = e.getBBox(); return {x:b.x,y:b.y,width:b.width,height:b.height,
                          centerX:b.x+b.width/2,centerY:b.y+b.height/2};})""")})
                for route, container, last in (("settings", ".settings-page", ".settings-version"),
                        ("artists", ".subpage", ".artist-profile:last-child")):
                    if route != "settings":
                        page.goto(f"http://127.0.0.1:{port}/{route}")
                        page.locator(last).wait_for(state="attached")
                    pane = page.locator(container)
                    if not phone:
                        for collapsed in (False, True):
                            if bool(page.locator('.app-shell.sidebar-collapsed').count()) != collapsed:
                                page.get_by_role('button', name='展开侧栏' if not collapsed else '收起侧栏', exact=True).click()
                            sidebar = page.locator('.app-header').bounding_box()
                            assert sidebar
                            content_left = sidebar['x'] + sidebar['width']
                            for position, x in (('center', (content_left + width) / 2),
                                                ('left-gutter', content_left + 8), ('right-gutter', width - 12)):
                                pane.evaluate('e => e.scrollTop = 0')
                                page.wait_for_timeout(100)
                                y = height * .55
                                hit = page.evaluate('''([x,y])=>{const e=document.elementFromPoint(x,y);
                                    return {className:String(e?.className || ''),
                                        inPane:!!e?.closest('.settings-page,.subpage')}}''', [x,y])
                                page.mouse.move(x, y)
                                page.mouse.wheel(0, 450)
                                page.wait_for_timeout(180)
                                top = pane.evaluate('e => e.scrollTop')
                                metrics.append({'width':width,'route':route,'position':position,
                                    'sidebar_collapsed':collapsed,'scrollTop':top,'hit':hit})
                                check = f'{route}-{width}-{position}-sidebar-{collapsed}'
                                if top > 0 and hit['inPane']:
                                    checks.append(check)
                                else:
                                    issues.append(check + ': wheel does not scroll the content pane')
                        # Return to the ordinary sidebar before the bottom/fit check.
                        if page.locator('.app-shell.sidebar-collapsed').count():
                            page.get_by_role('button', name='展开侧栏', exact=True).click()
                        pane.evaluate('e => e.scrollTop = 0')
                    before = pane.evaluate("e=>({client:e.clientHeight,scroll:e.scrollHeight,top:e.scrollTop})")
                    if phone:
                        cdp = context.new_cdp_session(page)
                        for _ in range(12):
                            cdp.send("Input.dispatchTouchEvent", {"type":"touchStart", "touchPoints":[
                                {"x":width*.7,"y":height-90,"id":1}]})
                            for step in range(1, 9):
                                cdp.send("Input.dispatchTouchEvent", {"type":"touchMove", "touchPoints":[
                                    {"x":width*.7,"y":height-90-step*(height-200)/8,"id":1}]})
                                page.wait_for_timeout(12)
                            cdp.send("Input.dispatchTouchEvent", {"type":"touchEnd", "touchPoints":[]})
                            page.wait_for_timeout(80)
                        cdp.detach()
                    else:
                        page.mouse.move(width*.75, height*.55)
                        page.mouse.wheel(0, 12000)
                    page.wait_for_timeout(500)
                    after = pane.evaluate("e=>({client:e.clientHeight,scroll:e.scrollHeight,top:e.scrollTop})")
                    reachable = page.locator(last).evaluate("""e=>{const b=e.getBoundingClientRect();
                        return b.bottom <= innerHeight+1 && b.bottom>50 && b.top<innerHeight;}""")
                    fits = page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    metrics.append({"width":width,"route":route,"before":before,"after":after,
                        "bottom_reachable":reachable,"horizontal_fit":fits})
                    page.screenshot(path=str(artifacts / f"{route}-{width}-bottom.png"))
                    if not reachable or not fits or (before["scroll"] > height and after["top"] == 0):
                        issues.append(f"{route}-{width}: bottom control is clipped or cannot scroll")
                    else:
                        checks.append(f"{route}-{width}-bottom-reachable")
                context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(20)
        (artifacts / "result.json").write_text(json.dumps({"checks":checks,"issues":issues,
            "errors":errors,"metrics":metrics,"real_library_used":False,"real_account_used":False,
            "real_phone_tested":False}, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not issues and not errors, issues + errors
    print(json.dumps({"checks":checks,"issues":issues}, ensure_ascii=False))


if __name__ == "__main__":
    main()
