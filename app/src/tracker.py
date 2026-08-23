import json
import os
import io
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import gallery_dl.job as gdl_job

from .client import PixivClient
from . import config as _cfg
from . import progress
from .caption_state import encode_caption_state, should_fetch_caption
from .file_index import decode_file_paths, encode_file_paths, scan_artist_files
from .models import Session, TrackedArtist, Illustration, IllustrationTag
from .security import resolve_image_path, sanitize_caption as _sanitize_caption
from .tombstones import (
    add_deleted_illust,
    discard_deleted_illust,
    load_deleted_illust_ids,
    purge_deleted_files,
)


def _dir_name(name, user_id):
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', name)
    return f"{safe_name} {user_id}"


def _artist_dir_name(artist):
    """画师对应的文件目录名：{name} {user_id}（Windows 非法字符替换为 _）"""
    return _dir_name(artist.name, artist.pixiv_user_id)


def _rebuild_file_paths(images_dir, dir_name, illust_id):
    """从磁盘扫描匹配的文件，重建 JSON file_paths。"""
    artist_path = images_dir / dir_name
    web_prefix = f"/images/{dir_name}"
    return encode_file_paths(scan_artist_files(artist_path, web_prefix).get(str(illust_id), []))


def _update_artist_file_paths(session, artist):
    """单次扫描一个画师目录并更新该画师的全部作品索引。"""
    dir_name = _artist_dir_name(artist)
    artist_dir = _cfg.IMAGES_DIR / dir_name
    web_prefix = f"/images/{dir_name}"
    file_index = scan_artist_files(artist_dir, web_prefix)
    all_illusts = (
        session.query(Illustration)
        .filter_by(artist_id=artist.id)
        .all()
    )
    for illust in all_illusts:
        illust.file_paths = encode_file_paths(
            file_index.get(str(illust.pixiv_illust_id), [])
        )


def rebuild_all_file_paths():
    """启动兼容迁移：从磁盘重建所有 JSON 文件索引。"""
    session = Session()
    task_id = progress.begin_task("重建文件索引")
    try:
        artists = session.query(TrackedArtist).all()
        progress.begin_phase(task_id, "syncing")
        progress.set_artist_progress(task_id, 0, len(artists))
        for index, artist in enumerate(artists, start=1):
            progress.set_artist(task_id, artist.name)
            progress.set_artist_progress(task_id, index, len(artists))
            _update_artist_file_paths(session, artist)
        session.commit()
        return len(artists)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        progress.finish_task(task_id)


def _caption_path(artist, illust_id):
    """返回 `{artist_dir}/{illust_id}.caption.html` 的 Path。"""
    from . import config as _cfg
    return _cfg.IMAGES_DIR / _artist_dir_name(artist) / f"{illust_id}.caption.html"


def _quarantine_files(paths):
    """先把待删文件原子移入图片根目录内的隔离区，返回可回滚映射。"""
    unique_paths = list(dict.fromkeys(Path(path) for path in paths if Path(path).is_file()))
    if not unique_paths:
        return None, []
    trash_root = _cfg.IMAGES_DIR.resolve() / ".pixiv_tracker_trash"
    trash_root.mkdir(parents=True, exist_ok=True)
    quarantine = trash_root / uuid.uuid4().hex
    quarantine.mkdir()
    moved = []
    try:
        for index, source in enumerate(unique_paths):
            destination = quarantine / f"{index:04d}_{source.name}"
            os.replace(source, destination)
            moved.append((destination, source))
    except Exception:
        for destination, source in reversed(moved):
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)
        shutil.rmtree(quarantine, ignore_errors=True)
        raise
    return quarantine, moved


def _restore_quarantine(quarantine, moved):
    for destination, source in reversed(moved):
        if destination.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)
    if quarantine:
        shutil.rmtree(quarantine, ignore_errors=True)


def _natsort_key(f):
    """自然排序：将文件名中的数字段转为 int，确保 _p10 排在 _p2 之后。"""
    parts = re.split(r'(\d+)', f.name)
    key = []
    for p in parts:
        key.append(int(p) if p.isdigit() else p.lower())
    return key


