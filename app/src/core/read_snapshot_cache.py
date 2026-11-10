"""Bounded, detached tag/task read data owned by one live catalogue or queue."""
from collections import OrderedDict
from copy import deepcopy
import json
from threading import Lock


class ReadSnapshotCache:
    def __init__(self, *, max_bytes=16 * 1024 * 1024):
        self._lock = Lock()
        self._values = OrderedDict()
        self._bytes = 0
        self.max_bytes = max_bytes

    def get(self, key):
        with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return None
            self._values.move_to_end(key)
        # Entries are never mutated after publication. No lock encloses copying,
        # SQL, decoding or any consumer's subsequent live-state assembly.
        return deepcopy(entry[0])

    def put(self, key, value):
        size = len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
        if size > self.max_bytes:
            return
        detached = deepcopy(value)
        with self._lock:
            previous = self._values.pop(key, None)
            if previous is not None:
                self._bytes -= previous[1]
            self._values[key] = (detached, size)
            self._bytes += size
            while len(self._values) > 8 or self._bytes > self.max_bytes:
                _, (_, removed) = self._values.popitem(last=False)
                self._bytes -= removed
