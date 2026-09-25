import asyncio
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import PORT, resource_path
from . import config as _cfg
from .auth import cancel_auth, get_pending_login_url, submit_oauth_code, validate_oauth_input
from .client import PixivClient
from .core.database import UpgradeRequired
from .core.runtime import Runtime
from .scheduler import start_scheduler, stop_scheduler
from .security import RequestBoundaryMiddleware
from .networking import access_urls

templates = Jinja2Templates(directory=resource_path("src/templates"))
_server = None
_shutdown_requested = threading.Event()


def get_tracker():
    return getattr(app.state, "runtime", None)


def get_client():
    current = get_tracker()
    return current.client if current else None


def reload_runtime():
    raise RuntimeError("新版目录切换必须使用准备、核验和激活流程")


def create_app(runtime_factory=None, *, authenticate=True, schedule=True):
    from .routes.archive_api import create_router
    from .routes.archive_pages import create_pages

    def default_runtime():
        path = _cfg.DATA_DIR / "pixiv_tracker.db"
        return Runtime(path, _cfg.IMAGES_DIR, create=not path.exists())

    async def connect(interactive=False):
        from .auth import _try_refresh_token, auth
        current = application.state.runtime
        if current is None:
            return
        try:
            if interactive:
                await asyncio.to_thread(auth)
                connected = True
            else:
                connected = bool(await asyncio.to_thread(_try_refresh_token))
            if connected:
                current.client = PixivClient()
                application.state.login_error = None
                if schedule:
                    start_scheduler(current)
        except Exception:
            application.state.login_error = "Pixiv 暂时无法连接，本地图库可继续使用"

    @asynccontextmanager
    async def lifespan(application):
        current = None
        try:
            current = await asyncio.to_thread(runtime_factory or default_runtime)
            application.state.runtime = current
            await asyncio.to_thread(current.start)
            if schedule and current.client is not None:
                start_scheduler(current)
            if authenticate and current.client is None:
                application.state.auth_task = asyncio.create_task(connect())
        except UpgradeRequired:
            application.state.upgrade_required = True
        except BaseException:
            if current is not None:
                await asyncio.to_thread(current.close)
            application.state.runtime = None
            raise
        try:
            yield
        finally:
            if schedule:
                stop_scheduler()
            if authenticate:
                cancel_auth()
                if application.state.auth_task:
                    application.state.auth_task.cancel()
            current = application.state.runtime
            if current is not None:
                await asyncio.to_thread(current.close)
            application.state.runtime = None

    release = getattr(sys, "frozen", False)
    application = FastAPI(title="Pixiv Tracking Tool", lifespan=lifespan,
        docs_url=None if release else "/docs", redoc_url=None if release else "/redoc",
        openapi_url=None if release else "/openapi.json")
    application.state.runtime = None
    application.state.auth_task = None
    application.state.login_error = None
    application.state.upgrade_required = False
    application.add_middleware(RequestBoundaryMiddleware)
    application.mount("/static", StaticFiles(directory=resource_path("static")), name="static")

    def current_runtime():
        return application.state.runtime

    def switch_root(new_path):
        from . import progress
        from .core.relocation import prepare_relocation
        from .data_migration import persist_data_root
        current = current_runtime()
        if current is None:
            return JSONResponse({"ok": False, "error": "图库尚未准备好"}, 503)
        auth_task = application.state.auth_task
        if auth_task is not None and not auth_task.done():
            return JSONResponse({"ok": False, "error": "登录处理中，请完成后再切换目录"}, 409)
        if not progress.begin_maintenance():
            return JSONResponse({"ok": False, "error": "另一个操作正在完成，请稍后再试"}, 409)
        candidate = None
        committed = False
        try:
            current.queue.accept_submissions(False)
            if schedule:
                stop_scheduler()
            target = Path(new_path).expanduser()
            if not target.is_absolute():
                target = _cfg.PROJECT_ROOT / target
            if target.resolve() == _cfg.DATA_ROOT.resolve():
                return {"ok": True, "message": "数据目录未变化"}
            candidate, report = prepare_relocation(current, _cfg.DATA_ROOT, target)
            candidate.start()
            candidate.queue.accept_submissions(True)
            # All copy/hash/database/startup checks precede the only commit point.
            persist_data_root(_cfg.env_file, target)
            committed = True
            application.state.runtime = candidate
            _cfg.DATA_ROOT = target.resolve()
            _cfg.DATA_DIR, _cfg.IMAGES_DIR = _cfg.DATA_ROOT / "data", _cfg.DATA_ROOT / "images"
            _cfg.DATABASE_URL = f"sqlite:///{_cfg.DATA_DIR / 'pixiv_tracker.db'}"
            os.environ["DATA_ROOT"] = str(_cfg.DATA_ROOT)
            current.close()
            if schedule and candidate.client is not None:
                start_scheduler(candidate)
            return {"ok": True, "message": f"已切换，核对了 {report.image_files} 个图片文件；旧目录保留"}
        except Exception as exc:
            if not committed and candidate is not None:
                candidate.close()
            message = "目录已切换，但后台服务需要重启" if committed else getattr(exc, "user_message", str(exc))
            return JSONResponse({"ok": False, "error": message}, 500)
        finally:
            if not committed:
                current.queue.accept_submissions(True)
                if schedule and current.client is not None:
                    start_scheduler(current)
            progress.end_maintenance()

    application.include_router(create_router(current_runtime))
    application.include_router(create_pages(current_runtime, templates, _cfg, switch_root=switch_root))

    @application.get("/api/status")
    def status():
        current = current_runtime()
        return {"logged_in": current is not None and current.client is not None,
                "login_url": get_pending_login_url() if authenticate else None,
                "login_error": application.state.login_error,
                "upgrade_required": application.state.upgrade_required,
                "listening_port": _cfg.LISTENING_PORT, "saved_port": _cfg.PORT}

    @application.post("/api/login")
    async def login():
        if not authenticate:
            return JSONResponse({"ok": False, "error": "隔离验证实例不连接真实账号"}, 503)
        if application.state.auth_task and not application.state.auth_task.done():
            return {"ok": True, "message": "登录正在进行"}
        application.state.auth_task = asyncio.create_task(connect(True))
        return {"ok": True, "message": "已开始登录，本地图库可继续使用"}

    @application.post("/api/submit-code")
    def submit_code(data: dict):
        if not authenticate:
            return JSONResponse({"ok": False, "error": "隔离验证实例不接收账号凭据"}, 503)
        code, error = validate_oauth_input(data.get("code", ""))
        if error:
            return JSONResponse({"ok": False, "error": error}, 422)
        submit_oauth_code(code)
        return {"ok": True}

    @application.exception_handler(ValueError)
    async def invalid_input(request, exc):
        return JSONResponse({"ok": False, "error": "参数无效，请检查后再试"}, 422)

    @application.exception_handler(KeyError)
    async def missing_record(request, exc):
        return JSONResponse({"ok": False, "error": "记录不存在"}, 404)

    return application


app = create_app()


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
