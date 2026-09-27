"""Local artist profiles; failed refreshes never discard an existing avatar."""
from __future__ import annotations

import os

from .download import AssetDownloader
from .media import build_thumbnail, contained_path, verify_media
from .models import Artist, utcnow


class ArtistProfiles:
    def __init__(self, database, publisher, remote, downloader=None):
        self.database, self.publisher, self.remote = database, publisher, remote
        self.downloader = downloader or AssetDownloader(max_bytes=8 * 1024 * 1024)

    def refresh(self, artist_id):
        with self.database.read() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                raise KeyError(artist_id)
            remote_id = artist.pixiv_user_id
        data = self.remote.get_artist_detail(remote_id)
        self.save(artist_id, data)

    def save(self, artist_id, data):
        # Profile text is independently useful when the avatar download fails.
        with self.database.write() as session:
            artist = session.get(Artist, artist_id)
            if artist is None:
                raise KeyError(artist_id)
            if isinstance(data.get("name"), str):
                artist.name = data["name"]
            if isinstance(data.get("account"), str):
                artist.account = data["account"]
            if isinstance(data.get("biography"), str):
                artist.biography = data["biography"]
            if isinstance(data.get("avatar_url"), str):
                artist.avatar_url = data["avatar_url"]
        url = data.get("avatar_url")
        if not url:
            with self.database.write() as session:
                session.get(Artist, artist_id).profile_refreshed_at = utcnow()
            return
        _, original = self.publisher.staging()
        _, preview = self.publisher.staging()
        self.downloader.download(url, original)
        verify_media(original, "image")
        verified = build_thumbnail(original, preview, size=(256, 256))
        relative = f".profiles/{artist_id}/{verified.sha256}.webp"
        target = contained_path(self.publisher.root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with preview.open("rb+") as handle:
            os.fsync(handle.fileno())
        # Content-addressed cache publication precedes the database pointer. A
        # crash leaves either the old pointer or an intact new file, never half
        # an avatar. Unreferenced stages/caches are retained for later cleanup.
        if target.exists():
            if verify_media(target, "thumbnail").sha256 != verified.sha256:
                raise ValueError("本地头像缓存校验失败，已有头像保留")
        else:
            os.rename(preview, target)
        with self.database.write() as session:
            artist = session.get(Artist, artist_id)
            if artist is not None:
                artist.avatar_path = relative
                artist.profile_refreshed_at = utcnow()