def _merge_and_cleanup(old_dir, new_dir):
    """将 old_dir 文件合并到 new_dir，同名文件内容一致则删旧，不一致则保留两份。"""
    import filecmp
    try:
        if os.path.samefile(old_dir, new_dir):
            return
    except (FileNotFoundError, OSError):
        pass
    for f in old_dir.iterdir():
        if f.is_dir():
            continue
        dest = new_dir / f.name
        if not dest.exists():
            shutil.move(str(f), str(dest))
        elif filecmp.cmp(str(f), str(dest), shallow=False):
            f.unlink()
        else:
            alt = new_dir / f"{f.stem}_old{f.suffix}"
            counter = 2
            while alt.exists():
                alt = new_dir / f"{f.stem}_old{counter}{f.suffix}"
                counter += 1
            shutil.move(str(f), str(alt))
            print(f"  [warn] 文件冲突，保留两份: {f.name} → {alt.name}")
    try:
        old_dir.rmdir()
    except OSError:
        pass


def _sync_artist_name(artist, client, session):
    """同步画师名：若 Pixiv 名字变更，重命名目录并更新所有 file_paths。"""
    try:
        info = client.get_artist_detail(artist.pixiv_user_id)
        new_name = info.get("name", "").strip()
    except Exception as e:
        print(f"  [warn] 获取画师 {artist.name} 信息失败，跳过名称同步: {e}")
        return

    if not new_name or new_name == artist.name:
        return

    old_dir_name = _artist_dir_name(artist)
    new_dir_name = _dir_name(new_name, artist.pixiv_user_id)

    if old_dir_name == new_dir_name:
        return

    old_dir = _cfg.IMAGES_DIR / old_dir_name
    new_dir = _cfg.IMAGES_DIR / new_dir_name

    same_directory = False
    if old_dir.exists() and new_dir.exists():
        try:
            same_directory = os.path.samefile(old_dir, new_dir)
        except OSError:
            same_directory = False

    if same_directory and old_dir_name != new_dir_name:
        # Windows 大小写不敏感：两步改名只修正目录显示大小写，不做“合并”。
        temporary = _cfg.IMAGES_DIR / f".pixiv_rename_{uuid.uuid4().hex}"
        try:
            os.replace(old_dir, temporary)
            os.replace(temporary, new_dir)
        except Exception:
            if temporary.exists() and not old_dir.exists():
                os.replace(temporary, old_dir)
            raise
    elif old_dir.exists() and not new_dir.exists():
        shutil.move(str(old_dir), str(new_dir))
    elif old_dir.exists() and new_dir.exists():
        _merge_and_cleanup(old_dir, new_dir)

    artist.name = new_name

    for illust in artist.illustrations:
        if illust.file_paths:
            paths = [
                path.replace(f"/images/{old_dir_name}/", f"/images/{new_dir_name}/", 1)
                for path in decode_file_paths(illust.file_paths)
            ]
            illust.file_paths = encode_file_paths(paths)

    session.commit()
    print(f"  [sync] 画师名已更新: {old_dir_name} → {new_dir_name}")


def _parse_iso_date(s):
    """将 Pixiv API 返回的 ISO 8601 日期字符串转为 datetime。"""
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # 处理 Python 3.11 之前不支持的时区格式
        s = s.replace("Z", "+00:00")
        if s.endswith("+00:00"):
            s = s[:-6]
        return datetime.fromisoformat(s)


