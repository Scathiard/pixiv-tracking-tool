"""Runtime-owned positive media grants; exact database ownership remains authoritative."""
from __future__ import annotations

from collections import OrderedDict
from itertools import islice
from pathlib import PurePosixPath
import re
from stat import S_ISREG
from sys import getsizeof
from threading import Lock

from sqlalchemy import select, union

from .media import contained_path
from .models import ArchiveIntent, Artist, Asset, Derivative, Work


_WORK_HINT = re.compile(r"([0-9]+)(?:_p[0-9]+(?:_v[0-9]+)?|_card_motion_v[0-9]+|_ugoira)?(?:\.[^.]+)+")


def media_work_hint(relative):
    """A basename can narrow a query, but never proves ownership."""
    match = _WORK_HINT.fullmatch(PurePosixPath(relative).name)
    return match[1] if match else None


class MediaAccess:
    MAX_ENTRIES = 8192
    MAX_BYTES = 4 * 1024 * 1024
    BATCH_SIZE = 64

    def __init__(self, database, root):
        self.database, self.root = database, root
        self._lock = Lock()
        self._generation = None
        self._entries = OrderedDict()
        self._bytes = 0
        self._closed = False

    def path(self, relative):
        if not isinstance(relative, str) or relative.startswith((".staging/", ".recovery/", ".archive-writer")):
            return None
        try:
            return contained_path(self.root, relative)
        except ValueError:
            return None

    def _synchronize(self, generation):
        if self._generation != generation:
            self._entries.clear()
            self._bytes = 0
            self._generation = generation

    def contains(self, relative, generation):
        with self._lock:
            if self._closed or self.database.reads.generation != generation:
                return False
            self._synchronize(generation)
            if relative not in self._entries:
                return False
            self._entries.move_to_end(relative)
            return True

    def discard(self, relative):
        with self._lock:
            self._bytes -= self._entries.pop(relative, 0)

    def publish(self, relatives, generation):
        """Never relabel an old read as a grant for a newer commit."""
        with self._lock:
            if self._closed or self.database.reads.generation != generation:
                return 0
            self._synchronize(generation)
            added = 0
            for relative in relatives:
                size = getsizeof(relative) + 128
                if size > self.MAX_BYTES:
                    continue
                if relative in self._entries:
                    self._entries.move_to_end(relative)
                    continue
                self._entries[relative] = size
                self._bytes += size
                added += 1
                while len(self._entries) > self.MAX_ENTRIES or self._bytes > self.MAX_BYTES:
                    _, removed = self._entries.popitem(last=False)
                    self._bytes -= removed
            return added

    def warm(self, relatives):
        """Authorize at most one page batch. Inputs are literal paths, never trusted URLs."""
        generation = self.database.reads.generation
        paths = {}
        for relative in islice(relatives, self.BATCH_SIZE):
            path = self.path(relative)
            if path is not None and not self.contains(relative, generation):
                paths[relative] = path
        if not paths:
            return 0
        selected = tuple(paths)
        suppressed = select(ArchiveIntent.pixiv_illust_id).where(ArchiveIntent.mode == "suppressed")
        with self.database.read() as session:
            hints = {hint for relative in selected if (hint := media_work_hint(relative)) is not None}
            allowed = set()
            if hints:
                owners = tuple(session.scalars(select(Work.id).where(
                    Work.pixiv_illust_id.in_(hints), ~Work.pixiv_illust_id.in_(suppressed))))
                if owners:
                    # Both lookups share one read snapshot. work_id indexes keep
                    # normal page preheats from visiting unrelated library rows.
                    branches = [select(model.relative_path).where(
                        model.work_id.in_(owners), model.relative_path.in_(selected))
                        for model in (Asset, Derivative)]
                    allowed.update(session.scalars(union(*branches)))
            unresolved = tuple(relative for relative in selected if relative not in allowed)
            if unresolved:
                # An arbitrary historic name, an avatar, or a path shared by a
                # different owner cannot safely be excluded using its basename.
                branches = [select(model.relative_path).join(Work).where(
                    model.relative_path.in_(unresolved), ~Work.pixiv_illust_id.in_(suppressed))
                    for model in (Asset, Derivative)]
                branches.append(select(Artist.avatar_path).where(Artist.avatar_path.in_(unresolved)))
                allowed.update(session.scalars(union(*branches)))
        available = []
        for relative in allowed:
            try:
                stat = paths[relative].stat()
            except OSError:
                continue
            if S_ISREG(stat.st_mode) and stat.st_size > 0:
                available.append(relative)
        return self.publish(available, generation)

    def close(self):
        with self._lock:
            self._closed = True
            self._entries.clear()
            self._bytes = 0
