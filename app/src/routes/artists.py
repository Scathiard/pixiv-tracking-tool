from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import func

from ..models import Session, TrackedArtist, Illustration
from .. import config as _cfg
from ..web import get_tracker, get_client, templates
from ..tracker import _sync_artist_name

router = APIRouter(prefix="/artists", tags=["artists"])


def _artist_counts(session, artists):
    artist_ids = [artist.id for artist in artists]
    if not artist_ids:
        return {}
    rows = (
        session.query(Illustration.artist_id, func.count(Illustration.id))
        .filter(Illustration.artist_id.in_(artist_ids))
        .group_by(Illustration.artist_id)
        .all()
    )
    counts = dict(rows)
    return {artist.id: counts.get(artist.id, 0) for artist in artists}


def _busy_error(request: Request):
    return templates.TemplateResponse(
        request,
        "error.html",
        {"message": "当前有检查、下载或其他写入任务，完成后才能删除。"},
        status_code=409,
    )


@router.get("")
async def list_artists(request: Request, search: str = Query(None)):
    session = Session()
    artists = session.query(TrackedArtist).order_by(TrackedArtist.added_at.desc()).all()
    artist_counts = _artist_counts(session, artists)

    results = []
    if search:
        client = get_client()
        if client:
            if search.strip().isdigit():
                # 按 Pixiv user_id 直接查找
                try:
                    import asyncio
                    info = await asyncio.to_thread(
                        client.get_artist_detail, search.strip()
                    )
                    results = [{
                        "user_id": info["user_id"],
                        "name": info["name"],
                        "account": info["account"],
                        "avatar_url": info["avatar_url"],
                    }]
                except Exception:
                    results = []
            else:
                import asyncio
                results = await asyncio.to_thread(client.search_artist, search)
        else:
            session.close()
            return templates.TemplateResponse(
                request, "artists.html",
                {"artists": artists, "artist_counts": artist_counts, "results": [], "error": "未登录 Pixiv，无法搜索"}
            )

    session.close()
    return templates.TemplateResponse(
        request, "artists.html",
        {"artists": artists, "artist_counts": artist_counts, "results": results, "search_term": search or ""}
    )


def _artists_context(session):
    artists = session.query(TrackedArtist).order_by(TrackedArtist.added_at.desc()).all()
    counts = _artist_counts(session, artists)
    return artists, counts


@router.post("/add")
async def add_artist(request: Request, user_id: str = Form(...)):
    tracker = get_tracker()

    if not tracker:
        session = Session()
        artists, counts = _artists_context(session)
        session.close()
        return templates.TemplateResponse(
            request, "artists.html",
            {"artists": artists, "artist_counts": counts, "results": [], "error": "未登录 Pixiv，无法添加画师"}
        )

    import asyncio
    artist, created = await asyncio.to_thread(tracker.add_artist, user_id)
    if not created:
        session = Session()
        artists, counts = _artists_context(session)
        session.close()
        return templates.TemplateResponse(
            request, "artists.html",
            {"artists": artists, "artist_counts": counts, "results": [], "error": f"画师 {artist.name} 已在特别关注列表中"}
        )

    # 进入统一任务队列，不阻塞页面，也不与其他下载任务重入。
    from .. import progress
    progress.submit_task(
        "fetch_artist",
        lambda: tracker.fetch_artist(artist.id),
        label=f"添加画师：{artist.name}",
        dedupe_key=f"artist-{artist.id}",
    )

    return RedirectResponse("/artists", status_code=303)


@router.post("/{artist_id}/remove")
async def remove_artist(request: Request, artist_id: int):
    tracker = get_tracker()
    if tracker:
        from .. import progress
        _, created = progress.submit_exclusive(
            "remove_artist",
            lambda: tracker.remove_artist(artist_id),
            label="移除画师记录",
        )
        if not created:
            return _busy_error(request)
    return RedirectResponse("/artists", status_code=303)


