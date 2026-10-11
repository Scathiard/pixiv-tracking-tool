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
from urllib.parse import unquote, urlsplit
import zipfile


@contextmanager
def _image_source():
    """Make packaged validation exercise the real downloader, not a prepared file."""
    from PIL import Image

    data = BytesIO()
    with Image.new("RGB", (32, 48), "navy") as picture:
        picture.save(data, format="PNG")
    animation_frames = []
    for color in ("navy", "gold"):
        frame = BytesIO()
        with Image.new("RGB", (1280, 960), color) as picture:
            picture.save(frame, format="PNG")
        animation_frames.append(frame.getvalue())
    animation = BytesIO()
    with zipfile.ZipFile(animation, "w") as archive:
        archive.writestr("000000.png", animation_frames[0])
        archive.writestr("000001.png", animation_frames[1])
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
    response_complete = asyncio.Event()

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        # Stay connected while the response is running; cancellation of a
        # disconnect probe must not synthesize a disconnected client.
        await response_complete.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            response_complete.set()

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


def _run_round_to_completion(queue, timeout=30):
    """Respect persisted cooldowns before asserting a synthetic round outcome.

    run_round() returns False while all remaining lanes are temporarily idle;
    that is not a finished round. Keep the real limiter and stop on a finite
    deadline instead of bypassing retries or assuming slow queries consumed it.
    """
    deadline = time.monotonic() + timeout
    while True:
        if queue.run_round():
            return
        if getattr(queue, "worker_error", None):
            raise RuntimeError("synthetic round worker failed before completion")
        if time.monotonic() >= deadline:
            raise TimeoutError("synthetic round did not finish before its smoke deadline")
        time.sleep(.05)


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
    from .core.models import ArchiveIntent, Artist, Asset, Derivative, Operation, Work, WorkTag
    from .version import __version__, __build__
    from .web import create_app
    import requests
    from sqlalchemy import select

    for relative in ("src/templates/base.html", "static/style.css", "static/task-panel.js"):
        if not Path(resource_path(relative)).is_file():
            raise RuntimeError(f"缺少打包资源: {relative}")
    transport = requests.Session()
    transport.trust_env = False  # Synthetic loopback must never use a proxy or account.
    with _image_source() as (source_url, received):
        from PIL import Image
        from .client import PixivClient, PixivRequestError
        from .core.archive import ArchiveHandler
        metadata_reads = []
        detail_reads = []

        def animation_metadata(remote_id):
            metadata_reads.append(remote_id)
            if remote_id == "3":
                raise PixivRequestError(404, "动图资料暂时无法获取", failure_code="ugoira_metadata_unavailable", retryable=False)
            if remote_id != "2":
                raise RuntimeError("隔离验证请求了其他作品的动图原包")
            return {"zip_urls": {"original": source_url.rsplit("/", 1)[0] + "/2.zip"},
                    "frames": [{"file": "000000.png", "delay": 100}, {"file": "000001.png", "delay": 200}]}

        def work_detail(remote_id):
            detail_reads.append(remote_id)
            if remote_id == "3":
                raise PixivRequestError(404, "主站暂时无法获取此作品，尚不能确认是否删除；已有存档保留", failure_code="work_unavailable", retryable=False)
            raise RuntimeError("隔离验证核对了不需要的作品")

        runtime = Runtime(root / "data/pixiv_tracker.db", root / "images", create=True,
            client=SimpleNamespace(get_ugoira_metadata=animation_metadata, get_illust_detail=work_detail, get_account_id=lambda: None),
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
                with Image.new("RGB", (32, 48), "red") as image, Image.new("RGB", (32, 48), "lime") as next_frame:
                    image.save(runtime.images / relative, save_all=True, append_images=[next_frame], duration=[120,160], loop=0)
                legacy = Work(id=work_id, pixiv_illust_id=str(work_id), artist_id=work.artist_id,
                    title=f"legacy animation {work_id}", type="ugoira",
                    source_metadata={"original_backfill_requires_selection": True})
                legacy.assets = [Asset(role="ugoira_zip", state="missing_deferred"), Asset(role="ugoira_timing")]
                legacy.derivatives = [Derivative(kind="gif", relative_path=relative)]
                legacy.tags = [WorkTag(tag="本地动图")]
                session.add_all([legacy, ArchiveIntent(pixiv_illust_id=str(work_id))])
        unavailable_gif = (runtime.images / "3.gif").read_bytes()
        original_legacy_gif = (runtime.images / "2.gif").read_bytes()
        def displayed_file(work_id):
            src = runtime.library.detail(work_id)["pages"][0]["src"]
            if not src or not src.startswith("/images/"):
                raise RuntimeError("默认版本没有可阅览文件")
            return runtime.images / unquote(urlsplit(src).path.removeprefix("/images/"))
        runtime.queue.submit("repair", {"work_ids": [1]}, "smoke")
        _run_round_to_completion(runtime.queue)
        if runtime.queue.snapshot()["round"]["outcome"] != "succeeded":
            raise RuntimeError("打包程序的原图下载、核验或缩略图生成失败")
        first_batch = runtime.queue.submit("repair", {}, "smoke manual animation scope")
        _run_round_to_completion(runtime.queue)
        first_result = runtime.queue.details(first_batch["operation_id"])
        if first_result["result"]["outcome"] != "partial_failure":
            raise RuntimeError("打包程序将不可获取的原包误报为已补齐")
        with runtime.database.read() as session:
            selected = session.get(Work, 2)
            if (runtime.library.detail(2)["archive"]["state"] != "complete"
                    or selected.source_metadata.get("original_backfill_requires_selection")):
                raise RuntimeError("打包程序未通过普通补下载补齐可获取的动图原包与时序")
            if not session.get(Work, 3).source_metadata.get("original_backfill_requires_selection"):
                raise RuntimeError("打包程序将无法获取的动图原包标成已存档")
            if session.get(Work, 3).source_metadata.get("remote_availability", {}).get("state") != "unavailable":
                raise RuntimeError("打包程序未保留核实的作品来源状态")
        retry_batch = runtime.queue.submit("repair", {}, "smoke skip unavailable animation")
        _run_round_to_completion(runtime.queue)
        if runtime.queue.details(retry_batch["operation_id"])["counts"].get("skipped") != 1:
            raise RuntimeError("打包程序未在批量补下载中跳过已知无法获取的原包")
        if metadata_reads != ["2", "3"] or detail_reads != ["3"] or (runtime.images / "3.gif").read_bytes() != unavailable_gif:
            raise RuntimeError("打包程序修改了不可获取动图或重复请求其来源")
        with Image.open(displayed_file(2)) as preview:
            if preview.n_frames != 2 or preview.size != (1280, 960):
                raise RuntimeError("打包程序的动图原包未生成可播放预览")
        compact = next(item for item in runtime.library.query(compact=True)["items"] if item["id"] == 2)
        if not compact["preview"] or "_card_motion_v1.webp?v=" not in compact["preview"]:
            raise RuntimeError("打包程序的图库未使用轻量动画缓存")
        with runtime.database.read() as session:
            motion = session.scalar(select(Derivative).where(Derivative.work_id == 2,
                Derivative.kind == "thumbnail", Derivative.spec_key == "card-motion-v1"))
            motion_path = runtime.images / motion.relative_path
        with Image.open(motion_path) as preview:
            if preview.n_frames != 2 or preview.size != (360, 270):
                raise RuntimeError("打包程序的图库动画未缩小或已丢失播放帧")
        if not str(displayed_file(2)).endswith(".gif") or (runtime.images / "2.gif").read_bytes() != original_legacy_gif:
            raise RuntimeError("打包程序未保留旧 GIF 或详情未使用完整 GIF")
        if received != [True, True]:
            raise RuntimeError("打包程序的图片下载请求未通过兼容性验证")
        parsed = PixivClient._parse_illust({"id": 1, "title": "isolated", "type": "illust", "page_count": 1,
            "image_urls": {}, "meta_single_page": {"original_image_url": source_url},
            "tags": [{"name": "本地单图", "translated_name": "测试单图"}]})
        ArchiveHandler("metadata", runtime.database, runtime.publisher, runtime.remote)._save_remote_work(1, parsed)
        detail = runtime.library.detail(1)
        if (detail["tags"] != ["本地单图"] or detail["tag_details"][0]["translated_name"] != "测试单图"
                or detail["tag_details"][0]["source"] != "pixiv"):
            raise RuntimeError("打包程序未保留合成主站标签译名及原标签身份")
        # Exercise the delivered unified workflow, including a formerly verified
        # original that becomes corrupt. All metadata and media remain synthetic.
        profile_reads, maintenance_reads = [], []

        def maintenance_detail(remote_id):
            maintenance_reads.append(remote_id)
            if remote_id == "3":
                raise PixivRequestError(404, "合成不可访问作品", failure_code="work_unavailable", retryable=False)
            if remote_id not in ("1", "2"):
                raise RuntimeError("统一维护请求了本地清单之外的作品")
            return PixivClient._parse_illust({"id": int(remote_id),
                "title": "isolated" if remote_id == "1" else "legacy animation 2",
                "type": "illust" if remote_id == "1" else "ugoira", "page_count": 1,
                "caption": "", "user": {"id": 1, "name": "smoke"}, "image_urls": {},
                "meta_single_page": {"original_image_url": source_url} if remote_id == "1" else {},
                "tags": [{"name": "本地单图", "translated_name": "测试单图"}] if remote_id == "1" else [{"name": "本地动图"}]})

        def maintenance_profile(remote_id):
            profile_reads.append(remote_id)
            if remote_id != "1":
                raise RuntimeError("主页核对请求了其他画师")
            return {"user_id": "1", "name": "smoke", "biography": "隔离测试，本地保存的画师简介"}

        def forbidden_write(*_args, **_kwargs):
            raise RuntimeError("统一维护不允许写入主站收藏")

        runtime.client = SimpleNamespace(get_account_id=lambda: None, get_illust_detail=maintenance_detail,
            get_artist_detail=maintenance_profile, get_ugoira_metadata=animation_metadata,
            add_bookmark=forbidden_write, delete_bookmark=forbidden_write)
        original_path = displayed_file(1)
        original_bytes = original_path.read_bytes()
        original_path.write_bytes(b"")
        maintenance = runtime.queue.submit("reconcile", {}, "检查并补齐图库")
        _run_round_to_completion(runtime.queue)
        maintenance_result = runtime.queue.details(maintenance["operation_id"])
        if maintenance_result["result"]["outcome"] != "succeeded" or maintenance_result["counts"].get("failed"):
            raise RuntimeError("打包程序统一维护未完成合成本地缺项修复")
        if displayed_file(1).read_bytes() != original_bytes or maintenance_reads != ["1", "2", "3"]:
            raise RuntimeError("统一维护未精确补齐损坏原件，或重复读取作品资料")
        if profile_reads != ["1"] or (runtime.images / "3.gif").read_bytes() != unavailable_gif:
            raise RuntimeError("统一维护未按画师去重，或改动了不可获取作品的本地存档")
        with runtime.database.read() as session:
            operation = session.get(Operation, maintenance["operation_id"])
            if operation.result.get("child_operation_ids"):
                raise RuntimeError("统一维护被错误实现为多个旧维护任务")
        overview = runtime.library.source_overview()
        if ([row["id"] for row in overview["items"]] != [3]
                or overview["counts"]["works"] != {"deleted": 0, "unavailable": 1}
                or overview["items"][0]["local"]["state"] != "incomplete"):
            raise RuntimeError("来源一览未保留已核实的无法访问事实，或丢失本地缺项")
        profile_check = runtime.queue.submit("artist_source", {"artist_ids": [1]}, "核对 smoke 的主站主页")
        _run_round_to_completion(runtime.queue)
        if (runtime.queue.details(profile_check["operation_id"])["result"]["outcome"] != "succeeded"
                or profile_reads != ["1", "1"] or maintenance_reads != ["1", "2", "3"]):
            raise RuntimeError("主页核对未保持独立画师范围")
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
            paths = ["/api/status", "/api/library", "/api/library/source-status", "/api/progress", "/api/settings", "/api/artists",
                     "/api/works/1/bookmark", "/", "/illust/1", "/artists", "/artist/1", "/settings"]
            if reading_ui:
                paths.extend(["/flow", *asset_paths])
            for path in paths:
                status, body = await _request_status(application, path)
                if status != 200 or not body:
                    raise RuntimeError(f"ASGI 冒烟失败: {path} HTTP {status}")
                result[path] = status
        return result

    from .single_instance import ApplicationInstance
    first, second = ApplicationInstance(root / "instance"), ApplicationInstance(root / "instance")
    if not first.acquire() or second.acquire():
        raise RuntimeError("程序级单实例互斥失效")
    try:
        first.mark_ready("http://localhost:1919")
        if second.existing_url(timeout=2) != "http://localhost:1919":
            raise RuntimeError("重复启动未返回已就绪程序入口")
    finally:
        first.close(); second.close()
    successor = ApplicationInstance(root / "instance")
    if not successor.acquire():
        raise RuntimeError("程序关闭后未释放实例所有权")
    successor.close()
    checks = asyncio.run(exercise())
    checks["application_instance_ownership_ready_and_release"] = 200
    checks["original_download_verified_and_thumbnail_published"] = 200
    checks["manual_scope_animation_originals_downloaded_and_verified"] = 200
    checks["unavailable_animation_skipped_without_repeated_requests_gif_retained"] = 200
    checks["bounded_card_animation_published_original_gif_retained"] = 200
    checks["tag_translation_parsed_saved_and_cached"] = 200
    checks["unified_maintenance_repairs_corrupt_original_in_one_operation"] = 200
    checks["unified_snapshot_reads_once_and_never_writes_bookmarks"] = 200
    checks["source_overview_uses_effective_facts_and_lists_specific_local_gaps"] = 200
    checks["artist_homepage_check_has_no_work_or_download_side_effects"] = 200
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
