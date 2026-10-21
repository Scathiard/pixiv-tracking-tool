"""Bounded, temporary reading visits. Membership and content revisions stay pinned."""
from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
import random
from threading import RLock
import time
from uuid import uuid4


@dataclass(frozen=True)
class Member:
    work_id: int
    revision_id: str | None
    page_count: int


@dataclass
class Visit:
    members: tuple[Member, ...]
    seed: str
    touched: float
    excluded: set[int] = field(default_factory=set)
    orders: OrderedDict = field(default_factory=OrderedDict)

    def order(self, mode: str, round_number: int):
        key = (mode, round_number)
        if key in self.orders:
            self.orders.move_to_end(key)
            return self.orders[key]
        members = list(self.members)
        if mode == "random" and len(members) > 1:
            original = tuple(members)
            # With two members only the same AB order avoids an AA/BB boundary.
            def shuffled(number):
                result = list(original)
                seed = sha256(f"{self.seed}:{number if len(original) > 2 else 0}".encode()).digest()
                random.Random(seed).shuffle(result)
                return result
            members = shuffled(round_number)
            if len(members) > 2 and round_number and members[0] == shuffled(round_number - 1)[-1]:
                # Leave the final member unchanged, so this is independently
                # reconstructible without retaining or walking previous rounds.
                members[0], members[1] = members[1], members[0]
        members = [member for member in members if member.work_id not in self.excluded]
        starts = [0]
        for member in members:
            starts.append(starts[-1] + member.page_count)
        value = (members, starts)
        self.orders[key] = value
        while len(self.orders) > 3:
            self.orders.popitem(last=False)
        return value


