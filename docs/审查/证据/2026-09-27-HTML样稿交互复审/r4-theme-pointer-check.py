"""Check theme changes using trusted pointer input during the animation."""

import hashlib
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


EVIDENCE = Path(__file__).resolve().parent
PAGE = EVIDENCE.parents[3] / ".devtools" / "ui-prototype-20260927.html"
results = {"sha256": hashlib.sha256(PAGE.read_bytes()).hexdigest(), "checks": [], "errors": [], "external_requests": []}


def check(name, passed, detail=None):
    results["checks"].append({"name": name, "passed": bool(passed), "detail": detail})


def setup(browser, mobile=False, reduced=False, path=PAGE):
    context = browser.new_context(
        viewport={"width": 390, "height": 844} if mobile else {"width": 1440, "height": 1000},
        has_touch=mobile, is_mobile=mobile,
        reduced_motion="reduce" if reduced else "no-preference",
    )
    page = context.new_page()
    page.on("pageerror", lambda e: results["errors"].append(str(e)))
    page.on("request", lambda r: results["external_requests"].append(r.url) if not r.url.startswith(("file:", "data:")) else None)
    page.goto(path.as_uri())
    page.locator('[data-view="settings"]').click()
    page.wait_for_timeout(250)
    page.evaluate("scrollTo(0,80)")
    page.wait_for_timeout(50)
    page.evaluate("""() => {
      window.themeClicks=[];window.savedCard=document.querySelector('.settings-card');
      document.addEventListener('click',e=>themeClicks.push({
        choice:e.target.closest('[data-theme-pick]')?.dataset.themePick||null,
        trusted:e.isTrusted,time:performance.now()
      }),true);
    }""")
    return context, page


def point(page, selector):
    r = page.locator(selector).bounding_box()
    return r["x"] + r["width"] / 2, r["y"] + r["height"] / 2


def click_at(page, coords, mobile):
    if mobile:
        page.touchscreen.tap(*coords)
    else:
        page.mouse.click(*coords)


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe", headless=True)
    try:
        context, page = setup(browser, path=EVIDENCE / "prototype-r3.html")
        coords = {k: point(page, f'[data-theme-pick="{k}"]') for k in ("dark", "light")}
        for choice in ("light", "dark"):
            click_at(page, coords[choice], False)
            page.wait_for_timeout(45)
        before = page.evaluate("({theme:document.body.dataset.theme,clicks:themeClicks})")
        check("R3 reproduces lost second physical click", before["theme"] == "light" and [x["choice"] for x in before["clicks"]] == ["light", None], before)
        context.close()

        for mobile in (False, True):
            label = "touch 390px" if mobile else "mouse 1440px"
            context, page = setup(browser, mobile=mobile)
            coords = {k: point(page, f'[data-theme-pick="{k}"]') for k in ("dark", "light")}
            sequence = ["light", "dark"] * 4
            states = []
            for choice in sequence:
                click_at(page, coords[choice], mobile)
                states.append(page.evaluate("""({theme:document.body.dataset.theme,
                  selected:document.querySelector('[data-theme-pick][aria-pressed=true]')?.dataset.themePick,
                  count:document.querySelectorAll('.theme-snapshot').length,
                  sameCard:document.querySelector('.settings-card')===savedCard,scrollY})"""))
                page.wait_for_timeout(25)
            events = page.evaluate("themeClicks")
            check(label + " every real click is handled during animation", [x["choice"] for x in events] == sequence and all(x["trusted"] for x in events) and max(b["time"]-a["time"] for a,b in zip(events, events[1:])) < 280, events)
            check(label + " every choice updates immediately without rebuilding", all(s["theme"] == s["selected"] == choice and s["count"] == 1 and s["sameCard"] and s["scrollY"] == 80 for s,choice in zip(states, sequence)), states)
            page.wait_for_timeout(350)
            check(label + " ends on last choice with no remaining overlay", page.evaluate("document.body.dataset.theme==='dark' && !themeTransition && !document.querySelector('.theme-snapshot')"))

            click_at(page, coords["light"], mobile)
            page.evaluate("themeTransition.animation.pause();themeTransition.animation.currentTime=110")
            middle = page.evaluate("""() => {
              const layer=document.querySelector('.theme-snapshot');
              const live=document.querySelector('.settings-card').getBoundingClientRect();
              const copy=layer.querySelector('.settings-card').getBoundingClientRect();
              const header=document.querySelector('.topbar').getBoundingClientRect();
              const copiedHeader=layer.querySelector('.topbar').getBoundingClientRect();
              return {radius:parseFloat(getComputedStyle(layer).getPropertyValue('--theme-radius')),
                inset:layer.inert,hidden:layer.getAttribute('aria-hidden'),
                hit:document.elementFromPoint(live.right-35,live.top+30)?.closest('.theme-snapshot')===null,
                aligned:Math.abs(live.y-copy.y)<1 && Math.abs(live.x-copy.x)<1 && Math.abs(header.y-copiedHeader.y)<1};
            }""")
            check(label + " mid-animation copy is aligned and never captures input", middle["radius"] > 0 and middle["inset"] and middle["hidden"] == "true" and middle["hit"] and middle["aligned"], middle)
            page.screenshot(path=str(EVIDENCE / ("r4-mobile-middle.png" if mobile else "r4-desktop-middle.png")))
            page.evaluate("themeTransition.animation.play()")
            click_at(page, point(page, '[data-view="gallery"]'), mobile)
            check(label + " navigation stays clickable during animation", page.evaluate("view==='gallery' && !themeTransition && !document.querySelector('.theme-snapshot')"))
            page.reload()
            check(label + " final choice persists without startup animation", page.evaluate("document.body.dataset.theme==='light' && !document.querySelector('.theme-snapshot')"))
            page.locator('[data-view="settings"]').click()
            page.wait_for_timeout(250)
            page.locator('[data-theme-pick="dark"]').focus()
            page.keyboard.press('Enter')
            page.keyboard.press('Tab')
            page.keyboard.press('Enter')
            check(label + " keyboard also switches during animation", page.evaluate("document.body.dataset.theme==='light' && document.activeElement.dataset.themePick==='light'"))
            if not mobile:
                page.mouse.wheel(0,120)
                page.wait_for_timeout(80)
                check("scrolling removes the stale visual copy", page.evaluate("!themeTransition && !document.querySelector('.theme-snapshot')"))
            context.close()

        context, page = setup(browser, mobile=True, reduced=True)
        click_at(page, point(page, '[data-theme-pick="light"]'), True)
        check("reduced motion is immediate without an overlay", page.evaluate("document.body.dataset.theme==='light' && !themeTransition && !document.querySelector('.theme-snapshot')"))
        context.close()
    finally:
        browser.close()

check("no script errors or external requests", not results["errors"] and not results["external_requests"])
(EVIDENCE / "r4-theme-pointer-checks.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
failed = [x["name"] for x in results["checks"] if not x["passed"]]
print(json.dumps({"passed": len(results["checks"])-len(failed), "total": len(results["checks"]), "failed": failed}))
raise SystemExit(bool(failed))
