"""A process-owned lease for one catalogue and media root; locks survive no process."""
from pathlib import Path
import os


class LibraryInUse(RuntimeError):
    user_message = "此图库已被另一个新版程序打开，请先关闭另一个程序"


class LibraryLease:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._stream = None

    def acquire(self):
        if self._stream is not None:
            return self
        self.root.mkdir(parents=True, exist_ok=True)
        stream = (self.root / ".archive-writer.lock").open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._stream = stream
        except OSError as exc:
            stream.close()
            raise LibraryInUse(LibraryInUse.user_message) from exc
        return self

    def close(self):
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()
