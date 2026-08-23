from fastapi import APIRouter, Request, Query, Form
from fastapi.responses import RedirectResponse, JSONResponse

from ..models import Session, TrackedArtist, Illustration, IllustrationTag
from ..file_index import decode_file_paths
from ..query_state import (
    build_local_url,
    parse_csv_choices,
    parse_csv_ints,
    safe_redirect_target,
)
from ..web import templates, get_tracker

router = APIRouter(tags=["works"])

TYPE_LABELS = {"illust": "插画", "manga": "漫画", "ugoira": "动图"}


def _ids_str(ids_set):
    return ",".join(str(x) for x in sorted(ids_set))


def reset_tag_cache():
    """保留旧调用点；v0.1.1 已移除会串筛选条件的全局 tag 缓存。"""
    return None


def _get_tags(session, query):
    """按当前完整筛选即时聚合 tag，避免跨请求复用错误结果。"""
    # 取最近 500 件作品（受筛选条件限制）的 tag 去重
    sub = session.query(IllustrationTag.illustration_id)\
        .join(Illustration, Illustration.id == IllustrationTag.illustration_id)
    if query.whereclause is not None:
        sub = sub.filter(query.whereclause)
    sub = sub.order_by(Illustration.posted_at.desc()).limit(500)
    tag_query = session.query(IllustrationTag.tag).distinct()\
        .filter(IllustrationTag.illustration_id.in_(sub))
    return sorted(r[0] for r in tag_query.all())


def _get_cached_artists(session):
    """读取当前画师列表；查询很小，正确性优先于跨请求 ORM 缓存。"""
    return session.query(TrackedArtist).filter_by(is_active=True)\
        .order_by(TrackedArtist.name).all()


