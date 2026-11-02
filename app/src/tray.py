"""系统托盘模块。在 Windows 托盘区显示图标，Web 服务后台运行。"""
import sys
import threading
import webbrowser
from pathlib import Path
from PIL import Image
import pystray
from .core import diagnostics, lifecycle
from .core.stdio_log import open_stdio_log


def _resource_path(relative_path):
    """获取资源文件路径，兼容 PyInstaller 打包。"""
    if getattr(sys, 'frozen', False):
        return str(Path(sys._MEIPASS) / relative_path)
    return str(Path(__file__).parent.parent / relative_path)


def _redirect_stdio(log_dir: Path):
    """持续追加普通日志，为新输出逐行记录本地时间。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    stream = open_stdio_log(log_dir / "tracker.log")
    sys.stdout = stream
    sys.stderr = stream


def _open_gallery():
    from src.config import LISTENING_PORT as PORT
    webbrowser.open_new(f"http://localhost:{PORT}")


def _open_settings():
    from src.config import LISTENING_PORT as PORT
    webbrowser.open_new(f"http://localhost:{PORT}/settings")


def _quit_app(icon: pystray.Icon):
    if getattr(icon, "_quit_requested", False):
        return
    diagnostics.emit("tray.quit_requested")
    lifecycle.emit("quit_requested", source="tray_menu")
    icon._quit_requested = True
    icon.title = "PixivTracker · 正在退出"
    try:
        icon.notify("正在退出：等待当前读写收尾，未完成任务下次继续。", "Pixiv Tracker")
    except Exception:
        pass
    from src.web import request_shutdown
    request_shutdown()
    done = getattr(icon, "_shutdown_complete", None)
    if done is None:
        icon.stop()
        return

    def finish():
        with diagnostics.span("tray.wait_service_exit"):
            done.wait()
        icon.stop()

    threading.Thread(target=finish, name="tray-shutdown", daemon=True).start()


def _create_icon():
    from src.version import DISPLAY_VERSION
    icon_path = _resource_path("static/icon.png")
    img = Image.open(icon_path)
    # pystray 需要合适尺寸的图标，保持宽高比缩放
    img = img.resize((64, 64), Image.LANCZOS)

    menu = pystray.Menu(
        pystray.MenuItem("打开 Pixiv 画廊", _open_gallery, default=True),
        pystray.MenuItem("设置", _open_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _quit_app),
    )

    return pystray.Icon(
        "pixiv_tracker",
        img,
        f"PixivTracker {DISPLAY_VERSION}",
        menu,
    )


def run_tray(*, offline=False, instance=None):
    """主线程运行托盘；退出时等待服务保存当前工作并释放图库。"""
    from src.config import PORT, DATA_DIR
    from src.version import DISPLAY_VERSION

    # 日志重定向（必须在任何 print 之前）
    if getattr(sys.stdout, "name", None) != str(DATA_DIR / "tracker.log"):
        _redirect_stdio(DATA_DIR)

    print(f"[tray] PixivTracker {DISPLAY_VERSION} 启动")
    print(f"[tray] 端口: {PORT}")
    if offline:
        print("[tray] 离线模式：不登录 Pixiv，不启动自动检查")

    web_done = threading.Event()
    service_failed = threading.Event()
    icon = _create_icon()
    icon._shutdown_complete = web_done
    def ready():
        lifecycle.emit("service_ready", port=PORT, offline=offline)
        if instance is not None:
            instance.mark_ready(f"http://localhost:{PORT}")
        _open_gallery()
    # 保持服务线程存活，直到安全收尾完成。
    def _start_web():
        shutdown_requested = None
        lifecycle.emit("service_start", offline=offline)
        try:
            from src.web import main, _shutdown_requested
            shutdown_requested = _shutdown_requested
            if not shutdown_requested.wait(1):  # 等托盘图标就位，可被退出唤醒
                main(offline=offline, ready_callback=ready)
        except BaseException as exc:
            service_failed.set()
            lifecycle.exception("web_thread", exc)
        finally:
            expected = shutdown_requested is not None and shutdown_requested.is_set()
            if not expected:
                service_failed.set()
            lifecycle.emit("service_stopped", expected=expected,
                           outcome="failed" if service_failed.is_set() else "ok")
            web_done.set()
            # A failed bind/lifespan must not leave an orphan tray owning the
            # application lock and preventing the next corrected launch.
            icon.stop()

    web_thread = threading.Thread(target=_start_web, daemon=False)
    web_thread.start()

    # 主线程运行托盘图标
    tray_failed = False
    try:
        icon.run()
    except BaseException:
        tray_failed = True
        raise
    finally:
        source = "tray_error" if tray_failed else "tray_menu" if getattr(icon, "_quit_requested", False) else (
            "service_exit" if web_done.is_set() else "tray_window_message")
        lifecycle.emit("tray_loop_stopped", source=source)
        from src.web import request_shutdown
        request_shutdown()
        with diagnostics.span("tray.join_web_thread"):
            web_thread.join()
    return 1 if service_failed.is_set() else 0