class ReadingSessions:
    MAX_VISITS = 4
    TTL = 4 * 60 * 60

    def __init__(self, library, clock=time.monotonic):
        self.library, self.clock = library, clock
        self._lock = RLock()
        self._visits: OrderedDict[str, Visit] = OrderedDict()
        self._preparation = None
        self._stop_requested = False

    def start(self):
        from .reading_cache import ReadingPreparation
        with self._lock:
            if self._stop_requested:
                from .read_control import ReadCancelled
                raise ReadCancelled("阅读清单准备已停止")
            if self._preparation is None:
                self._preparation = ReadingPreparation(self.library, self._make_visit)
            preparation = self._preparation
        preparation.start()

    def request_stop(self):
        with self._lock:
            self._stop_requested = True
            preparation = self._preparation
        if preparation is not None:
            preparation.request_stop()

    def stop(self, timeout=None):
        self.request_stop()
        return self._preparation is None or self._preparation.stop(timeout)

    def prepare(self, filters, ceiling=None):
        self.start()
        return self._preparation.prepare(filters, ceiling)

    def _make_visit(self, manifest):
        self._checkpoint()
        members = []
        for index, item in enumerate(manifest):
            if index % 256 == 0:
                self._checkpoint()
            members.append(Member(**item))
        self._checkpoint()
        visit = Visit(tuple(members), uuid4().hex, self.clock())
        visit.order("random", 0)
        self._checkpoint()
        return visit

    def _checkpoint(self):
        with self._lock:
            if self._stop_requested:
                from .read_control import ReadCancelled
                raise ReadCancelled("阅读清单准备已停止")

    def create(self, filters, ceiling=None):
        from .read_control import current_read
        visit = None
        if self._preparation is not None:
            if current_read.get() is None:
                visit = self.prepare(filters, ceiling).result()
            else:
                # Legacy decorated callers own a read slot, so they must not
                # wait for the background commit fence and preparation worker.
                visit = self._preparation.try_claim(filters, ceiling)
        if visit is None:
            visit = self._make_visit(self.library.reading_manifest(filters, ceiling=ceiling))
        return self.install(visit)

    def install(self, visit):
        now, token = self.clock(), uuid4().hex
        visit.touched = now
        with self._lock:
            for key in list(self._visits):
                if now - self._visits[key].touched > self.TTL:
                    self._visits.pop(key)
            self._visits[token] = visit
            while len(self._visits) > self.MAX_VISITS:
                self._visits.popitem(last=False)
        return {"session_id": token, "total": len(visit.members),
                "page_total": sum(m.page_count for m in visit.members)}

    def _visit(self, token):
        visit = self._visits.get(token)
        if visit is None or self.clock() - visit.touched > self.TTL:
            raise KeyError("阅读范围已过期，请重新打开；本地作品未改变")
        visit.touched = self.clock()
        self._visits.move_to_end(token)
        return visit

    def release(self, token):
        with self._lock:
            self._visits.pop(token, None)

    def exclude(self, token, work_id):
        with self._lock:
            visit = self._visit(token)
            visit.excluded.add(work_id)
            visit.orders.clear()

    @staticmethod
    def _mode(mode):
        if mode not in ("sequential", "random"):
            raise ValueError("不支持的阅读顺序")

    def locate(self, token, work_id, *, mode="sequential", round_number=0, page=0):
        self._mode(mode)
        with self._lock:
            members, starts = self._visit(token).order(mode, max(0, round_number))
            position = next((i for i, member in enumerate(members) if member.work_id == work_id), None)
            if position is None:
                raise KeyError(work_id)
            member = members[position]
            page = max(0, min(page, member.page_count - 1))
            base = starts[-1] * round_number if mode == "random" and len(members) > 1 else 0
            return {"work_id": work_id, "revision_id": member.revision_id, "work_index": position,
                    "round": round_number, "page": page, "index": base + starts[position] + page,
                    "previous_work_index": base + starts[position - 1] if position else
                        (base - self._visit(token).order(mode, round_number - 1)[0][-1].page_count if base else None),
                    "next_work_index": base + starts[position + 1]}

    def entry(self, token, index, *, mode="sequential", round_number=0):
        self._mode(mode)
        with self._lock:
            members, _ = self._visit(token).order(mode, max(0, round_number))
            if not 0 <= index < len(members):
                raise KeyError(index)
            member = members[index]
            return {"work_id": member.work_id, "revision_id": member.revision_id,
                    "work_index": index, "page_count": member.page_count, "total": len(members)}

    def feed(self, token, *, offset=0, limit=48, mode="sequential"):
        self._mode(mode)
        if offset < 0 or not 1 <= limit <= 120:
            raise ValueError("流式窗口范围不合法")
        with self._lock:
            visit = self._visit(token)
            members, starts = visit.order(mode, 0)
            page_total, work_total = starts[-1], len(members)
            looping = mode == "random" and work_total > 1
            if not page_total:
                return {"items": [], "total": 0, "offset": offset, "has_more": False, "work_total": 0}
            end = offset + limit if looping else min(offset + limit, page_total)
            requests, cursor = [], offset
            while cursor < end:
                round_number = cursor // page_total if looping else 0
                members, starts = visit.order(mode, round_number)
                relative = cursor % page_total if looping else cursor
                position = bisect_right(starts, relative) - 1
                if position >= len(members):
                    break
                member = members[position]
                page = relative - starts[position]
                count = min(member.page_count - page, end - cursor)
                previous = round_number * page_total + starts[position - 1] if position else None
                if position == 0 and round_number:
                    previous = round_number * page_total - visit.order(mode, round_number - 1)[0][-1].page_count
                requests.append({"work_id": member.work_id, "revision_id": member.revision_id,
                    "page_offset": page, "page_limit": count, "index": cursor, "work_index": position,
                    "round": round_number, "previous_work_index": previous,
                    "next_work_index": cursor + member.page_count - page})
                cursor += count
            total = ((end - 1) // page_total + 2) * page_total if looping else page_total
        # All catalogue reads and media work happen outside the visit mutex.
        result = self.library.reading_window(requests)
        return {**result, "total": total, "offset": offset, "has_more": end < total,
                "work_total": work_total, "looping": looping}