@router.post("/{artist_id}/remove-keep-files")
async def remove_artist_keep_files(request: Request, artist_id: int):
    tracker = get_tracker()
    if tracker:
        from .. import progress
        _, created = progress.submit_exclusive(
            "remove_artist",
            lambda: tracker.remove_artist_keep_files(artist_id),
            label="移除画师记录（保留文件）",
        )
        if not created:
            return _busy_error(request)
    return RedirectResponse("/artists", status_code=303)


@router.post("/{artist_id}/remove-and-files")
async def remove_artist_and_files(request: Request, artist_id: int):
    tracker = get_tracker()
    if tracker:
        from .. import progress
        _, created = progress.submit_exclusive(
            "remove_artist_and_files",
            lambda: tracker.remove_artist_and_files(artist_id),
            label="移除画师及文件",
        )
        if not created:
            return _busy_error(request)
    return RedirectResponse("/artists", status_code=303)


@router.post("/{artist_id}/toggle")
async def toggle_artist(artist_id: int):
    session = Session()
    artist = session.get(TrackedArtist, artist_id)
    if artist:
        artist.is_active = not artist.is_active
        session.commit()
    session.close()
    return RedirectResponse("/artists", status_code=303)


@router.post("/{artist_id}/refresh")
async def refresh_artist(artist_id: int):
    tracker = get_tracker()
    if tracker:
        from .. import progress
        progress.submit_task(
            "refresh_artist",
            lambda: _do_refresh_artist(tracker, artist_id),
            label="刷新画师",
            dedupe_key=f"artist-{artist_id}",
        )
    return RedirectResponse("/artists", status_code=303)


def _do_refresh_artist(tracker, artist_id):
    from .. import progress

    session = Session()
    artist = session.get(TrackedArtist, artist_id)
    if not artist:
        session.close()
        return

    _sync_artist_name(artist, tracker.client, session)

    task_id = progress.begin_task(artist.name)

    progress.begin_phase(task_id, "checking")
    progress.set_artist_progress(task_id, 1, 1)
    progress.set_detail(task_id, "正在扫描本地文件...")

    tracker._update_file_paths(session, artist)
    tracker._convert_ugoira_zips(session, artist)
    session.commit()

    # 补拉缺失的 caption（用户主动触发的单画师操作，API 开销可控）
    from ..caption_state import should_fetch_caption
    from ..tracker import _caption_path
    no_caption = []
    for ill in artist.illustrations:
        caption_exists = _caption_path(artist, ill.pixiv_illust_id).exists()
        if should_fetch_caption(caption_exists, ill.caption):
            no_caption.append(ill)
    if no_caption:
        progress.begin_phase(task_id, "syncing")
        progress.set_files_total(task_id, len(no_caption))
        progress.set_detail(task_id, "正在补拉作品简介...")
        for i, ill in enumerate(no_caption):
            progress.add_files_done(task_id, 1)
            progress.set_artist(task_id, artist.name)
            tracker._fetch_and_save_caption(ill)
    session.commit()

    pending = (
        session.query(Illustration)
        .filter_by(artist_id=artist.id)
        .filter(Illustration.file_paths.is_(None))
        .all()
    )

    if pending:
        all_illusts = (
            session.query(Illustration)
            .filter_by(artist_id=artist.id).all()
        )
        files_total = sum(i.page_count for i in all_illusts)

        progress.begin_phase(task_id, "downloading")
        progress.set_files_total(task_id, files_total)
        progress.set_dl_artist_progress(task_id, 1, 1)
        progress.set_detail(task_id, f"正在下载 {artist.name} 的作品...")

        safe_name = artist.name
        for ch in r'\/:*?"<>|':
            safe_name = safe_name.replace(ch, '_')
        artist_dir = str(_cfg.IMAGES_DIR / f"{safe_name} {artist.pixiv_user_id}")

        try:
            tracker._download_artist(
                artist.pixiv_user_id,
                artist_dir=artist_dir,
                task_id=task_id,
                clear_archive=True,
            )
        except Exception as e:
            progress.add_error(task_id, f"下载 {artist.name}: {e}")

        tracker._update_file_paths(session, artist)
        tracker._convert_ugoira_zips(session, artist)
        session.commit()

    progress.finish_task(task_id)
    session.close()
