"""Isolated Chromium request probe against the existing v0.2.4 frontend bundle.

Serves synthetic JSON and a data-URL image. Never opens project configuration,
tokens, the normal application, or the user's library. Keep run artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import socket
import shutil
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright
import uvicorn


ROOT = Path(__file__).resolve().parents[3]
DIST = ROOT / "app/frontend/dist"
PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg=="
BOOKMARK = {"status": "confirmed", "version": 0, "cached": False, "value": False,
            "desired": None, "operation_id": None, "confirmed_at": "2026-09-28T00:00:00Z", "error": None}
AS_OF = "2026-09-28T00:00:00Z"
WORK_COUNT = 601


def card(work_id: int) -> dict:
    return {"id": work_id, "pixiv_id": str(900000 + work_id), "title": f"Synthetic work {work_id}",
            "artist_id": 1, "artist_name": "Synthetic artist", "type": "illust", "page_count": 1,
            "x_restrict": None, "thumbnail": None, "is_hidden": False,
            "posted_at": AS_OF, "bookmark": {**BOOKMARK, "work_id": work_id}}


def detail(work_id: int, offset: int, limit: int) -> dict:
    return {**card(work_id), "caption": "Synthetic description", "caption_state": "available", "tags": [],
            "is_bookmarked": False, "rating": 0, "bookmark_count": 0, "view_count": 0,
            "display_page_count": 1, "pages": [{"index": 0, "src": PIXEL, "width": 1, "height": 1,
                "state": "verified"}] if offset == 0 and limit > 0 else [],
            "assets": [], "legacy_variants": []}


def make_app(state: dict) -> FastAPI:
    app = FastAPI()
    app.mount("/reader", StaticFiles(directory=DIST), name="reader")

    @app.middleware("http")
    async def record(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            state["requests"].append({"at": round(time.monotonic() - state["started"], 3),
                                      "method": request.method, "path": path,
                                      "query": str(request.url.query)})
        return await call_next(request)

    @app.get("/api/status")
    def status():
        return {"page_size": 100, "pagination_mode": "scroll"}

    @app.get("/api/progress")
    def progress():
        return {"round": None, "next_round_count": 0, "needs_attention_count": 0,
                "operations": [], "recovery_issues": [], "automatic_paused": False}

    @app.get("/api/library")
    def library(request: Request, offset: int = 0, limit: int = 100, compact: bool = False):
        return {"items": [card(i) for i in range(offset + 1, min(WORK_COUNT, offset + limit) + 1)],
                "total": WORK_COUNT, "offset": offset, "ceiling": WORK_COUNT, "as_of": AS_OF}

    @app.post("/api/tasks")
    async def tasks(request: Request):
        body = await request.json()
        if body.get("kind") == "thumbnails":
            state["thumbnail_posts"].append({"at": round(time.monotonic() - state["started"], 3),
                                             "work_count": len(body.get("work_ids", [])),
                                             "fail": state["fail_thumbnail"]})
            if state["fail_thumbnail"]:
                return JSONResponse({"detail": "synthetic thumbnail queue failure"}, status_code=503)
        return {"operation_id": "synthetic-task", "created": True}

    @app.get("/api/works/{work_id}")
    def work(work_id: int, page_offset: int = 0, page_limit: int = 100):
        return detail(work_id, page_offset, page_limit)

    @app.post("/api/works/{work_id}/bookmark/refresh")
    def bookmark_refresh(work_id: int):
        return {**BOOKMARK, "work_id": work_id}

    @app.get("/api/works/{work_id}/bookmark")
    def bookmark_read(work_id: int):
        return {**BOOKMARK, "work_id": work_id}

    @app.get("/{path:path}")
    def index(path: str):
        return FileResponse(DIST / "index.html")

    return app


def range_requests(events: list[dict]) -> list[dict]:
    return [item for item in events if item["path"] == "/api/library" and
            "compact=1" in item["query"]]


def browser_path() -> str:
    for path in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                 r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
        if Path(path).is_file():
            return path
    for name in ("chrome", "chromium", "msedge"):
        if path := shutil.which(name):
            return path
    raise RuntimeError("Chromium browser not found")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    args = parser.parse_args()
    output = args.artifacts.resolve()
    output.mkdir(parents=True, exist_ok=False)
    state = {"started": time.monotonic(), "requests": [], "thumbnail_posts": [], "fail_thumbnail": True}
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(make_app(state), host="127.0.0.1", port=port,
                                          log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    browser = None
    result = {"baseline": "main/f31b12241b7c2acc7bcdcb201138e1d745ea65bb",
              "bundle_sha256": hashlib.sha256((DIST / "index.html").read_bytes()).hexdigest(),
              "synthetic_works": WORK_COUNT, "checks": {}, "errors": []}
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("Synthetic server did not start")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=browser_path())
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            page = context.new_page()
            page.set_default_timeout(12000)
            page.on("pageerror", lambda error: result["errors"].append(str(error)))
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.locator(".gallery-window[data-window-size]").wait_for()
            page.wait_for_function("document.querySelector('.gallery-window')?.dataset.windowSize === '240'")
            page.get_by_text("synthetic thumbnail queue failure").wait_for()
            initial = {"range_count": len(range_requests(state["requests"])),
                       "window_size": int(page.locator(".gallery-window").get_attribute("data-window-size")),
                       "thumbnail_posts": list(state["thumbnail_posts"])}
            page.screenshot(path=str(output / "thumbnail-503.png"))
            page.locator(".gallery-viewport").evaluate("element => element.scrollTop = element.scrollHeight")
            page.wait_for_timeout(500)
            blocked = {"range_count": len(range_requests(state["requests"])),
                       "window_size": int(page.locator(".gallery-window").get_attribute("data-window-size")),
                       "error_visible": page.get_by_text("synthetic thumbnail queue failure").count() > 0}
            assert blocked["range_count"] == initial["range_count"]
            assert blocked["window_size"] == initial["window_size"]
            result["checks"]["thumbnail_503_blocks_continuous"] = {"initial": initial, "after_scroll": blocked}

            state["fail_thumbnail"] = False
            page.get_by_role("button", name="重试").first.click()
            page.wait_for_function("document.querySelector('.gallery-viewport')?.getAttribute('aria-busy') === 'false'")
            page.locator(".gallery-viewport").evaluate("element => element.scrollTop = element.scrollHeight")
            page.wait_for_function("Number(document.querySelector('.gallery-window')?.dataset.windowSize) > 240")
            recovered = {"range_count": len(range_requests(state["requests"])),
                         "window_size": int(page.locator(".gallery-window").get_attribute("data-window-size")),
                         "thumbnail_posts": len(state["thumbnail_posts"])}
            result["checks"]["retry_restores_continuous"] = recovered

            idle_start = len(state["requests"])
            page.wait_for_timeout(4800)
            idle_progress = [item for item in state["requests"][idle_start:] if item["path"] == "/api/progress"]
            result["checks"]["idle_progress_polling"] = {"duration_ms": 4800,
                "count": len(idle_progress), "times_s": [item["at"] for item in idle_progress],
                "panel_visible": page.locator(".task-status").count() > 0}

            # A fresh browser context has no saved gallery anchor from the scroll probe.
            context.close()
            context = browser.new_context(viewport={"width": 1400, "height": 900})
            page = context.new_page()
            page.set_default_timeout(12000)
            page.on("pageerror", lambda error: result["errors"].append(str(error)))
            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.locator(".work-card[data-work-id='1'] .card-open").wait_for()
            page.locator(".work-card[data-work-id='1'] .card-open").click()
            page.locator(".page-viewport.reader .image-page[data-work-id='1']").wait_for()
            page.wait_for_function("!document.querySelector('.reader-notice')")
            start = len(state["requests"])
            page.get_by_role("button", name="下一件").click()
            page.locator(".page-viewport.reader .image-page[data-work-id='2']").wait_for()
            page.wait_for_function("!document.querySelector('.reader-notice')")
            midpoint = len(state["requests"])
            page.get_by_role("button", name="上一件").click()
            page.locator(".page-viewport.reader .image-page[data-work-id='1']").wait_for()
            page.wait_for_function("!document.querySelector('.reader-notice')")
            backward = state["requests"][midpoint:]
            forward = state["requests"][start:midpoint]
            def summarize(events):
                return {"count": len(events), "paths": [{"path": x["path"], "query": x["query"]}
                                                       for x in events if x["path"] in ("/api/library", "/api/works/1", "/api/works/2")],
                        "relevant_count": sum(x["path"] in ("/api/library", "/api/works/1", "/api/works/2") for x in events)}
            result["checks"]["ordinary_a_b_a_requests"] = {"a_to_b": summarize(forward), "b_to_a": summarize(backward)}
            page.screenshot(path=str(output / "ordinary-return-a.png"))
            assert result["checks"]["ordinary_a_b_a_requests"]["a_to_b"]["relevant_count"] >= 3
            assert result["checks"]["ordinary_a_b_a_requests"]["b_to_a"]["relevant_count"] >= 3
            context.close()
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        result["requests"] = state["requests"]
        result["thumbnail_posts"] = state["thumbnail_posts"]
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"checks": result["checks"], "errors": result["errors"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
