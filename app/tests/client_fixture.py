"""Generated v0.2.2 browser library. Never opens configuration or a real token."""
from datetime import datetime, timedelta
import hashlib
import io
import os
from pathlib import Path
import threading

from PIL import Image, ImageDraw, ImageOps

from src.core.models import ArchiveIntent, Artist, Asset, Derivative, RemoteBookmark, TrackingPolicy, Work, WorkTag
from src.core.runtime import Runtime


class SyntheticRemote:
    def __init__(self):
        self.values = {}
        self.write_count = 0
        self.mode = "normal"
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def get_account_id(self):
        return "90001"

    def add_bookmark(self, remote_id, restrict="public"):
        self.write_count += 1
        self.entered.set()
        if not self.release.wait(20):
            raise TimeoutError("synthetic readback gate")
        if self.mode == "reject":
            from src.client import PixivRequestError
            raise PixivRequestError(403, "测试：主站拒绝了请求")
        self.values[remote_id] = True
        if self.mode == "lost":
            raise TimeoutError("synthetic response lost")

    def delete_bookmark(self, remote_id):
        self.write_count += 1
        self.values[remote_id] = False

    def read_bookmark(self, remote_id):
        return None if self.mode == "missing" else self.values.get(remote_id, False)

    def get_all_artist_illusts(self, remote_id):
        return iter(())

    def get_artist_detail(self, remote_id):
        return {"user_id": str(remote_id), "name": f"本地画师 {remote_id}", "biography": "隔离测试资料"}


def make_library(root: Path, works=5000):
    root.mkdir(parents=True, exist_ok=False)
    remote = SyntheticRemote()
    runtime = Runtime(root / "data/pixiv_tracker.db", root / "images", create=True, client=remote)
    runtime.account_id = runtime.library.account_id = "90001"
    palette = []
    for index in range(24):
        with Image.new("RGB", (640, 860), (40 + index * 7 % 125, 65 + index * 3 % 110, 100 + index * 6 % 120)) as picture:
            draw = ImageDraw.Draw(picture)
            draw.ellipse((250, 100, 510, 360), fill=(235, 198, 158))
            draw.polygon(((0, 640), (280, 300), (640, 740)), fill=(45, 60 + index * 4, 110))
            draw.polygon(((100, 860), (450, 400), (640, 730), (640, 860)), fill=(32, 47, 75))
            draw.text((35, 40), f"LOCAL / {index + 1:02}", fill="white", font_size=28)
            original, thumb = io.BytesIO(), io.BytesIO()
            picture.save(original, format="PNG")
            with ImageOps.fit(picture, (360, 360)) as small:
                small.save(thumb, format="WEBP")
            original_path = runtime.images / f"fixture-{index}.png"
            thumbnail_path = runtime.images / f"fixture-{index}.webp"
            original_path.write_bytes(original.getvalue())
            thumbnail_path.write_bytes(thumb.getvalue())
            palette.append((original_path, thumbnail_path, hashlib.sha256(original.getvalue()).hexdigest()))
    rows = []
    for index in range(1, works + 1):
        rows.append({"id": index, "pixiv_illust_id": str(100000 + index), "artist_id": 1 + index % 12,
                     "title": f"山与海 · 本地作品 {index:05}", "type": "manga" if index % 5 == 0 else "illust",
                     "page_count": 2000 if index == works else 3 if index % 5 == 0 else 1,
                     "caption": "<p>完整简介第一段：海边与山间的光。</p><p>第二段不会折叠；手机也能直接阅读。</p>",
                     "caption_state": "available", "posted_at": datetime(2026, 1, 1) + timedelta(minutes=index)})
    with runtime.database.write() as session:
        session.execute(Artist.__table__.insert(), [{"id": index, "pixiv_user_id": str(index), "name": f"本地画师 {index:02}",
            "biography": "用色彩记录旅途中的风景。\n这里的资料离线也能查看。", "avatar_path": f"fixture-{index}.webp",
            "profile_refreshed_at": datetime(2026, 9, 27)} for index in range(1, 13)])
        session.execute(TrackingPolicy.__table__.insert(), [{"artist_id": index, "mode": "paused" if index % 3 == 0 else "active"} for index in range(1, 13)])
        session.execute(Work.__table__.insert(), rows)
        session.execute(ArchiveIntent.__table__.insert(), [{"pixiv_illust_id": row["pixiv_illust_id"]} for row in rows])
        session.execute(WorkTag.__table__.insert(), [{"work_id": row["id"], "tag": "风景" if row["id"] % 2 else "插画"} for row in rows])
        session.execute(RemoteBookmark.__table__.insert(), [{"work_id": row["id"], "value": row["id"] % 11 == 0,
            "source": "legacy_cache", "version": 0} for row in rows])
        assets, previews = [], []
        for row in rows:
            original, thumbnail, digest = palette[row["id"] % 24]
            for page in range(row["page_count"]):
                relative = f"{row['pixiv_illust_id']}_p{page}.png"
                target = runtime.images / relative
                try:
                    os.link(original, target)
                except OSError as exc:
                    if getattr(exc, "winerror", None) != 1142:
                        raise
                    # NTFS limits links per file. Start a fresh synthetic source
                    # for the rest of a very long work, retaining every fixture.
                    replacement = runtime.images / f"fixture-overflow-{row['id']}-{page}.png"
                    with replacement.open("xb") as output:
                        output.write(original.read_bytes())
                    original = replacement
                    os.link(original, target)
                stat = target.stat()
                assets.append({"work_id": row["id"], "role": "image", "page_index": page, "relative_path": relative,
                    "state": "verified", "width": 640, "height": 860, "sha256": digest, "byte_size": stat.st_size,
                    "file_mtime_ns": stat.st_mtime_ns, "file_identity": stat.st_ino})
            thumb_relative = f"{row['pixiv_illust_id']}-thumb.webp"
            os.link(thumbnail, runtime.images / thumb_relative)
            previews.append({"work_id": row["id"], "kind": "thumbnail", "relative_path": thumb_relative,
                "state": "verified", "width": 360, "height": 360})
        for start in range(0, len(assets), 800):
            session.execute(Asset.__table__.insert(), assets[start:start + 800])
        session.execute(Derivative.__table__.insert(), previews)
    return runtime, remote
