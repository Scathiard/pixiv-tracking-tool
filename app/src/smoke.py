"""发布 EXE 的无浏览器、无网络冒烟入口。"""
from __future__ import annotations

import asyncio
import argparse
import json
import os
from pathlib import Path
import tempfile


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
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="pixiv-isolated-smoke-"))
    os.environ["DATA_ROOT"] = str(root)
    from .config import resource_path
    from .core.runtime import Runtime
    from .core.models import ArchiveIntent, Artist, Asset, Work
    from .version import __version__
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
    application = create_app(lambda: runtime, authenticate=False, schedule=False)

    async def exercise():
        result = {}
        async with application.router.lifespan_context(application):
            for path in ("/api/status", "/api/library", "/api/progress", "/", "/illust/1", "/settings"):
                status, body = await _request_status(application, path)
                if status != 200 or not body:
                    raise RuntimeError(f"ASGI 冒烟失败: {path} HTTP {status}")
                result[path] = status
        return result

    checks = asyncio.run(exercise())
    report = {"status": "SMOKE_OK", "version": __version__, "fixture": str(root), "checks": checks,
              "real_account_used": False, "real_library_used": False}
    destination = args.smoke_output or root / "smoke-result.json"
    with destination.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))
    return 0