@router.get("/")
async def index(request: Request, artist_ids: str = Query(None), types: str = Query(None),
                show_hidden: str = Query(None),
                offset: int = Query(0, ge=0), fragment: str = Query(None),
                page: int = Query(None, ge=1),
                period: str = Query(None),
                posted_after: str = Query(None),
                posted_before: str = Query(None),
                tags: str = Query(None)):
    show_hidden = show_hidden in ("1", "true")
    from .. import config as _cfg
    from datetime import datetime, timedelta

    page_size = _cfg.PAGE_SIZE
    if page is not None and page > 0:
        offset = (page - 1) * page_size

    session = Session()
    all_artists = _get_cached_artists(session)
    all_artist_ids = {a.id for a in all_artists}

    selected_ids = parse_csv_ints(artist_ids)
    if selected_ids is not None:
        selected_ids &= all_artist_ids
    selected_types = parse_csv_choices(types, set(TYPE_LABELS))

    query = session.query(Illustration).order_by(Illustration.posted_at.desc())
    if selected_ids is not None:
        query = query.filter(Illustration.artist_id.in_(selected_ids))
    if selected_types is not None:
        query = query.filter(Illustration.type.in_(selected_types))
    if not show_hidden:
        query = query.filter(Illustration.is_hidden.is_not(True))

    # 投稿期间筛选
    if period and period != 'custom':
        now = datetime.utcnow()
        delta_map = {
            '24h': timedelta(hours=24),
            'week': timedelta(days=7),
            'month': timedelta(days=30),
            'half_year': timedelta(days=183),
            'year': timedelta(days=365),
        }
        if period in delta_map:
            query = query.filter(Illustration.posted_at >= now - delta_map[period])
    elif period == 'custom':
        if posted_after:
            query = query.filter(Illustration.posted_at >= posted_after)
        if posted_before:
            query = query.filter(Illustration.posted_at <= posted_before + 'T23:59:59')

    # Tag 筛选（OR 逻辑，走 illustration_tag 索引）
    selected_tags = set()
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        selected_tags = set(tag_list)
        tag_subquery = session.query(IllustrationTag.illustration_id).filter(IllustrationTag.tag.in_(tag_list))
        query = query.filter(Illustration.id.in_(tag_subquery))

    # Fetch +1 extra to check has_more without COUNT query
    illustrations = query.offset(offset).limit(page_size + 1).all()
    has_more = len(illustrations) > page_size
    if has_more:
        illustrations = illustrations[:page_size]

    illust_artist_ids = {i.artist_id for i in illustrations}
    artists_map = {}
    if illust_artist_ids:
        artists_list = session.query(TrackedArtist).filter(TrackedArtist.id.in_(illust_artist_ids)).all()
        artists_map = {a.id: a for a in artists_list}
    # Fragment: 提前返回，跳过所有后续重计算
    if fragment == "1":
        detail_query = build_local_url("", {
            "artist_ids": _ids_str(selected_ids) if selected_ids is not None else None,
            "types": _ids_str(selected_types) if selected_types is not None else None,
            "show_hidden": "1" if show_hidden else None,
            "period": period,
            "posted_after": posted_after,
            "posted_before": posted_before,
            "tags": tags,
        })
        resp = templates.TemplateResponse(request, "gallery_fragment.html", {
            "illustrations": illustrations,
            "artists_map": artists_map,
            "type_labels": TYPE_LABELS,
            "show_hidden": show_hidden,
            "selected_artist_ids": selected_ids,
            "selected_types": selected_types,
            "detail_query": detail_query,
        })
        session.close()
        resp.headers["X-Has-More"] = "1" if has_more else "0"
        return resp

    # ── 以下仅完整页面请求执行 ──

    total_count = query.count()

    tag_list_all = _get_tags(session, query)
    session.close()

    def _index_url(artist_value=None, type_value=None):
        return build_local_url("/", {
            "artist_ids": artist_value,
            "types": type_value,
            "show_hidden": "1" if show_hidden else None,
            "period": period,
            "posted_after": posted_after,
            "posted_before": posted_before,
            "tags": tags,
        })

    # 画师 toggle
    toggle_urls = {}
    if selected_ids is None:
        for a in all_artists:
            others = all_artist_ids - {a.id}
            toggle_urls[a.id] = _index_url(
                _ids_str(others),
                _ids_str(selected_types) if selected_types is not None else None,
            )
    else:
        for a in all_artists:
            new_set = selected_ids.copy()
            new_set.discard(a.id) if a.id in new_set else new_set.add(a.id)
            art_p = None if new_set == all_artist_ids else _ids_str(new_set)
            typ_p = _ids_str(selected_types) if selected_types is not None else None
            toggle_urls[a.id] = _index_url(art_p, typ_p)

    # 类型 toggle
    type_toggle_urls = {}
    all_types = set(TYPE_LABELS.keys())
    if selected_types is None:
        for t in TYPE_LABELS:
            others = all_types - {t}
            art_p = _ids_str(selected_ids) if selected_ids is not None else None
            type_toggle_urls[t] = _index_url(art_p, _ids_str(others))
    else:
        for t in TYPE_LABELS:
            new_set = selected_types.copy()
            new_set.discard(t) if t in new_set else new_set.add(t)
            art_p = _ids_str(selected_ids) if selected_ids is not None else None
            typ_p = None if new_set == all_types else _ids_str(new_set)
            type_toggle_urls[t] = _index_url(art_p, typ_p)

    # 反选
    if selected_ids is None:
        artist_invert_url = _index_url("")
    else:
        inverted = all_artist_ids - selected_ids
        typ_p = _ids_str(selected_types) if selected_types is not None else None
        artist_invert_url = _index_url(
            None if inverted == all_artist_ids else _ids_str(inverted), typ_p
        )

    if selected_types is None:
        type_invert_url = _index_url(None, "")
    else:
        inverted = all_types - selected_types
        art_p = _ids_str(selected_ids) if selected_ids is not None else None
        type_invert_url = _index_url(
            art_p, None if inverted == all_types else _ids_str(inverted)
        )

    all_url = _index_url()

    # "显示已隐藏" toggle URL
    def _show_hidden_toggle_url():
        return build_local_url("/", {
            "artist_ids": artist_ids,
            "types": types,
            "show_hidden": None if show_hidden else "1",
            "period": period,
            "posted_after": posted_after,
            "posted_before": posted_before,
            "tags": tags,
        })
    show_hidden_toggle_url = _show_hidden_toggle_url()

    # 通用 URL 构建器：保留所有筛选条件，覆盖指定 key
    def _preserve_url(**overrides):
        p = {
            "artist_ids": artist_ids,
            "types": types,
            "show_hidden": "1" if show_hidden else None,
            "period": period,
            "posted_after": posted_after,
            "posted_before": posted_before,
            "tags": tags,
        }
        for k, v in overrides.items():
            p[k] = v
        return build_local_url("/", p)

    detail_query = build_local_url("", {
        "artist_ids": _ids_str(selected_ids) if selected_ids is not None else None,
        "types": _ids_str(selected_types) if selected_types is not None else None,
        "show_hidden": "1" if show_hidden else None,
        "period": period,
        "posted_after": posted_after,
        "posted_before": posted_before,
        "tags": tags,
    })

    # 筛选摘要
    period_labels = {'24h': '24小时内', 'week': '一周内', 'month': '一个月内',
                     'half_year': '半年内', 'year': '一年内', 'custom': '指定期间'}
    filter_summary = ''
    if selected_ids is not None:
        filter_summary += f'画师:{len(selected_ids)}位'
    else:
        filter_summary += '画师:全部'
    if selected_types is not None:
        labels = [TYPE_LABELS[t] for t in sorted(selected_types)]
        filter_summary += ' · 类型:' + ','.join(labels)
    else:
        filter_summary += ' · 类型:全部'
    if period:
        filter_summary += ' · 期间:' + period_labels.get(period, period)
    else:
        filter_summary += ' · 期间:不限'
    if show_hidden:
        filter_summary += ' · 含已隐藏'
    if selected_tags:
        filter_summary += ' · 标签:' + ','.join(sorted(selected_tags))

    # 页码计算（模式 A）
    import math
    current_page = (offset // page_size) + 1 if page_size > 0 else 1
    total_pages = max(1, math.ceil(total_count / page_size))
    # 页码窗口：当前页前后各 3 页
    p_start = max(1, current_page - 3)
    p_end = min(total_pages, current_page + 3)
    page_range = list(range(p_start, p_end + 1))

    return templates.TemplateResponse(
        request, "index.html",
        {"illustrations": illustrations, "artists": all_artists,
         "artists_map": artists_map, "selected_artist_ids": selected_ids,
         "all_artist_ids": all_artist_ids, "all_type_ids": all_types,
         "toggle_urls": toggle_urls, "type_toggle_urls": type_toggle_urls,
         "artist_invert_url": artist_invert_url, "type_invert_url": type_invert_url,
         "all_url": all_url, "selected_types": selected_types,
         "type_labels": TYPE_LABELS,
         "show_hidden": show_hidden,
         "show_hidden_toggle_url": show_hidden_toggle_url,
         "offset": offset, "page_size": page_size,
         "total_count": total_count, "has_more": has_more,
         "pagination_mode": _cfg.PAGINATION_MODE,
         "max_visible_items": _cfg.MAX_VISIBLE_ITEMS,
         "current_page": current_page, "total_pages": total_pages,
         "page_range": page_range,
         "period": period, "posted_after": posted_after,
         "posted_before": posted_before,
         "preserve_url": _preserve_url,
         "detail_query": detail_query,
         "period_labels": period_labels,
         "filter_summary": filter_summary,
         "tag_list": tag_list_all, "selected_tags": selected_tags},
    )


@router.get("/illust/{illust_id}")
async def illust_detail(request: Request, illust_id: int,
                         artist_ids: str = Query(None), artist_id: int = Query(None),
                         types: str = Query(None), show_hidden: str = Query(None),
                         period: str = Query(None), posted_after: str = Query(None),
                         posted_before: str = Query(None), tags: str = Query(None)):
    from datetime import datetime, timedelta

    show_hidden_value = show_hidden in ("1", "true")
    filter_tags = tags
    session = Session()
    illust = session.get(Illustration, illust_id)
    if not illust:
        session.close()
        return templates.TemplateResponse(request, "error.html", {"message": "作品不存在"})

    artist = session.get(TrackedArtist, illust.artist_id)

    import json
    paths = decode_file_paths(illust.file_paths)
    page_count = len(paths) or illust.page_count
    try:
        illust_tags = json.loads(illust.tags) if illust.tags else []
    except (json.JSONDecodeError, TypeError):
        illust_tags = []

    all_artists = _get_cached_artists(session)
    all_artist_ids = {a.id for a in all_artists}
    all_type_ids = set(TYPE_LABELS)
    selected_types = parse_csv_choices(types, all_type_ids)
    if artist_id is not None:
        selected_ids = {artist_id}
    else:
        selected_ids = parse_csv_ints(artist_ids)
        if selected_ids is not None:
            selected_ids &= all_artist_ids

    # 根据上下文构建查询，找到前/后作品（带 posted_at 用于插入点查找）
    base_query = session.query(Illustration.id, Illustration.posted_at)\
        .order_by(Illustration.posted_at.desc())
    if artist_id is not None:
        base_query = base_query.filter_by(artist_id=artist_id)
    elif selected_ids is not None:
        base_query = base_query.filter(Illustration.artist_id.in_(selected_ids))
    if selected_types is not None:
        base_query = base_query.filter(Illustration.type.in_(selected_types))
    if not show_hidden_value:
        base_query = base_query.filter(Illustration.is_hidden.is_not(True))
    if period and period != "custom":
        delta_map = {
            "24h": timedelta(hours=24),
            "week": timedelta(days=7),
            "month": timedelta(days=30),
            "half_year": timedelta(days=183),
            "year": timedelta(days=365),
        }
        if period in delta_map:
            base_query = base_query.filter(
                Illustration.posted_at >= datetime.utcnow() - delta_map[period]
            )
    elif period == "custom":
        if posted_after:
            base_query = base_query.filter(Illustration.posted_at >= posted_after)
        if posted_before:
            base_query = base_query.filter(
                Illustration.posted_at <= posted_before + "T23:59:59"
            )
    if filter_tags:
        tag_list = [tag.strip() for tag in filter_tags.split(",") if tag.strip()]
        if tag_list:
            tag_subquery = session.query(IllustrationTag.illustration_id).filter(
                IllustrationTag.tag.in_(tag_list)
            )
            base_query = base_query.filter(Illustration.id.in_(tag_subquery))

    all_rows = base_query.all()
    all_ids = [row[0] for row in all_rows]
    try:
        current_idx = all_ids.index(illust.id)
        prev_id = all_ids[current_idx - 1] if current_idx > 0 else None
        next_id = all_ids[current_idx + 1] if current_idx < len(all_ids) - 1 else None
    except ValueError:
        # 当前图不在筛选范围，找插入位置作为导航锚点
        current_idx = len(all_ids)
        for i, (rid, rdate) in enumerate(all_rows):
            if illust.posted_at and rdate and rdate <= illust.posted_at:
                current_idx = i
                break
        prev_id = all_ids[current_idx - 1] if current_idx > 0 else None
        next_id = all_ids[current_idx] if current_idx < len(all_ids) else None

    # 构建导航和返回链接，完整保留筛选上下文并统一 URL 编码。
    nav_params = {
        "artist_id": artist_id,
        "artist_ids": (
            _ids_str(selected_ids)
            if artist_id is None and selected_ids is not None
            else None
        ),
        "types": _ids_str(selected_types) if selected_types is not None else None,
        "show_hidden": "1" if show_hidden_value else None,
        "period": period,
        "posted_after": posted_after,
        "posted_before": posted_before,
        "tags": filter_tags,
    }
    nav_query = build_local_url("", nav_params)
    self_url = build_local_url(f"/illust/{illust.id}", nav_params)
    prev_url = build_local_url(f"/illust/{prev_id}", nav_params) if prev_id else None
    next_url = build_local_url(f"/illust/{next_id}", nav_params) if next_id else None

    if artist_id is not None:
        only_type = (
            next(iter(selected_types))
            if selected_types is not None and len(selected_types) == 1
            else None
        )
        back_url = build_local_url(f"/artist/{artist_id}", {
            "type": only_type,
            "show_hidden": "1" if show_hidden_value else None,
            "period": period,
            "posted_after": posted_after,
            "posted_before": posted_before,
            "tags": filter_tags,
        })
    else:
        back_url = build_local_url("/", {
            key: value for key, value in nav_params.items() if key != "artist_id"
        })

    # 构建详情页筛选 URL
    def _make_url(artist_value=None, type_value=None):
        return build_local_url(f"/illust/{illust_id}", {
            "artist_ids": artist_value,
            "types": type_value,
            "show_hidden": "1" if show_hidden_value else None,
            "period": period,
            "posted_after": posted_after,
            "posted_before": posted_before,
            "tags": filter_tags,
        })

    # 画师 toggle
    detail_toggle_urls = {}
    if selected_ids is None:
        for a in all_artists:
            others = all_artist_ids - {a.id}
            typ_p = _ids_str(selected_types) if selected_types is not None else None
            detail_toggle_urls[a.id] = _make_url(_ids_str(others), typ_p)
    else:
        for a in all_artists:
            new_set = selected_ids.copy()
            new_set.discard(a.id) if a.id in new_set else new_set.add(a.id)
            art_p = None if new_set == all_artist_ids else _ids_str(new_set)
            typ_p = _ids_str(selected_types) if selected_types is not None else None
            detail_toggle_urls[a.id] = _make_url(art_p, typ_p)

    # 类型 toggle
    detail_type_urls = {}
    if selected_types is None:
        for t in TYPE_LABELS:
            others = all_type_ids - {t}
            art_p = _ids_str(selected_ids) if selected_ids is not None else None
            detail_type_urls[t] = _make_url(art_p, _ids_str(others))
    else:
        for t in TYPE_LABELS:
            new_set = selected_types.copy()
            new_set.discard(t) if t in new_set else new_set.add(t)
            art_p = _ids_str(selected_ids) if selected_ids is not None else None
            typ_p = None if new_set == all_type_ids else _ids_str(new_set)
            detail_type_urls[t] = _make_url(art_p, typ_p)

    # 反选
    if selected_ids is None:
        artist_invert_url = _make_url("")
    else:
        inverted = all_artist_ids - selected_ids
        typ_p = _ids_str(selected_types) if selected_types is not None else None
        artist_invert_url = _make_url(
            None if inverted == all_artist_ids else _ids_str(inverted), typ_p
        )

    if selected_types is None:
        type_invert_url = _make_url(None, "")
    else:
        inverted = all_type_ids - selected_types
        art_p = _ids_str(selected_ids) if selected_ids is not None else None
        type_invert_url = _make_url(
            art_p, None if inverted == all_type_ids else _ids_str(inverted)
        )

    all_url = _make_url()

    session.close()

    from .. import config as _cfg

    # 从文件读取 caption（与图片同级，随 DATA_ROOT 迁移）
    caption_html = ""
    from ..tracker import _caption_path, _artist_dir_name
    from ..security import sanitize_caption
    caption_file = _caption_path(artist, illust.pixiv_illust_id)
    try:
        if caption_file.exists():
            caption_html = sanitize_caption(caption_file.read_text(encoding='utf-8'))
    except OSError:
        caption_html = ""

    # 本地画师目录路径（供「在资源管理器中打开」使用）
    local_dir = str(_cfg.IMAGES_DIR / _artist_dir_name(artist))

    return templates.TemplateResponse(
        request, "illust_detail.html",
        {"illust": illust, "artist": artist, "paths": paths,
         "caption_html": caption_html,
         "local_dir": local_dir,
         "page_count": page_count, "tags": illust_tags,
         "prev_id": prev_id, "next_id": next_id, "nav_query": nav_query,
         "self_url": self_url, "prev_url": prev_url, "next_url": next_url,
         "current_idx": current_idx + 1 if all_ids else 0, "total_count": len(all_ids),
         "back_url": back_url,
         "all_artists": all_artists, "all_artist_ids": all_artist_ids,
         "all_type_ids": all_type_ids,
         "selected_artist_ids": selected_ids,
         "selected_types": selected_types,
         "detail_toggle_urls": detail_toggle_urls, "detail_type_urls": detail_type_urls,
         "artist_invert_url": artist_invert_url, "type_invert_url": type_invert_url,
         "all_url": all_url,
         "type_labels": TYPE_LABELS,
         "show_hidden": show_hidden_value},
    )


@router.get("/artist/{artist_id}")
async def artist_works(request: Request, artist_id: int, type: str = Query(None),
                       show_hidden: str = Query(None),
                       offset: int = Query(0, ge=0), fragment: str = Query(None),
                       page: int = Query(None, ge=1),
                       period: str = Query(None),
                       posted_after: str = Query(None),
                       posted_before: str = Query(None),
                       tags: str = Query(None)):
    show_hidden = show_hidden in ("1", "true")
    from .. import config as _cfg
    from datetime import datetime, timedelta

    page_size = _cfg.PAGE_SIZE
    if page is not None and page > 0:
        offset = (page - 1) * page_size

    session = Session()
    artist = session.get(TrackedArtist, artist_id)
    if not artist:
        session.close()
        return templates.TemplateResponse(request, "error.html", {"message": "画师不存在"})

    query = (
        session.query(Illustration)
        .filter_by(artist_id=artist_id)
        .order_by(Illustration.posted_at.desc())
    )
    if type and type in TYPE_LABELS:
        query = query.filter_by(type=type)
    if not show_hidden:
        query = query.filter(Illustration.is_hidden.is_not(True))

    # 投稿期间筛选
    if period and period != 'custom':
        now = datetime.utcnow()
        delta_map = {
            '24h': timedelta(hours=24),
            'week': timedelta(days=7),
            'month': timedelta(days=30),
            'half_year': timedelta(days=183),
            'year': timedelta(days=365),
        }
        if period in delta_map:
            query = query.filter(Illustration.posted_at >= now - delta_map[period])
    elif period == 'custom':
        if posted_after:
            query = query.filter(Illustration.posted_at >= posted_after)
        if posted_before:
            query = query.filter(Illustration.posted_at <= posted_before + 'T23:59:59')

    # Tag 筛选（OR 逻辑，走 illustration_tag 索引）
    selected_tags = set()
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        selected_tags = set(tag_list)
        tag_subquery = session.query(IllustrationTag.illustration_id).filter(IllustrationTag.tag.in_(tag_list))
        query = query.filter(Illustration.id.in_(tag_subquery))

    # +1 trick: 免 COUNT 判断 has_more
    illustrations = query.offset(offset).limit(page_size + 1).all()
    has_more = len(illustrations) > page_size
    if has_more:
        illustrations = illustrations[:page_size]

    # Fragment 提前返回
    if fragment == "1":
        detail_query = build_local_url("", {
            "artist_id": artist.id,
            "types": type if type in TYPE_LABELS else None,
            "show_hidden": "1" if show_hidden else None,
            "period": period,
            "posted_after": posted_after,
            "posted_before": posted_before,
            "tags": tags,
        })
        resp = templates.TemplateResponse(request, "artist_fragment.html", {
            "artist": artist, "illustrations": illustrations,
            "type_labels": TYPE_LABELS, "current_type": type,
            "show_hidden": show_hidden,
            "detail_query": detail_query,
        })
        session.close()
        resp.headers["X-Has-More"] = "1" if has_more else "0"
        return resp

    # ── 完整页面仅以下 ──
    total_count = query.count()

    tag_list_all = _get_tags(session, query)
    session.close()

    # 通用 URL 构建器
    def _preserve_url(**overrides):
        p = {
            "type": type if type in TYPE_LABELS else None,
            "show_hidden": "1" if show_hidden else None,
            "period": period,
            "posted_after": posted_after,
            "posted_before": posted_before,
            "tags": tags,
        }
        for k, v in overrides.items():
            p[k] = v
        return build_local_url(f"/artist/{artist_id}", p)

    # 筛选摘要
    period_labels = {'24h': '24小时内', 'week': '一周内', 'month': '一个月内',
                     'half_year': '半年内', 'year': '一年内', 'custom': '指定期间'}
    filter_summary = ''
    filter_summary += '类型:' + (TYPE_LABELS[type] if type and type in TYPE_LABELS else '全部')
    if period:
        filter_summary += ' · 期间:' + period_labels.get(period, period)
    else:
        filter_summary += ' · 期间:不限'
    if show_hidden:
        filter_summary += ' · 含已隐藏'
    if selected_tags:
        filter_summary += ' · 标签:' + ','.join(sorted(selected_tags))

    # 构建 show_hidden toggle URL
    def _toggle_url(target_show):
        return build_local_url(f"/artist/{artist_id}", {
            "type": type if type in TYPE_LABELS else None,
            "show_hidden": "1" if target_show else None,
            "period": period,
            "posted_after": posted_after,
            "posted_before": posted_before,
            "tags": tags,
        })
    show_hidden_toggle_url = _toggle_url(not show_hidden)

    detail_query = build_local_url("", {
        "artist_id": artist.id,
        "types": type if type in TYPE_LABELS else None,
        "show_hidden": "1" if show_hidden else None,
        "period": period,
        "posted_after": posted_after,
        "posted_before": posted_before,
        "tags": tags,
    })

    # 页码计算（模式 A）
    import math
    current_page = (offset // page_size) + 1 if page_size > 0 else 1
    total_pages = max(1, math.ceil(total_count / page_size))
    p_start = max(1, current_page - 3)
    p_end = min(total_pages, current_page + 3)
    page_range = list(range(p_start, p_end + 1))

    return templates.TemplateResponse(
        request, "artist_works.html",
        {"artist": artist, "illustrations": illustrations,
         "type_labels": TYPE_LABELS, "current_type": type,
         "show_hidden": show_hidden,
         "show_hidden_toggle_url": show_hidden_toggle_url,
         "offset": offset, "page_size": page_size,
         "total_count": total_count, "has_more": has_more,
         "pagination_mode": _cfg.PAGINATION_MODE,
         "max_visible_items": _cfg.MAX_VISIBLE_ITEMS,
         "current_page": current_page, "total_pages": total_pages,
         "page_range": page_range,
         "period": period, "posted_after": posted_after,
         "posted_before": posted_before,
         "preserve_url": _preserve_url,
         "detail_query": detail_query,
         "period_labels": period_labels,
         "filter_summary": filter_summary,
         "tag_list": tag_list_all, "selected_tags": selected_tags},
    )


@router.post("/refresh")
async def refresh_all(artist_ids: str = Form(None), types: str = Form(None)):
    tracker = get_tracker()
    if tracker:
        from .. import progress
        progress.submit_task(
            "check_updates",
            tracker.check_updates,
            label="检查更新",
            dedupe_key="check-updates",
        )
    url = build_local_url("/", {"artist_ids": artist_ids, "types": types})
    return RedirectResponse(url, status_code=303)


@router.post("/illust/{illust_id}/hide")
async def hide_illust(illust_id: int, redirect_url: str = Form("/")):
    session = Session()
    illust = session.get(Illustration, illust_id)
    if illust:
        illust.is_hidden = True
        session.commit()
    session.close()
    return RedirectResponse(safe_redirect_target(redirect_url), status_code=303)


@router.post("/illust/{illust_id}/unhide")
async def unhide_illust(illust_id: int, redirect_url: str = Form("/")):
    session = Session()
    illust = session.get(Illustration, illust_id)
    if illust:
        illust.is_hidden = False
        session.commit()
    session.close()
    return RedirectResponse(safe_redirect_target(redirect_url), status_code=303)


@router.post("/illust/{illust_id}/delete")
async def delete_illust(request: Request, illust_id: int, redirect_url: str = Form("/")):
    tracker = get_tracker()
    if tracker:
        from .. import progress
        _, created = progress.submit_exclusive(
            "delete_illust",
            lambda: tracker.permanently_delete_illust(illust_id),
            label="永久删除作品",
        )
        if not created:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"message": "当前有检查、下载或其他写入任务，完成后才能永久删除。"},
                status_code=409,
            )
    return RedirectResponse(safe_redirect_target(redirect_url), status_code=303)


