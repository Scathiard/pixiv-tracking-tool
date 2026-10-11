"""Isolated tray/server process probe; no native tray or external services.

Writes synthetic data only under the calling test's explicit external root.
"""
from pathlib import Path
import json
import os
import socket
import sys
import threading
import time


root = Path(sys.argv[1]).resolve()
mode = sys.argv[2]
allowed = Path(os.environ["PIXIV_TEST_ARTIFACT_ROOT"]).resolve()
if not root.is_relative_to(allowed) or not root.relative_to(allowed).parts[0].startswith("b7-shutdown-"):
    raise ValueError("Requires a new isolated b7-shutdown test-run directory")
root.mkdir(parents=True, exist_ok=False)
os.environ["DATA_ROOT"] = str(root)
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
# Block dotenv reading even on older dependency versions.
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    os.environ["PORT"] = str(probe.getsockname()[1])

# Keep the production dual-socket main() path, restricting this probe's binds
# to loopback rather than exposing a synthetic service on LAN interfaces.
class LocalSocket(socket.socket):
    def bind(self, address):
        host, *rest = address
        host = "::1" if host == "::" else "127.0.0.1" if host == "0.0.0.0" else host
        return super().bind((host, *rest))

socket.socket = LocalSocket
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import auth, tray, web
from src.core.runtime import Runtime
from src.core.tasks import ItemResult, ItemSpec

entered, release = threading.Event(), threading.Event()
runtimes = []
operation_ids = []
notices = []
timing = {}


class Held:
    def plan(self, parameters, context):
        if mode == "check":
            entered.set()
            assert release.wait(4)
            context.report("checking", 0, 1, "works", "checkpoint")
        phase, kind = ("converting", "gif") if mode == "local" else ("downloading", "download")
        return [ItemSpec(str(index), kind, {}, phase, depends_on=() if index == 0 else ("0",)) for index in range(2)]

    def execute(self, item, context):
        entered.set()
        assert release.wait(4)
        if mode == "download":
            context.report_bytes(1, 100)
        return ItemResult()


def factory():
    if mode == "factory":
        entered.set()
        assert release.wait(4)
    # These modes simulate an already connected task reaching its safe exit
    # boundary. Held never performs network I/O; idle/offline modes stay offline.
    runtime = Runtime(root / "data/archive.db", root / "images", create=True,
                      client=object() if mode in ("check", "download", "local") else None)
    runtime.maintenance.tick = lambda: None
    runtime.queue.handlers["repair"] = Held()
    if mode == "paused":
        runtime.queue.set_automatic_paused(True)
        operation_ids.append(runtime.queue.submit("repair", {}, "paused", source="automatic")["operation_id"])
    elif mode in ("check", "download", "local"):
        operation_ids.append(runtime.queue.submit("repair", {}, mode)["operation_id"])
    runtimes.append(runtime)
    return runtime


original_create = web.create_app
web.create_app = lambda **kwargs: original_create(factory, authenticate=False, schedule=False)
if mode == "auth":
    def held_auth():
        entered.set()
        assert release.wait(4)
        return True
    auth._try_refresh_token = held_auth
    web.cancel_auth = release.set
    web.app = original_create(factory, authenticate=True, schedule=False)


class Icon:
    def __init__(self):
        self.stopped = threading.Event()

    def notify(self, message, title):
        notices.append(message)

    def stop(self):
        self.stopped.set()

    def run(self):
        if mode == "factory":
            assert entered.wait(5)
            threading.Timer(.25, release.set).start()
        elif mode != "starting":
            deadline = time.monotonic() + 5
            while web._server is None or not web._server.started:
                assert time.monotonic() < deadline, "server did not start"
                time.sleep(.005)
            if mode in ("check", "download", "local", "auth"):
                assert entered.wait(3)
                threading.Timer(.25, release.set).start()
        start = time.monotonic()
        tray._quit_app(self)
        tray._quit_app(self)
        timing["callback_seconds"] = time.monotonic() - start
        timing["waiting_visible"] = "正在退出" in self.title and not self.stopped.is_set()
        assert self.stopped.wait(5), "tray did not stop after service shutdown"
        timing["exit_seconds"] = time.monotonic() - start


icon = Icon()
tray._create_icon = lambda: icon
tray._open_gallery = lambda: None
tray.run_tray(offline=mode != "auth")
assert all(runtime.database._closed and runtime.lease._stream is None for runtime in runtimes)
assert web._server is None
states = []
if runtimes:
    from sqlalchemy import select
    from src.core.database import ArchiveDatabase
    from src.core.models import Operation, OperationItem
    db = ArchiveDatabase(root / "data/archive.db")
    with db.read() as session:
        for op in operation_ids:
            operation = session.get(Operation, op)
            states.append({"state": operation.state, "items": list(session.scalars(select(OperationItem.state).where(
                OperationItem.operation_id == op).order_by(OperationItem.position)))})
    db.close()
result = {"mode": mode, **timing, "states": states, "notices": notices, "closed": True}
(root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
