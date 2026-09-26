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
    from src import progress
    from src.web import get_tracker
    current = get_tracker()

    if (current is not None and current.busy()) or progress.maintenance_active():
        try:
            icon.notify("当前任务尚未完成，请等待进度结束后再退出。", "Pixiv Tracker")
        except Exception:
            pass
        return
    from src.web import request_shutdown
    request_shutdown()
    icon.stop()


def _create_icon():
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
        "Pixiv Tracking Tool",
        menu,
    )


def run_tray(*, offline=False):
    """系统托盘入口。在主线程运行 pystray，Web 服务在 daemon 线程启动。"""
    from src.config import PORT, DATA_DIR
    from src.version import __version__

    # 日志重定向（必须在任何 print 之前）
    _redirect_stdio(DATA_DIR)

    print(f"[tray] Pixiv Tracking Tool v{__version__} 启动")
    print(f"[tray] 端口: {PORT}")
    if offline:
        print("[tray] 离线模式：不登录 Pixiv，不启动自动检查")

    # 在 daemon 线程启动 Web 服务
    def _start_web():
        import time
        time.sleep(1)  # 等托盘图标就位
        from src.web import main
        main(offline=offline)

    web_thread = threading.Thread(target=_start_web, daemon=False)
    web_thread.start()

    # 自动打开浏览器
    def _auto_browser():
        import time
        time.sleep(3)
        _open_gallery()

    threading.Thread(target=_auto_browser, daemon=True).start()

    # 主线程运行托盘图标
    icon = _create_icon()
    icon.run()
    from src.web import request_shutdown
    request_shutdown()
    web_thread.join()