class Tracker:
    def __init__(self):
        self.client = PixivClient()

    def add_artist(self, user_id):
        """添加一个特别关注画师（只创建记录，不拉取作品）。
        返回 (ArtistLike, is_new)。ArtistLike 有 .id 和 .name 属性。"""
        session = Session()

        existing = session.query(TrackedArtist).filter_by(pixiv_user_id=user_id).first()
        if existing:
            eid, ename = existing.id, existing.name
            session.close()
            return type("_ArtistRef", (), {"id": eid, "name": ename})(), False

        info = self.client.get_artist_detail(user_id)
        artist = TrackedArtist(
            pixiv_user_id=info["user_id"],
            name=info["name"],
            pixiv_account=info["account"],
            avatar_url=info["avatar_url"],
        )
        session.add(artist)
        session.commit()
        aid, aname = artist.id, artist.name
        session.close()
        return type("_ArtistRef", (), {"id": aid, "name": aname})(), True

    def fetch_artist(self, artist_id):
        """后台拉取画师的全部作品并下载（用于添加画师后的异步任务）。"""
        session = Session()
        artist = session.get(TrackedArtist, artist_id)
        if not artist:
            session.close()
            return

        task_id = progress.begin_task(artist.name)
        progress.begin_phase(task_id, "checking")
        progress.set_detail(task_id, f"正在获取 {artist.name} 的全部作品...")
        progress.set_artist_progress(task_id, 1, 1)

        try:
            count = self._fetch_all_illusts(session, artist)
            progress.add_found(task_id, count)
        except Exception as e:
            progress.add_error(task_id, f"{artist.name}: {e}")
            progress.finish_task(task_id)
            session.close()
            return

        self._update_file_paths(session, artist)
        self._convert_ugoira_zips(session, artist)
        session.commit()

        pending = (
            session.query(Illustration)
            .filter_by(artist_id=artist.id)
            .filter(Illustration.file_paths.is_(None))
            .all()
        )
        total_pending = len(pending)

        if total_pending > 0:
            needs_clear = total_pending > count

            if needs_clear:
                all_illusts = (
                    session.query(Illustration)
                    .filter_by(artist_id=artist.id).all()
                )
                files_total = sum(i.page_count for i in all_illusts)
            else:
                files_total = sum(i.page_count for i in pending)

            progress.begin_phase(task_id, "downloading")
            progress.set_files_total(task_id, files_total)
            progress.set_dl_artist_progress(task_id, 1, 1)
            progress.set_detail(task_id, f"正在下载 {artist.name} 的作品...")

            try:
                self._download_artist(
                    artist.pixiv_user_id,
                    artist_dir=str(_cfg.IMAGES_DIR / _artist_dir_name(artist)),
                    task_id=task_id,
                    clear_archive=needs_clear,
                )
            except Exception as e:
                progress.add_error(task_id, f"下载 {artist.name}: {e}")

            self._update_file_paths(session, artist)
            self._convert_ugoira_zips(session, artist)
            session.commit()

        progress.finish_task(task_id)
        session.close()

    def remove_artist(self, artist_id):
        session = Session()
        artist = session.get(TrackedArtist, artist_id)
        if artist:
            session.delete(artist)
            session.commit()
        session.close()

    def _illust_files(self, illust):
        """只收集图片根目录内的普通文件，忽略越界或损坏的索引项。"""
        files = []
        for web_path in decode_file_paths(illust.file_paths):
            if not web_path.startswith("/images/"):
                continue
            file_path = resolve_image_path(_cfg.IMAGES_DIR, web_path[len("/images/"):])
            if file_path:
                files.append(file_path)
        caption_path = _caption_path(illust.artist, illust.pixiv_illust_id)
        try:
            resolved_caption = caption_path.resolve()
            resolved_caption.relative_to(_cfg.IMAGES_DIR.resolve())
            if resolved_caption.is_file():
                files.append(resolved_caption)
        except (OSError, RuntimeError, ValueError):
            pass
        return files

    def permanently_delete_illust(self, illust_id):
        """永久删除作品：文件先隔离，DB 提交失败时恢复文件。"""
        session = Session()
        quarantine = None
        moved = []
        tombstone_added = False
        pixiv_illust_id = None
        try:
            illust = session.get(Illustration, illust_id)
            if not illust:
                return
            pixiv_illust_id = illust.pixiv_illust_id
            quarantine, moved = _quarantine_files(self._illust_files(illust))
            tombstone_added = add_deleted_illust(pixiv_illust_id)
            session.delete(illust)
            session.commit()
        except Exception:
            session.rollback()
            _restore_quarantine(quarantine, moved)
            if tombstone_added and pixiv_illust_id:
                discard_deleted_illust(pixiv_illust_id)
            raise
        finally:
            session.close()
        if quarantine:
            shutil.rmtree(quarantine)

    def sync_all_bookmarks(self):
        """全量同步所有活跃画师作品的 is_bookmarked 状态。
        在后台线程中调用；通过 progress 模块报告进度。"""
        from . import progress

        session = Session()
        artists = session.query(TrackedArtist).filter_by(is_active=True).all()

        task_id = progress.begin_task("同步收藏状态")
        progress.begin_phase(task_id, "checking")
        progress.set_artist_progress(task_id, 0, len(artists))
        progress.set_detail(task_id, "正在同步收藏状态...")

        total_updated = 0
        for i, artist in enumerate(artists):
            progress.set_artist(task_id, artist.name)
            progress.set_artist_progress(task_id, i + 1, len(artists))
            try:
                for illust_data in self.client.get_all_artist_illusts(artist.pixiv_user_id):
                    illust = session.query(Illustration).filter_by(
                        pixiv_illust_id=illust_data["illust_id"]
                    ).first()
                    if illust:
                        api_bookmarked = illust_data.get("is_bookmarked", False)
                        if illust.is_bookmarked != api_bookmarked:
                            illust.is_bookmarked = api_bookmarked
                            total_updated += 1
                session.commit()
            except Exception as e:
                progress.add_error(task_id, f"{artist.name}: {e}")

        session.close()
        progress.set_detail(task_id, f"同步完成，更新 {total_updated} 件作品")
        progress.finish_task(task_id)
        return total_updated

    def validate_and_fix_all(self):
        """校验并补全所有已有作品信息：补拉缺失的 caption，标记缺失图片文件。
        在后台线程中调用；通过 progress 模块报告进度。"""
        from . import progress

        session = Session()
        all_illusts = session.query(Illustration).all()
        total = len(all_illusts)

        # 预加载画师信息，避免循环中 N+1 懒加载查询
        artist_ids = {i.artist_id for i in all_illusts}
        artists = session.query(TrackedArtist).filter(TrackedArtist.id.in_(artist_ids)).all()
        artist_map = {a.id: a for a in artists}
        artist_name_map = {a.id: a.name for a in artists}

        task_id = progress.begin_task("校验并补全作品信息")
        progress.begin_phase(task_id, "syncing")
        progress.set_files_total(task_id, total)
        progress.set_detail(task_id, "正在校验...")

        caption_filled = 0

        for i, illust in enumerate(all_illusts):
            progress.add_files_done(task_id, 1)
            artist_name = artist_name_map.get(illust.artist_id, str(illust.artist_id))
            progress.set_artist(task_id, artist_name)

            # 1. 补拉 caption
            artist = artist_map.get(illust.artist_id)
            if artist:
                caption_file = _caption_path(artist, illust.pixiv_illust_id)
                if should_fetch_caption(caption_file.exists(), illust.caption):
                    status = self._fetch_and_save_caption(illust)
                    if status in {"ok", "empty"}:
                        caption_filled += 1
                        progress.set_detail(task_id, f"补拉 caption: {illust.title}")
                    elif status == "failed":
                        state = illust.caption or ""
                        progress.add_error(task_id, f"caption {illust.pixiv_illust_id}: {state}")

            # 2. file_paths 重建由下方的 per-artist _update_file_paths 统一处理
            pass

            # 每 10 件 commit 一次
            if (i + 1) % 10 == 0:
                session.commit()

        # 逐画师从磁盘重建 file_paths
        progress.set_detail(task_id, "正在重建文件索引...")
        for artist in artists:
            self._update_file_paths(session, artist)

        session.commit()
        session.close()

        msg = f"校验完成：补 caption {caption_filled}"
        progress.set_detail(task_id, msg)
        progress.finish_task(task_id)
        return {"caption_filled": caption_filled}

    def remove_artist_keep_files(self, artist_id):
        """移除画师：删数据库记录，保留已下载文件。"""
        session = Session()
        artist = session.get(TrackedArtist, artist_id)
        if artist:
            session.delete(artist)  # cascade 删除所有 Illustration 记录
            session.commit()
        session.close()

    def remove_artist_and_files(self, artist_id):
        """移除画师：目录先隔离，DB 提交失败时恢复目录。"""
        session = Session()
        quarantine = None
        original_folder = None
        try:
            artist = session.get(TrackedArtist, artist_id)
            if not artist:
                return
            images_root = _cfg.IMAGES_DIR.resolve()
            folder = (images_root / _artist_dir_name(artist)).resolve()
            folder.relative_to(images_root)
            if folder.is_dir():
                trash_root = images_root / ".pixiv_tracker_trash"
                trash_root.mkdir(parents=True, exist_ok=True)
                quarantine = trash_root / uuid.uuid4().hex
                os.replace(folder, quarantine)
                original_folder = folder
            session.delete(artist)
            session.commit()
        except Exception:
            session.rollback()
            if quarantine and quarantine.exists() and original_folder:
                original_folder.parent.mkdir(parents=True, exist_ok=True)
                os.replace(quarantine, original_folder)
            raise
        finally:
            session.close()
        if quarantine:
            shutil.rmtree(quarantine)

    def check_updates(self):
        """检查所有活跃画师的新作品——先全部检查，再统一下载。"""
        session = Session()
        artists = session.query(TrackedArtist).filter_by(is_active=True).all()
        results = {}

        task_id = progress.begin_task()
        progress.begin_phase(task_id, "checking")
        progress.set_artist_progress(task_id, 0, len(artists))
        progress.set_detail(task_id, "正在检查画师更新...")

        # ═══ Phase 1: 检查所有画师，记录需要下载的画师 ═══
        to_download = []

        for i, artist in enumerate(artists):
            progress.set_artist(task_id, artist.name)
            progress.set_artist_progress(task_id, i + 1, len(artists))

            try:
                new_count = self._fetch_new_illusts(session, artist)
                progress.add_found(task_id, new_count)
            except Exception as e:
                progress.add_error(task_id, f"{artist.name}: {e}")
                new_count = 0

            self._update_file_paths(session, artist)
            self._convert_ugoira_zips(session, artist)
            session.commit()

            results[artist.name] = new_count
            artist.last_checked_at = datetime.utcnow()

            pending = (
                session.query(Illustration)
                .filter_by(artist_id=artist.id)
                .filter(Illustration.file_paths.is_(None))
                .all()
            )
            if pending:
                to_download.append((artist, pending, new_count))

        session.commit()

        # ═══ Phase 2: 统一下载，逐文件更新进度 ═══
        total_files = 0
        download_plan = []
        for artist, pending, new_count in to_download:
            needs_clear = len(pending) > new_count
            if needs_clear:
                all_illusts = (
                    session.query(Illustration)
                    .filter_by(artist_id=artist.id).all()
                )
                artist_files = sum(i.page_count for i in all_illusts)
            else:
                artist_files = sum(i.page_count for i in pending)
            total_files += artist_files
            download_plan.append((artist, needs_clear))

        if total_files > 0:
            progress.begin_phase(task_id, "downloading")
            progress.set_files_total(task_id, total_files)
            progress.set_dl_artist_progress(task_id, 0, len(download_plan))

            for i, (artist, needs_clear) in enumerate(download_plan):
                progress.set_artist(task_id, artist.name)
                progress.set_dl_artist_progress(task_id, i + 1, len(download_plan))
                progress.set_detail(task_id, f"正在下载 {artist.name} 的作品...")

                try:
                    self._download_artist(
                        artist.pixiv_user_id,
                        artist_dir=str(_cfg.IMAGES_DIR / _artist_dir_name(artist)),
                        task_id=task_id,
                        clear_archive=needs_clear,
                    )
                except Exception as e:
                    progress.add_error(task_id, f"下载 {artist.name}: {e}")

                self._update_file_paths(session, artist)
                self._convert_ugoira_zips(session, artist)
                session.commit()

        session.close()
        progress.finish_task(task_id)
        return results

    def download_single_artist(self, artist_id):
        """下载指定画师的缺失作品（跳过元数据重拉，直接扫描磁盘+下载）。"""
        session = Session()
        artist = session.get(TrackedArtist, artist_id)
        if not artist:
            session.close()
            return

        task_id = progress.begin_task(artist.name)
        progress.begin_phase(task_id, "checking")
        progress.set_artist_progress(task_id, 1, 1)
        progress.set_detail(task_id, f"正在扫描 {artist.name} 的本地文件...")

        self._update_file_paths(session, artist)
        session.commit()

        pending = (
            session.query(Illustration)
            .filter_by(artist_id=artist.id)
            .filter(Illustration.file_paths.is_(None))
            .all()
        )

        if pending:
            progress.begin_phase(task_id, "downloading")
            progress.set_detail(task_id, f"正在下载 {artist.name} 的缺失作品...")
            try:
                safe_name = artist.name
                for ch in r'\/:*?"<>|':
                    safe_name = safe_name.replace(ch, '_')
                artist_dir = str(_cfg.IMAGES_DIR / f"{safe_name} {artist.pixiv_user_id}")
                self._download_artist(artist.pixiv_user_id, artist_dir=artist_dir, task_id=task_id)
            except Exception as e:
                progress.add_error(task_id, f"下载 {artist.name}: {e}")

            self._update_file_paths(session, artist)
            self._convert_ugoira_zips(session, artist)
            session.commit()

        session.close()
        progress.finish_task(task_id)

    def download_pending(self):
        """下载所有缺失文件的作品。先扫描全部画师，再统一下载。"""
        session = Session()
        artists = session.query(TrackedArtist).filter_by(is_active=True).all()

        task_id = progress.begin_task()
        progress.begin_phase(task_id, "checking")
        progress.set_artist_progress(task_id, 0, len(artists))
        progress.set_detail(task_id, "正在扫描缺失文件...")

        # ═══ Phase 1: 扫描所有画师，收集待下载清单 ═══
        to_download = []

        for i, artist in enumerate(artists):
            progress.set_artist(task_id, artist.name)
            progress.set_artist_progress(task_id, i + 1, len(artists))

            self._update_file_paths(session, artist)
            self._convert_ugoira_zips(session, artist)
            session.commit()

            pending = (
                session.query(Illustration)
                .filter_by(artist_id=artist.id)
                .filter(Illustration.file_paths.is_(None))
                .all()
            )
            if pending:
                to_download.append((artist, pending))

        # ═══ Phase 2: 统一下载，逐文件更新进度 ═══
        total_files = 0
        for artist, _ in to_download:
            all_illusts = (
                session.query(Illustration)
                .filter_by(artist_id=artist.id).all()
            )
            total_files += sum(i.page_count for i in all_illusts)

        if total_files > 0:
            progress.begin_phase(task_id, "downloading")
            progress.set_files_total(task_id, total_files)
            progress.set_dl_artist_progress(task_id, 0, len(to_download))

            for i, (artist, _) in enumerate(to_download):
                progress.set_artist(task_id, artist.name)
                progress.set_dl_artist_progress(task_id, i + 1, len(to_download))
                progress.set_detail(task_id, f"正在下载 {artist.name} 的作品...")

                try:
                    self._download_artist(
                        artist.pixiv_user_id,
                        artist_dir=str(_cfg.IMAGES_DIR / _artist_dir_name(artist)),
                        task_id=task_id,
                        clear_archive=True,
                    )
                except Exception as e:
                    progress.add_error(task_id, f"下载 {artist.name}: {e}")

                self._update_file_paths(session, artist)
                self._convert_ugoira_zips(session, artist)
                session.commit()

        session.close()
        progress.finish_task(task_id)

    def _fetch_all_illusts(self, session, artist):
        count = 0
        deleted_ids = load_deleted_illust_ids()
        for illust_data in self.client.get_all_artist_illusts(artist.pixiv_user_id):
            if illust_data["illust_id"] in deleted_ids:
                continue
            illust = self._save_illust(session, artist, illust_data)
            self._fetch_and_save_caption(illust)
            count += 1
        return count

    def _fetch_new_illusts(self, session, artist):
        """检查并保存画师的新作品。全量拉取后比对，不依赖 API 返回顺序。"""
        _sync_artist_name(artist, self.client, session)

        existing_ids = {
            row[0]
            for row in session.query(Illustration.pixiv_illust_id)
            .filter_by(artist_id=artist.id)
            .all()
        }

        count = 0
        deleted_ids = load_deleted_illust_ids()
        for illust_data in self.client.get_all_artist_illusts(artist.pixiv_user_id):
            if illust_data["illust_id"] in deleted_ids:
                continue
            if illust_data["illust_id"] not in existing_ids:
                illust = self._save_illust(session, artist, illust_data)
                self._fetch_and_save_caption(illust)
                count += 1
            else:
                # 已有作品：同步收藏状态（caption 补拉走设置页「校验补全」按钮，不在 refresh 时逐件调 detail API）
                illust = session.query(Illustration).filter_by(
                    pixiv_illust_id=illust_data["illust_id"]
                ).first()
                if illust:
                    api_bookmarked = illust_data.get("is_bookmarked", False)
                    if illust.is_bookmarked != api_bookmarked:
                        illust.is_bookmarked = api_bookmarked
        return count

    def _save_illust(self, session, artist, illust_data):
        posted_at = illust_data.get("posted_at")
        if posted_at and isinstance(posted_at, str):
            posted_at = _parse_iso_date(posted_at)

        illust = Illustration(
            pixiv_illust_id=illust_data["illust_id"],
            artist_id=artist.id,
            title=illust_data["title"],
            type=illust_data["type"],
            page_count=illust_data["page_count"],
            tags=json.dumps(illust_data["tags"], ensure_ascii=False),
            bookmark_count=illust_data["bookmark_count"],
            view_count=illust_data["view_count"],
            posted_at=posted_at,
            is_bookmarked=illust_data.get("is_bookmarked", False),
            x_restrict=illust_data.get("x_restrict", 0),
        )
        session.add(illust)
        session.commit()

        # 写入 illustration_tag 表
        for tag in illust_data["tags"]:
            session.execute(
                IllustrationTag.__table__.insert().values(
                    illustration_id=illust.id, tag=tag
                ).prefix_with("OR IGNORE")
            )
        session.commit()

        return illust

    def _fetch_and_save_caption(self, illust):
        """补拉 caption，并持久化成功、空内容、不可用或暂时失败状态。"""
        try:
            detail = self.client.get_illust_detail(illust.pixiv_illust_id)
            caption = detail.get("caption", "")
            artist = illust.artist
            caption_path = _caption_path(artist, illust.pixiv_illust_id)
            caption_path.parent.mkdir(parents=True, exist_ok=True)
            caption_path.write_text(_sanitize_caption(caption), encoding='utf-8')
            status = "ok" if caption else "empty"
            illust.caption = encode_caption_state(status)
            return status
        except RuntimeError as e:
            msg = str(e)
            if "尚无此页" in msg or "not found" in msg.lower():
                illust.caption = encode_caption_state("unavailable", msg)
                return "unavailable"
            illust.caption = encode_caption_state("failed", msg)
            print(f"  [caption] ✗ {illust.pixiv_illust_id}: {e}")
            return "failed"
        except Exception as e:
            illust.caption = encode_caption_state("failed", str(e))
            print(f"  [caption] ✗ {illust.pixiv_illust_id}: {e}")
            return "failed"

    def _download_artist(self, user_id, artist_dir=None, task_id=None, clear_archive=False):
        """用 gallery-dl 下载画师的全部作品（auto-dedup）。
        若提供 artist_dir+task_id，会每秒轮询该目录新增文件数来更新下载进度。"""
        from .auth import configure_gallery_dl, _load_token
        import threading
        from pathlib import Path

        saved = _load_token()
        if saved.get("refresh_token"):
            configure_gallery_dl(saved["refresh_token"])

        if clear_archive:
            archive = _cfg.DATA_DIR / "gallery_dl_archive.db"
            if archive.exists():
                archive.unlink()

        url = f"https://www.pixiv.net/users/{user_id}"

        if artist_dir and task_id:
            artist_dir = Path(artist_dir)
            artist_dir.mkdir(parents=True, exist_ok=True)
            before = {f.name for f in artist_dir.iterdir() if not f.name.endswith(".part")}
            last_count = [0]
            download_error = [None]
            done = threading.Event()

            def _dl():
                try:
                    job = gdl_job.DownloadJob(url)
                    job.run()
                except Exception as e:
                    download_error[0] = e
                finally:
                    done.set()

            t = threading.Thread(target=_dl, daemon=True)
            t.start()

            while not done.wait(0.5):
                if artist_dir.exists():
                    current = {f.name for f in artist_dir.iterdir() if not f.name.endswith(".part")}
                    new = len(current - before)
                    if new > last_count[0]:
                        progress.add_files_done(task_id, new - last_count[0])
                        last_count[0] = new

            t.join()

            if download_error[0] is not None:
                purge_deleted_files(artist_dir)
                raise RuntimeError(f"下载出错 ({user_id}): {download_error[0]}") from download_error[0]

            if artist_dir.exists():
                current = {f.name for f in artist_dir.iterdir() if not f.name.endswith(".part")}
                new = len(current - before)
                if new > last_count[0]:
                    progress.add_files_done(task_id, new - last_count[0])
        else:
            try:
                job = gdl_job.DownloadJob(url)
                job.run()
            except Exception:
                if artist_dir:
                    purge_deleted_files(Path(artist_dir))
                raise

        if artist_dir:
            purge_deleted_files(Path(artist_dir))

    def _convert_ugoira_zips(self, session, artist):
        """将画师目录下已下载的 ugoira ZIP 转换为 GIF。"""
        import zipfile
        from PIL import Image
        dir_name = _artist_dir_name(artist)
        artist_dir = _cfg.IMAGES_DIR / dir_name
        if not artist_dir.exists():
            return

        ugoira_illusts = (
            session.query(Illustration)
            .filter_by(artist_id=artist.id, type='ugoira')
            .all()
        )

        for illust in ugoira_illusts:
            # gallery-dl 下载的 ugoira 格式：{id}_p0.zip
            zip_path = artist_dir / f"{illust.pixiv_illust_id}_p0.zip"
            if not zip_path.exists():
                # 也尝试旧格式
                zip_path = artist_dir / f"{illust.pixiv_illust_id}_ugoira.zip"
            gif_path = artist_dir / f"{illust.pixiv_illust_id}.gif"
            if not zip_path.exists() or gif_path.exists():
                continue

            try:
                frames_data = []
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    for name in sorted(zf.namelist()):
                        with zf.open(name) as f:
                            img = Image.open(io.BytesIO(f.read()))
                            frames_data.append(img.convert('RGBA'))

                if not frames_data:
                    continue

                # 从 Pixiv API 获取帧延迟
                delays = []
                try:
                    data = self.client._get(
                        '/v1/ugoira/metadata',
                        {'illust_id': illust.pixiv_illust_id}
                    )
                    frames = data.get('ugoira_metadata', {}).get('frames', [])
                    delays = [f.get('delay', 100) for f in frames]
                except Exception:
                    pass

                if frames_data:
                    kwargs = {
                        'save_all': True,
                        'append_images': frames_data[1:],
                        'loop': 0,
                        'optimize': True,
                        'disposal': 2,
                    }
                    if delays:
                        kwargs['duration'] = delays
                    frames_data[0].save(gif_path, **kwargs)

                # 更新文件路径指向 GIF
                web_prefix = f"/images/{dir_name}"
                illust.file_paths = encode_file_paths([f"{web_prefix}/{gif_path.name}"])
                zip_path.unlink()  # 删除原始 ZIP

            except Exception as e:
                print(f"  [!] ugoira 转换失败 {illust.title}: {e}")

    def _update_file_paths(self, session, artist):
        """单次扫描画师目录并更新全部作品的 JSON 文件索引。"""
        _update_artist_file_paths(session, artist)
