"""发布 EXE 的隔离冒烟入口，仅连接本机合成图片服务。"""
from __future__ import annotations

import asyncio
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import zipfile


@contextmanager
def _image_source():
    """Make packaged validation exercise the real downloader, not a prepared file."""
    from PIL import Image

    data = BytesIO()
    with Image.new("RGB", (32, 48), "navy") as picture:
        picture.save(data, format="PNG")
    animation = BytesIO()
    with zipfile.ZipFile(animation, "w") as archive:
        archive.writestr("000000.png", data.getvalue())
    payloads = {"/1_p0.png": data.getvalue(), "/2.zip": animation.getvalue()}
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            accepted = (self.path in payloads
                        and self.headers.get("User-Agent", "").startswith("PixivIOSApp/")
                        and self.headers.get("Referer") == "https://www.pixiv.net/"
                        and not self.headers.get("Authorization"))
            received.append(accepted)
            payload = payloads.get(self.path, b"")
            self.send_response(200 if accepted else 403)
            self.send_header("Content-Length", str(len(payload) if accepted else 0))
            self.end_headers()
            if accepted:
                self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/1_p0.png", received
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


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
    from .core.download import AssetDownloader
    from .core.models import ArchiveIntent, Artist, Asset, Derivative, Work, WorkTag
    from .version import __version__, __build__
    from .web import create_app
    import requests

    for relative in ("src/templates/base.html", "static/style.css", "static/task-panel.js"):
        if not Path(resource_path(relative)).is_file():
            raise RuntimeError(f"缺少打包资源: {relative}")
    transport = requests.Session()
    transport.trust_env = False  # Synthetic loopback must never use a proxy or account.
    with _image_source() as (source_url, received):
        from PIL import Image
        metadata_reads = []

        def animation_metadata(remote_id):
            metadata_reads.append(remote_id)
            if remote_id != "2":
                raise RuntimeError("隔离验证请求了未选择的动图原包")
            return {"zip_urls": {"original": source_url.rsplit("/", 1)[0] + "/2.zip"},
                    "frames": [{"file": "000000.png", "delay": 80}]}

        runtime = Runtime(root / "data/pixiv_tracker.db", root / "images", create=True,
            client=SimpleNamespace(get_ugoira_metadata=animation_metadata, get_account_id=lambda: None),
            downloader=AssetDownloader(session=transport, allowed_hosts=("127.0.0.1",), allow_http=True))
        with runtime.database.write() as session:
            work = Work(id=1, pixiv_illust_id="1", artist=Artist(pixiv_user_id="1", name="smoke"), title="isolated")
            work.assets = [Asset(role="image", relative_path="1_p0.png", state="failed", source_url=source_url,
                                error="图片来源拒绝访问（403），原因待核查")]
            work.tags = [WorkTag(tag="本地单图")]
            session.add_all([work, ArchiveIntent(pixiv_illust_id="1")])
            session.flush()
            for work_id in (2, 3):
                relative = f"{work_id}.gif"
                with Image.new("RGB", (32, 48), "red") as image:
                    image.save(runtime.images / relative)
                legacy = Work(id=work_id, pixiv_illust_id=str(work_id), artist_id=work.artist_id,
                    title=f"legacy animation {work_id}", type="ugoira",
                    source_metadata={"original_backfill_requires_selection": True})
                legacy.assets = [Asset(role="ugoira_zip", state="missing_deferred"), Asset(role="ugoira_timing")]
                legacy.derivatives = [Derivative(kind="gif", relative_path=relative)]
                legacy.tags = [WorkTag(tag="本地动图")]
                session.add_all([legacy, ArchiveIntent(pixiv_illust_id=str(work_id))])
        unselected_gif = (runtime.images / "3.gif").read_bytes()
        runtime.queue.submit("repair", {"work_ids": [1]}, "smoke")
        runtime.queue.run_round()
        if runtime.queue.snapshot()["round"]["outcome"] != "succeeded":
            raise RuntimeError("打包程序的原图下载、核验或缩略图生成失败")
        runtime.queue.submit("repair", {"work_ids": [2]}, "smoke selected animation")
        runtime.queue.run_round()
        with runtime.database.read() as session:
            selected = session.get(Work, 2)
            if (not all(asset.state == "verified" for asset in selected.assets)
                    or selected.source_metadata.get("original_backfill_requires_selection")):
                raise RuntimeError("打包程序未补齐所选动图原包与时序")
            if not session.get(Work, 3).source_metadata.get("original_backfill_requires_selection"):
                raise RuntimeError("打包程序擅自选择了另一件动图原包")
        if metadata_reads != ["2"] or (runtime.images / "3.gif").read_bytes() != unselected_gif:
            raise RuntimeError("打包程序修改了未选动图或请求了其他原包")
        if received != [True, True]:
            raise RuntimeError("打包程序的图片下载请求未通过兼容性验证")
    transport.close()
    if runtime.queue.snapshot()["round"]["outcome"] != "succeeded":
        raise RuntimeError("打包程序的下载、原件核验或缩略图生成失败")
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
    checks["original_download_verified_and_thumbnail_published"] = 200
    checks["selected_animation_originals_downloaded_and_verified"] = 200
    checks["unselected_legacy_animation_remains_deferred"] = 200
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
