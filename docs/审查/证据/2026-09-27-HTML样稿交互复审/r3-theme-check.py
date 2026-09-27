"""Focused, offline browser check for the R3 theme transition."""

import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
PAGE = (ROOT / ".devtools" / "ui-prototype-20260927.html").as_uri()
CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe"
results = {"target": PAGE, "checks": [], "page_errors": [], "external_requests": []}


def check(name, passed, detail=None):
    results["checks"].append({"name": name, "passed": bool(passed), "detail": detail})


def read(page):
    return page.evaluate(
        """() => ({
          theme: document.body.dataset.theme,
          scrollY,
          cardSame: document.querySelector('.settings-card') === window.r3Card,
          buttonSame: document.querySelector('[data-theme-pick="light"]') === window.r3Light,
          favoriteCount: favorites.size,
          taskRound,
          focus: document.activeElement?.getAttribute('data-theme-pick'),
          pressed: [...document.querySelectorAll('[data-theme-pick]')].map(x => [x.dataset.themePick, x.getAttribute('aria-pressed')]),
          clip: getComputedStyle(document.documentElement, '::view-transition-new(root)').clipPath
        })"""
    )


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, headless=True)
    try:
        for label, viewport in (("desktop", {"width": 1440, "height": 1000}), ("mobile", {"width": 390, "height": 844})):
            context = browser.new_context(viewport=viewport, reduced_motion="no-preference")
            page = context.new_page()
            page.on("pageerror", lambda e: results["page_errors"].append(str(e)))
            page.on("request", lambda r: results["external_requests"].append(r.url) if not r.url.startswith(("file:", "data:")) else None)
            page.goto(PAGE)
            check(label + " initial load has no theme animation", page.evaluate("getComputedStyle(document.documentElement,'::view-transition-new(root)').clipPath") == "none")
            page.locator('[data-view="settings"]').click()
            page.wait_for_timeout(300)
            page.evaluate("scrollTo(0,80)")
            page.wait_for_timeout(50)
            page.evaluate("window.r3Card=document.querySelector('.settings-card');window.r3Light=document.querySelector('[data-theme-pick=light]')")
            before = read(page)
            page.locator('[data-theme-pick="light"]').evaluate("el => el.click()")
            page.wait_for_function("document.body.dataset.theme==='light' && getComputedStyle(document.documentElement,'::view-transition-new(root)').clipPath!=='none'")
            page.wait_for_timeout(70)
            middle = read(page)
            page.screenshot(path=str(EVIDENCE / f"r3-{label}-dark-to-light-middle.png"))
            page.wait_for_timeout(260)
            after = read(page)
            check(label + " dark to light transition", middle["theme"] == "light" and middle["clip"] != "none" and after["clip"] == "none", {"middle": middle, "after": after})
            check(label + " preserves nodes, scroll, favorites and task count", middle["cardSame"] and middle["buttonSame"] and abs(middle["scrollY"] - before["scrollY"]) < 1 and middle["favoriteCount"] == before["favoriteCount"] and middle["taskRound"] == before["taskRound"], {"beforeScroll": before["scrollY"], "middleScroll": middle["scrollY"]})
            page.locator('[data-theme-pick="dark"]').click()
            page.wait_for_timeout(115)
            back_middle = read(page)
            page.screenshot(path=str(EVIDENCE / f"r3-{label}-light-to-dark-middle.png"))
            page.wait_for_timeout(260)
            back_after = read(page)
            check(label + " light to dark transition", back_middle["theme"] == "dark" and back_middle["clip"] != "none" and back_after["clip"] == "none", {"middle": back_middle, "after": back_after})
            page.locator('[data-theme-pick="light"]').click()
            page.wait_for_timeout(40)
            page.locator('[data-theme-pick="dark"]').click()
            page.wait_for_timeout(40)
            page.locator('[data-theme-pick="light"]').click()
            page.wait_for_timeout(350)
            rapid = read(page)
            check(label + " rapid clicks end on last choice", rapid["theme"] == "light" and rapid["clip"] == "none" and rapid["pressed"] == [["dark", "false"], ["light", "true"]], rapid)
            page.evaluate("() => {document.querySelector('[data-theme-pick=dark]').click()}")
            page.wait_for_timeout(320)
            page.evaluate("() => {document.querySelector('[data-theme-pick=light]').click();document.querySelector('[data-theme-pick=dark]').click()}")
            page.wait_for_timeout(350)
            same_frame = read(page)
            check(label + " same-frame reversal ends on last choice", same_frame["theme"] == "dark" and same_frame["pressed"] == [["dark", "true"], ["light", "false"]] and same_frame["clip"] == "none", same_frame)
            page.locator('[data-theme-pick="light"]').click()
            page.locator('[data-view="gallery"]').click()
            page.wait_for_timeout(320)
            check(label + " navigation keeps choice without animation", page.evaluate("({view:document.querySelector('main')?.className,theme:document.body.dataset.theme,clip:getComputedStyle(document.documentElement,'::view-transition-new(root)').clipPath})") == {"view": "gallery", "theme": "light", "clip": "none"})
            page.reload()
            check(label + " persistence and reload without fade", page.locator("body").get_attribute("data-theme") == "light" and page.evaluate("getComputedStyle(document.documentElement,'::view-transition-new(root)').clipPath") == "none")
            context.close()

        context = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
        page = context.new_page()
        page.on("pageerror", lambda e: results["page_errors"].append(str(e)))
        page.goto(PAGE)
        page.locator('[data-view="settings"]').click()
        page.emulate_media(reduced_motion="reduce")
        page.evaluate("() => {window.r3Transitions=0;const start=document.startViewTransition.bind(document);document.startViewTransition=(...args)=>{r3Transitions++;return start(...args)}}")
        page.locator('[data-theme-pick="light"]').click()
        reduced = page.evaluate("({theme:document.body.dataset.theme,starts:r3Transitions,matches:matchMedia('(prefers-reduced-motion:reduce)').matches})")
        check("reduced motion switches immediately", reduced["theme"] == "light" and reduced["starts"] == 0 and reduced["matches"], reduced)
        context.close()
    finally:
        browser.close()

check("no script errors or external requests", not results["page_errors"] and not results["external_requests"], {"errors": results["page_errors"], "requests": results["external_requests"]})
(EVIDENCE / "r3-theme-checks.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"passed": sum(x["passed"] for x in results["checks"]), "total": len(results["checks"]), "failed": [x["name"] for x in results["checks"] if not x["passed"]]}, ensure_ascii=False))