@router.post("/illust/{illust_id}/download-single")
async def download_single_illust(illust_id: int):
    """为未下载的作品触发下载（复用已有下载流程，gallery-dl 自动去重）。"""
    session = Session()
    illust = session.get(Illustration, illust_id)
    if not illust:
        session.close()
        return JSONResponse({"ok": False, "error": "作品不存在"}, status_code=404)
    artist = session.get(TrackedArtist, illust.artist_id)
    session.close()
    if not artist:
        return JSONResponse({"ok": False, "error": "画师不存在"}, status_code=404)

    tracker = get_tracker()
    if not tracker:
        return JSONResponse({"ok": False, "error": "未登录 Pixiv"}, status_code=503)

    from .. import progress
    job_id, created = progress.submit_task(
        "download_artist",
        lambda: tracker.download_single_artist(artist.id),
        label=f"下载 {artist.name}",
        dedupe_key=f"artist-{artist.id}",
    )
    return JSONResponse({"ok": True, "job_id": job_id, "created": created})


@router.post("/illust/{illust_id}/open-folder")
async def open_illust_folder(illust_id: int):
    """在 Windows 资源管理器中打开作品所在的本地目录。"""
    import os
    session = Session()
    illust = session.get(Illustration, illust_id)
    if not illust:
        session.close()
        return JSONResponse({"ok": False, "error": "作品不存在"}, status_code=404)
    artist = session.get(TrackedArtist, illust.artist_id)
    session.close()
    if not artist:
        return JSONResponse({"ok": False, "error": "画师不存在"}, status_code=404)
    from .. import config as _cfg
    from ..tracker import _artist_dir_name
    folder = _cfg.IMAGES_DIR / _artist_dir_name(artist)
    folder.mkdir(parents=True, exist_ok=True)
    os.startfile(str(folder))
    return JSONResponse({"ok": True})


@router.post("/illust/{illust_id}/bookmark")
async def toggle_bookmark(illust_id: int):
    from fastapi.responses import JSONResponse
    tracker = get_tracker()
    if not tracker:
        return JSONResponse({"ok": False, "error": "未登录 Pixiv"}, status_code=503)

    session = Session()
    illust = session.get(Illustration, illust_id)
    if not illust:
        session.close()
        return JSONResponse({"ok": False, "error": "作品不存在"}, status_code=404)

    try:
        if illust.is_bookmarked:
            import asyncio
            await asyncio.to_thread(
                tracker.client.delete_bookmark, illust.pixiv_illust_id
            )
            illust.is_bookmarked = False
        else:
            import asyncio
            await asyncio.to_thread(
                tracker.client.add_bookmark,
                illust.pixiv_illust_id,
                restrict="public",
            )
            illust.is_bookmarked = True
        session.commit()
        result = {"ok": True, "is_bookmarked": illust.is_bookmarked}
    except Exception as e:
        session.rollback()
        result = {"ok": False, "error": str(e)}
    finally:
        session.close()

    return JSONResponse(result, status_code=200 if result["ok"] else 500)
