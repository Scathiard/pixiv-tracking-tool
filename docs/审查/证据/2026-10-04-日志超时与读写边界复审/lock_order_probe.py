"""Two-thread, memory-only checkout/lock-order audit; creates no archive/schema."""
from pathlib import Path
import json
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "app"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool
from src.core.database import ArchiveDatabase
from src.core.bookmarks import BookmarkService


def probe(release_reader_before_snapshot):
    database = ArchiveDatabase.__new__(ArchiveDatabase)
    database.engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=1,
        max_overflow=0, pool_timeout=0.25, connect_args={"check_same_thread": False})
    database.session = sessionmaker(database.engine, expire_on_commit=False)
    database._closed = False
    database._write_lock = threading.RLock()
    bookmarks = BookmarkService(database, None, current_account=lambda: None)
    reader_ready, writer_locked = threading.Event(), threading.Event()
    result = {}

    def reader():
        started = time.perf_counter()
        try:
            with database.read():
                reader_ready.set()
                if not writer_locked.wait(1):
                    raise RuntimeError("writer did not enter bounded gate")
                if not release_reader_before_snapshot:
                    bookmarks.snapshot(None, work_id=1)
            if release_reader_before_snapshot:
                bookmarks.snapshot(None, work_id=1)
            result["reader"] = "completed"
        except Exception as exc:
            result["reader"] = type(exc).__name__
        finally:
            result["reader_ms"] = round((time.perf_counter() - started) * 1000, 2)

    def writer():
        if not reader_ready.wait(1):
            result["writer"] = "reader gate timeout"
            writer_locked.set()
            return
        started = time.perf_counter()
        with bookmarks._lock:
            writer_locked.set()
            try:
                with database.write():
                    pass
                result["writer"] = "completed"
            except Exception as exc:
                result["writer"] = type(exc).__name__
        result["writer_ms"] = round((time.perf_counter() - started) * 1000, 2)

    threads = [threading.Thread(target=reader, daemon=True),
        threading.Thread(target=writer, daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)
    result["alive"] = sum(thread.is_alive() for thread in threads)
    result["checked_out"] = database.engine.pool.checkedout()
    assert result["alive"] == 0, result
    assert result["checked_out"] == 0, result
    database.close()
    return result


if __name__ == "__main__":
    current, detached = probe(False), probe(True)
    assert current["writer"] == "TimeoutError", current
    assert current["reader"] == "completed", current
    assert detached["writer"] == detached["reader"] == "completed", detached
    print(json.dumps({"real_account_used": False, "persistent_database_created": False,
        "forced_pool_capacity": 1, "checkout_timeout_seconds": 0.25,
        "current_order": current, "release_before_snapshot": detached}, ensure_ascii=False, indent=2))
