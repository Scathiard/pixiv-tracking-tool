import faulthandler

faulthandler.enable(all_threads=True)

import hashlib
import json
import os
import sys
import time


FILES = (
    r"D:\claude_sandbox\pixiv-tracking-tool\.worktrees\v0.2.0-build20261004.3\app\tests\__pycache__\test_settings_projection.cpython-312-pytest-9.1.1.pyc",
    r"D:\claude_sandbox\pixiv-tracking-tool\.worktrees\v0.2.0-build20261004.3\app\tests\__pycache__\test_archive_api.cpython-312-pytest-9.1.1.pyc",
    r"D:\claude_sandbox\pixiv-tracking-tool\.devtools\python\cpython-3.12-windows-x86_64-none\Lib\zoneinfo\__init__.py",
    r"D:\claude_sandbox\pixiv-tracking-tool\.devtools\v012-env\Lib\site-packages\gallery_dl\__init__.py",
)
MAX_BYTES = 2 * 1024 * 1024
MAX_OPERATIONS = 50
MAX_SECONDS = 10.0
started = time.perf_counter()
operations = 0
total_bytes = 0
completed_reads = 0


def emit(event, path=None, **fields):
    record = {"event": event, "elapsed": time.perf_counter() - started,
              "wall_time": time.time(), **fields}
    if path is not None:
        record["path"] = path
    print(json.dumps(record), flush=True)
    print("PROBE_PHASE " + json.dumps({key: value for key, value in record.items()
          if key in ("event", "path", "elapsed", "wall_time")}), file=sys.stderr, flush=True)


emit("started", operations=operations, total_bytes=total_bytes)
for cycle in range(1, 4):
    for path in FILES:
        if time.perf_counter() - started >= MAX_SECONDS:
            emit("time_budget_reached", path, operations=operations, total_bytes=total_bytes)
            raise SystemExit(2)
        if operations + 4 > MAX_OPERATIONS or total_bytes >= MAX_BYTES:
            emit("read_budget_reached", path, operations=operations, total_bytes=total_bytes)
            raise SystemExit(2)
        emit("before_stat", path)
        stat = os.stat(path)
        operations += 1
        emit("before_open", path)
        stream = open(path, "rb")
        operations += 1
        try:
            emit("before_read", path)
            content = stream.read(min(stat.st_size, MAX_BYTES - total_bytes))
            operations += 1
            total_bytes += len(content)
        finally:
            stream.close()
            operations += 1
        completed_reads += 1
        emit("read_complete", path, cycle=cycle, expected_bytes=stat.st_size,
             bytes=len(content), sha256=hashlib.sha256(content).hexdigest(),
             operations=operations, total_bytes=total_bytes)
        if time.perf_counter() - started >= MAX_SECONDS:
            emit("time_budget_reached", path, operations=operations, total_bytes=total_bytes)
            raise SystemExit(2)
emit("complete", completed_reads=completed_reads, operations=operations, total_bytes=total_bytes)
