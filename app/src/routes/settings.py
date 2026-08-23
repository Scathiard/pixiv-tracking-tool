from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse

from ..web import get_tracker, templates
from ..models import Session, TrackedArtist, Illustration
from ..config import set_data_root, set_pagination, set_port

router = APIRouter(prefix="/settings", tags=["settings"])


def _settings_context():
    from ..config import CHECK_INTERVAL_HOURS, CHECK_INTERVAL_MINUTES, PORT, DATA_ROOT, \
        PAGE_SIZE, PAGINATION_MODE, MAX_VISIBLE_ITEMS
    from ..networking import access_urls
    session = Session()
    artist_count = session.query(TrackedArtist).count()
    illust_count = session.query(Illustration).count()
    downloaded = session.query(Illustration).filter(Illustration.file_paths != None).count()  # noqa: E711
    session.close()
    return {
        "artist_count": artist_count,
        "illust_count": illust_count,
        "downloaded": downloaded,
        "check_interval_hours": CHECK_INTERVAL_HOURS,
        "check_interval_minutes": CHECK_INTERVAL_MINUTES,
        "port": PORT,
        "data_root": str(DATA_ROOT),
        "page_size": PAGE_SIZE,
        "pagination_mode": PAGINATION_MODE,
        "max_visible_items": MAX_VISIBLE_ITEMS,
        "network_urls": access_urls(PORT),
    }


@router.get("")
async def settings_page(request: Request):
    return templates.TemplateResponse(request, "settings.html", _settings_context())


@router.post("/check-now")
async def check_now():
    tracker = get_tracker()
    if tracker:
        from .. import progress
        progress.submit_task(
            "check_updates",
            tracker.check_updates,
            label="检查更新",
            dedupe_key="check-updates",
        )
    return RedirectResponse("/settings", status_code=303)
@router.post("/download-pending")
async def download_pending():
    tracker = get_tracker()
    if tracker:
        from .. import progress
        progress.submit_task(
            "download_pending",
            tracker.download_pending,
            label="下载待处理作品",
            dedupe_key="download-pending",
        )
    return RedirectResponse("/settings", status_code=303)
@router.post("/change-path")
async def change_path(request: Request, new_path: str = Form(...)):
    new_path = new_path.strip()
    if not new_path:
        return RedirectResponse("/settings", status_code=303)

    from .. import progress
    from ..scheduler import start_scheduler, stop_scheduler

    if not progress.begin_maintenance():
        return JSONResponse(
            {"ok": False, "error": "另一个写操作正在结束，请稍后再迁移"},
            status_code=409,
        )

    if progress.is_busy():
        progress.end_maintenance()
        return JSONResponse(
            {"ok": False, "error": "当前有任务正在运行，请完成后再迁移数据目录"},
            status_code=409,
        )

    tracker = get_tracker()
    stop_scheduler()

    def migrate_and_reload():
        try:
            report = set_data_root(new_path)
            if report is None:
                if tracker:
                    start_scheduler(tracker)
                return "数据目录未变化"
            from ..web import reload_runtime
            reload_runtime()
            return f"迁移完成：{report.image_files} 个图片文件"
        except Exception:
            if tracker:
                start_scheduler(tracker)
            raise
        finally:
            progress.end_maintenance()

    job_id, created = progress.submit_exclusive(
        "data_migration",
        migrate_and_reload,
        label="迁移数据目录",
    )
    if not created:
        progress.end_maintenance()
        if tracker:
            start_scheduler(tracker)
        return JSONResponse(
            {"ok": False, "error": "当前有任务正在运行，请完成后再迁移数据目录"},
            status_code=409,
        )

    import asyncio
    job = await asyncio.to_thread(progress.wait_for_job, job_id, None)
    if not job or job["status"] != "succeeded":
        error = job["errors"][-1] if job and job["errors"] else "未知错误"
        return JSONResponse({"ok": False, "error": f"迁移失败: {error}"}, status_code=500)
    return JSONResponse({"ok": True, "job_id": job_id, "message": job["result"]})


@router.post("/validate-and-fix")
async def validate_and_fix():
    """校验并补全所有已有作品信息：caption、收藏状态、缺失文件。后台运行。"""
    tracker = get_tracker()
    if tracker:
        from .. import progress
        progress.submit_task(
            "validate_and_fix",
            tracker.validate_and_fix_all,
            label="校验并补全作品信息",
            dedupe_key="validate-and-fix",
        )
    return RedirectResponse("/settings", status_code=303)


@router.post("/pagination")
async def update_pagination(page_size: int = Form(200), pagination_mode: str = Form("scroll"),
                            max_visible_items: int = Form(0)):
    """更新翻页设置并持久化到 .env。"""
    set_pagination(page_size, pagination_mode, max_visible_items)
    return RedirectResponse("/settings", status_code=303)


@router.post("/port")
async def update_port(port: int = Form(...)):
    """更新端口号，需要重启才能生效。"""
    try:
        set_port(port)
        return JSONResponse({"ok": True, "message": f"端口已改为 {port}，请手动重启程序使其生效。"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.post("/browse-path")
async def browse_path():
    """弹出原生文件夹选择对话框，返回所选路径。"""
    import asyncio
    import tkinter as tk
    from tkinter import filedialog
    from .. import config as _cfg

    result = {"path": None}

    def _open_dialog():
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(
            parent=root,
            initialdir=str(_cfg.DATA_ROOT),
            title="选择数据存储目录",
        )
        root.destroy()
        result["path"] = path

    await asyncio.to_thread(_open_dialog)
    return JSONResponse(result)
