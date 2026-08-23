"""发布 EXE 的无浏览器、无网络冒烟入口。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path


async def _request_status(app):
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
        "path": "/api/status",
        "raw_path": b"/api/status",
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
    from .config import DATA_DIR, resource_path
    from .models import engine, init_db
    from .version import __version__
    from .web import app

    init_db()
    for relative in ("src/templates/base.html", "static/style.css"):
        if not Path(resource_path(relative)).is_file():
            raise RuntimeError(f"缺少打包资源: {relative}")
    status, body = asyncio.run(_request_status(app))
    if status != 200 or not isinstance(json.loads(body), dict):
        raise RuntimeError(f"ASGI 冒烟失败: HTTP {status}")
    engine.dispose()
    print(f"SMOKE_OK version={__version__} data={DATA_DIR}")
    return 0
