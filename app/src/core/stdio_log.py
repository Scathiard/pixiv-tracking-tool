"""Append-only, timestamped stdout/stderr with atomic completed lines."""
from datetime import datetime
import io
from pathlib import Path
import threading
from typing import TextIO


class TimestampedLogStream(io.TextIOBase):
    """Assemble split writes per thread; completed lines share one file lock.

    Explicit flush writes the calling thread's unfinished line immediately.
    If another thread subsequently writes, it starts a separate physical line;
    a later continuation then receives its own timestamp. Closing drains all
    remaining fragments. No existing file content is rewritten.
    """

    def __init__(self, file: TextIO, *, needs_separator: bool = False):
        super().__init__()
        self._file = file
        self._lock = threading.RLock()
        self._pending: dict[threading.Thread, tuple[str, str]] = {}
        self._partial_owner: threading.Thread | None = None
        self._needs_separator = needs_separator

    @property
    def encoding(self):
        return self._file.encoding

    @property
    def errors(self):
        return self._file.errors

    @property
    def name(self):
        return self._file.name

    @property
    def mode(self):
        return self._file.mode

    @property
    def line_buffering(self):
        return self._file.line_buffering

    def writable(self):
        return not self.closed

    def fileno(self):
        return self._file.fileno()

    @staticmethod
    def _timestamp():
        return f"[{datetime.now().astimezone().isoformat(timespec='milliseconds')}] "

    def _emit(self, owner, text, prefix):
        separator = "\n" if self._needs_separator or (
            self._partial_owner is not None and self._partial_owner is not owner
        ) else ""
        stamp = "" if self._partial_owner is owner else prefix
        self._file.write(separator + stamp + text)
        self._needs_separator = False
        self._partial_owner = None if text.endswith("\n") else owner

    def write(self, text):
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        with self._lock:
            if self.closed:
                raise ValueError("I/O operation on closed file")
            if not text:
                return 0
            owner = threading.current_thread()
            pending, prefix = self._pending.get(owner, ("", self._timestamp()))
            parts = (pending + text).split("\n")
            for part in parts[:-1]:
                self._emit(owner, part + "\n", prefix)
                prefix = self._timestamp()
            if parts[-1]:
                self._pending[owner] = (parts[-1], prefix)
            else:
                self._pending.pop(owner, None)
            return len(text)

    def flush(self):
        with self._lock:
            if self.closed:
                raise ValueError("I/O operation on closed file")
            owner = threading.current_thread()
            pending = self._pending.pop(owner, None)
            if pending is not None:
                self._emit(owner, *pending)
            self._file.flush()

    def close(self):
        with self._lock:
            if self.closed:
                return
            for owner, pending in self._pending.items():
                self._emit(owner, *pending)
            self._pending.clear()
            try:
                super().close()
            finally:
                self._file.close()


def open_stdio_log(path: Path) -> TimestampedLogStream:
    """Open an existing log in append mode, preserving even an unfinished tail."""
    needs_separator = False
    if path.exists() and path.stat().st_size:
        with path.open("rb") as existing:
            existing.seek(-1, io.SEEK_END)
            needs_separator = existing.read(1) != b"\n"
    file = path.open("a", encoding="utf-8", buffering=1, newline="")
    return TimestampedLogStream(file, needs_separator=needs_separator)
