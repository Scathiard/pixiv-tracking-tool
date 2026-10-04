"""Bounded real HTTP probe: production settings router, no database/config/account."""
import asyncio
import http.client
import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import uvicorn
from fastapi import FastAPI

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "app"))

# Prevent config/auth/scheduler imports from reading real .env or token files.
# None of these unrelated settings handlers is called by this GET-only probe.
config = ModuleType("src.config")
sys.modules["src.config"] = config
auth = ModuleType("src.auth")
auth.get_pending_login_url = lambda: None
sys.modules["src.auth"] = auth
scheduler = ModuleType("src.scheduler")
scheduler.interval_snapshot = lambda _: {}
scheduler.save_interval = lambda *_: None
sys.modules["src.scheduler"] = scheduler

from src.routes import settings_api

entered = threading.Event()
release = threading.Event()
finished = threading.Event()
disconnect = threading.Event()
timeline = []
started_at = time.perf_counter()

def stamp(event):
    timeline.append({"event": event, "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 3)})

sentinel = object()
def controlled_log(database):
    assert database is sentinel
    stamp("processing_log_entered")
    entered.set()
    reason = "explicit_release" if release.wait(1.5) else "bounded_safety_timeout"
    stamp("processing_log_finished_" + reason)
    finished.set()
    return {"lines": ["synthetic log"], "truncated": False}

settings_api.processing_log = controlled_log
app = FastAPI()
app.include_router(settings_api.create_settings_router(lambda: SimpleNamespace(database=sentinel)))

class ObserveDisconnect:
    """GET-only transport observation; does not cancel or change the route task."""
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        async def observe():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    stamp("asgi_http_disconnect")
                    disconnect.set()
                    return
        watcher = asyncio.create_task(observe())
        try:
            await app(scope, receive, send)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen(4)
port = listener.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(ObserveDisconnect(), host="127.0.0.1", port=port,
    access_log=False, log_level="warning", lifespan="off", loop="asyncio", http="h11", ws="none",
    timeout_graceful_shutdown=2))
thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
result = {"scope": "127.0.0.1 random port; one real GET; synthetic Event log; no DB/config/token/account"}
try:
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started, "Server did not start"
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        client.request("GET", "/api/logs")
        assert entered.wait(2), "Route did not enter controlled processing_log"
        client.sock.settimeout(.15)
        try:
            client.getresponse()
            raise AssertionError("Expected bounded client read timeout")
        except TimeoutError:
            stamp("http_client_read_timeout")
    finally:
        client.close()
        stamp("http_client_closed")
    assert disconnect.wait(.4), "No ASGI disconnect was observed"
    active_after_disconnect = not finished.is_set()
    assert active_after_disconnect, "Server synchronous function unexpectedly finished on disconnect"
    stamp("processing_log_still_active_after_disconnect")
    release.set()
    assert finished.wait(.5), "Controlled function did not finish after explicit release"
    result.update({"client_read_timeout_seconds": .15, "service_read_safety_limit_seconds": 1.5,
        "asgi_disconnect_observed": True, "processing_log_active_after_disconnect": active_after_disconnect,
        "explicit_release_completed": True})
finally:
    release.set()
    server.should_exit = True
    thread.join(3)
    listener.close()
    result["server_exited_normally"] = not thread.is_alive()
    result["timeline"] = timeline
    (HERE / "server-disconnect-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
assert result["server_exited_normally"], "Server did not exit normally"
result["passed"] = True
(HERE / "server-disconnect-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
