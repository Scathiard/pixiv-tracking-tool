"""发布 EXE 的无浏览器、无网络冒烟入口。"""
from __future__ import annotations

import asyncio
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time


async def _request_status(app, path):
    messages = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [(b"host", b"localhost:1919")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 1919),
        "state": {},
    }
    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    return start["status"], body


def run_smoke_test() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--smoke-output", type=Path)
    parser.add_argument("--smoke-serve-port", type=int)
    parser.add_argument("--smoke-stop-file", type=Path)
    args = parser.parse_args()
    if (args.smoke_serve_port is None) != (args.smoke_stop_file is None):
        parser.error("--smoke-serve-port 和 --smoke-stop-file 必须一起使用")
    if args.smoke_serve_port is not None and not (1 <= args.smoke_serve_port <= 65535):
        parser.error("无效的隔离服务端口")
    root = Path(tempfile.mkdtemp(prefix="pixiv-isolated-smoke-"))
    os.environ["DATA_ROOT"] = str(root)
    from .config import resource_path
    from .core.runtime import Runtime
    from .core.models import ArchiveIntent, Artist, Asset, Work
    from .version import __version__, __build__
    from .web import create_app
    from PIL import Image

    for relative in ("src/templates/base.html", "static/style.css", "static/task-panel.js"):
        if not Path(resource_path(relative)).is_file():
            raise RuntimeError(f"缺少打包资源: {relative}")
    runtime = Runtime(root / "data/pixiv_tracker.db", root / "images", create=True)
    with Image.new("RGB", (32, 48), "navy") as picture:
        picture.save(root / "images/1_p0.png")
    with runtime.database.write() as session:
        work = Work(id=1, pixiv_illust_id="1", artist=Artist(pixiv_user_id="1", name="smoke"), title="isolated")
        work.assets = [Asset(role="image", relative_path="1_p0.png")]
        session.add_all([work, ArchiveIntent(pixiv_illust_id="1")])
    # Exercise the bundled task core without starting a real-account path.
    runtime.queue.submit("repair", {"work_ids": [1]}, "smoke")
    runtime.queue.run_round()
    if runtime.queue.snapshot()["round"]["outcome"] != "succeeded":
        raise RuntimeError("打包程序的原件核验和缩略图生成失败")
    index = Path(resource_path("frontend/dist/index.html"))
    reading_ui = index.is_file()
    if not reading_ui:
        raise RuntimeError(f"v{__version__} 缺少阅览界面产物")
    asset_paths = re.findall(r"/reader/assets/[^\"']+", index.read_text(encoding="utf-8")) if reading_ui else []
    if reading_ui and not asset_paths:
        raise RuntimeError("阅览界面缺少可加载的脚本与样式")
    application = create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=reading_ui)

    async def exercise():
        result = {}
        async with application.router.lifespan_context(application):
            paths = ["/api/status", "/api/library", "/api/progress", "/api/settings", "/api/artists",
                     "/api/works/1/bookmark", "/", "/illust/1", "/artists", "/artist/1", "/settings"]
            if reading_ui:
                paths.extend(["/flow", *asset_paths])
            for path in paths:
                status, body = await _request_status(application, path)
                if status != 200 or not body:
                    raise RuntimeError(f"ASGI 冒烟失败: {path} HTTP {status}")
                result[path] = status
        return result

    checks = asyncio.run(exercise())
    if args.smoke_serve_port is not None:
        import uvicorn
        # The ASGI checks closed their runtime; serve the same synthetic files
        # through a new owner for actual packaged-browser checks.
        browser_runtime = Runtime(root / "data/pixiv_tracker.db", root / "images")
        browser_app = create_app(lambda: browser_runtime, authenticate=False, schedule=False, reading_ui=reading_ui)
        server = uvicorn.Server(uvicorn.Config(browser_app, host="127.0.0.1", port=args.smoke_serve_port,
            loop="src.networking:http_loop_factory", log_level="error", timeout_graceful_shutdown=10,
            limit_concurrency=128))
        expired = threading.Event()

        def stop_when_requested():
            deadline = time.monotonic() + 180
            while not args.smoke_stop_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.1)
            if not args.smoke_stop_file.is_file():
                expired.set()
            server.should_exit = True

        observer = threading.Thread(target=stop_when_requested, daemon=True)
        observer.start()
        server.run()
        observer.join(1)
        if expired.is_set() or not server.started:
            raise RuntimeError("隔离浏览器服务未正常完成")
        checks["packaged_browser_service"] = 200
    report = {"status": "SMOKE_OK", "version": __version__, "build": __build__, "fixture": str(root), "checks": checks,
              "real_account_used": False, "real_library_used": False, "reading_ui": reading_ui}
    destination = args.smoke_output or root / "smoke-result.json"
    with destination.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))
    return 0
