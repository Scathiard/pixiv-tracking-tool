import asyncio
import sys
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import PORT, resource_path
from . import config as _cfg
from .models import init_db
from .auth import auth, cancel_auth, get_pending_login_url, submit_oauth_code, validate_oauth_input
from .client import PixivClient
from .tracker import Tracker, rebuild_all_file_paths
from .scheduler import start_scheduler, stop_scheduler
from .security import RequestBoundaryMiddleware, resolve_image_path
from .networking import access_urls

templates = Jinja2Templates(directory=resource_path("src/templates"))

# 全局实例，在 startup 时初始化
tracker: Tracker = None
_server = None
_shutdown_requested = threading.Event()


def get_tracker():
    return tracker


def get_client():
    return PixivClient() if tracker else None


def reload_runtime():
    """在数据目录切换后重建数据库引擎、tracker 和调度器。"""
    global tracker
    stop_scheduler()
    tracker = None
    from .models import reinit_db
    reinit_db()
    tracker = Tracker()
    start_scheduler(tracker)
    templates.env.globals["pixiv_logged_in"] = True


async def _init_auth():
    """后台执行 Pixiv 认证，完成后更新全局状态。"""
    global tracker
    try:
        await asyncio.to_thread(auth)
        tracker = Tracker()
        start_scheduler(tracker)
        templates.env.globals["pixiv_logged_in"] = True
        print("[OK] Pixiv 登录成功，追踪功能已就绪")
    except Exception as e:
        print(f"[warn] Pixiv 未登录: {e}")
        print("[warn] Web 界面可用，但追踪/下载功能需登录后使用")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tracker

    init_db()

    from .routes import artists, works, settings
    app.include_router(artists.router)
    app.include_router(works.router)
    app.include_router(settings.router)

    templates.env.globals["pixiv_logged_in"] = False
    from . import progress
    progress.submit_task(
        "startup_file_index",
        rebuild_all_file_paths,
        label="启动时重建文件索引",
        dedupe_key="startup-file-index",
    )
    auth_task = asyncio.create_task(_init_auth())

    yield

    stop_scheduler()
    from . import progress
    progress.shutdown(wait=False)
    cancel_auth()
    auth_task.cancel()


_release_mode = getattr(sys, "frozen", False)
app = FastAPI(
    title="Pixiv Tracking Tool",
    lifespan=lifespan,
    docs_url=None if _release_mode else "/docs",
    redoc_url=None if _release_mode else "/redoc",
    openapi_url=None if _release_mode else "/openapi.json",
)
app.add_middleware(RequestBoundaryMiddleware)
app.mount("/static", StaticFiles(directory=resource_path("static")), name="static")


@app.get("/api/status")
async def api_status():
    from fastapi.responses import JSONResponse
    login_url = get_pending_login_url()
    return JSONResponse({
        "logged_in": tracker is not None,
        "login_url": login_url,
    })


@app.get("/api/progress")
async def api_progress():
    from . import progress
    from fastapi.responses import JSONResponse
    return JSONResponse(progress.get_state())


@app.post("/api/submit-code")
async def api_submit_code(data: dict):
    from fastapi.responses import JSONResponse
    code, error = validate_oauth_input(data.get("code", ""))
    if error:
        return JSONResponse({"ok": False, "error": error})
    submit_oauth_code(code)
    return JSONResponse({"ok": True})


@app.post("/api/sync-bookmarks")
async def api_sync_bookmarks():
    """一次性全量同步所有活跃画师作品的收藏状态。"""
    from fastapi.responses import JSONResponse
    global tracker
    if not tracker:
        return JSONResponse({"ok": False, "error": "未登录 Pixiv"}, status_code=503)
    from . import progress
    job_id, created = progress.submit_task(
        "sync_bookmarks",
        tracker.sync_all_bookmarks,
        label="同步收藏状态",
        dedupe_key="sync-bookmarks",
    )
    return JSONResponse({"ok": True, "job_id": job_id, "created": created})


# 动态服务图片文件（支持自定义路径）
@app.get("/images/{path:path}")
async def serve_image(path: str):
    file_path = resolve_image_path(_cfg.IMAGES_DIR, path)
    if file_path:
        return FileResponse(str(file_path))
    from starlette.responses import Response
    return Response(status_code=404)


def _print_startup_urls():
    """启动后打印本机可访问链接。"""
    print("\n  Pixiv Tracking Tool 已启动")
    print(f"  本地访问: http://localhost:{PORT}")
    for label, url in access_urls(PORT):
        print(f"  {label}: {url}")

    print()


def main():
    import uvicorn
    import threading
    import socket as _socket

    global _server

    if _shutdown_requested.is_set():
        return

    def _after_start():
        import time
        time.sleep(1.5)
        _print_startup_urls()

    threading.Thread(target=_after_start, daemon=True).start()

    # 开发版与发布版使用同一双 socket 路径，保证默认行为一致。
    config = uvicorn.Config(app, host=None, port=PORT, loop="asyncio", log_level="warning")
    server = uvicorn.Server(config)

    socks = []
    try:
        s_v6 = _socket.socket(_socket.AF_INET6, _socket.SOCK_STREAM)
        s_v6.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        s_v6.setsockopt(_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY, 1)
        s_v6.bind(('::', PORT))
        s_v6.listen(2048)
        socks.append(s_v6)
    except Exception as e:
        print(f"  [warn] IPv6 绑定失败: {e}")

    try:
        s_v4 = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s_v4.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        s_v4.bind(('0.0.0.0', PORT))
        s_v4.listen(2048)
        socks.append(s_v4)
    except Exception as e:
        print(f"  [warn] IPv4 绑定失败: {e}")

    if not socks:
        print("  [ERROR] 无法绑定任何地址，退出")
        return

    _server = server
    if _shutdown_requested.is_set():
        server.should_exit = True
    try:
        server.run(sockets=socks)
    finally:
        _server = None
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass


def request_shutdown():
    """请求 Web 服务走 Uvicorn lifespan 正常退出。"""
    _shutdown_requested.set()
    if _server is not None:
        _server.should_exit = True


if __name__ == "__main__":
    main()
