"""系统托盘模块。在 Windows 托盘区显示图标，Web 服务后台运行。"""
import sys
import threading
import webbrowser
from pathlib import Path
from PIL import Image
import pystray


def _resource_path(relative_path):
    """获取资源文件路径，兼容 PyInstaller 打包。"""
    if getattr(sys, 'frozen', False):
        return str(Path(sys._MEIPASS) / relative_path)
    return str(Path(__file__).parent.parent / relative_path)


def _redirect_stdio(log_dir: Path):
    """将 stdout/stderr 重定向到日志文件。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "tracker.log"

    # 限制日志文件大小：超过 1MB 截断保留后半
    if log_path.exists() and log_path.stat().st_size > 1_048_576:
        old = log_path.read_text(encoding='utf-8', errors='replace')
        log_path.write_text(old[-512_000:], encoding='utf-8')

    f = open(log_path, 'a', encoding='utf-8', buffering=1)
    sys.stdout = f
    sys.stderr = f


def _open_gallery():
    from src.config import LISTENING_PORT as PORT
    webbrowser.open_new(f"http://localhost:{PORT}")


def _open_settings():
    from src.config import LISTENING_PORT as PORT
    webbrowser.open_new(f"http://localhost:{PORT}/settings")


def _quit_app(icon: pystray.Icon):
    if getattr(icon, "_quit_requested", False):
        return
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
    _redirect_stdio(DATA_DIR)

    print(f"[tray] PixivTracker {DISPLAY_VERSION} 启动")
    print(f"[tray] 端口: {PORT}")
    if offline:
        print("[tray] 离线模式：不登录 Pixiv，不启动自动检查")

    web_done = threading.Event()
    icon = _create_icon()
    icon._shutdown_complete = web_done
    def ready():
        if instance is not None:
            instance.mark_ready(f"http://localhost:{PORT}")
        _open_gallery()
    # 保持服务线程存活，直到安全收尾完成。
    def _start_web():
        from src.web import main, _shutdown_requested
        try:
            if not _shutdown_requested.wait(1):  # 等托盘图标就位，可被退出唤醒
                main(offline=offline, ready_callback=ready)
        finally:
            web_done.set()
            # A failed bind/lifespan must not leave an orphan tray owning the
            # application lock and preventing the next corrected launch.
            icon.stop()

    web_thread = threading.Thread(target=_start_web, daemon=False)
    web_thread.start()

    # 主线程运行托盘图标
    icon.run()
    from src.web import request_shutdown
    request_shutdown()
    web_thread.join()
