"""Pure replacement transport: production RemoteAccess task-scope lookup audit."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import json
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "app"))

import requests
from src.client import PixivClient
from src.core.runtime import RemoteAccess


def probe(proxied):
    calls, waits, checkpoints = [], [], []
    client = PixivClient()
    client._stopping = SimpleNamespace(is_set=lambda: False,
        wait=lambda delay: waits.append(delay) or False)
    proxy = RemoteAccess(SimpleNamespace(client=client, _closing=threading.Event()))
    task_client = proxy if proxied else client
    # Same type-based lookup as TaskQueue._plan and _execute_item.
    scope_factory = getattr(type(task_client), "task_scope", None)
    context = SimpleNamespace(checkpoint=lambda: checkpoints.append(True))

    def synthetic_transport(*args, **kwargs):
        calls.append(True)
        raise requests.exceptions.ConnectionError("synthetic replacement transport")

    original = requests.get
    requests.get = synthetic_transport
    try:
        with scope_factory(task_client, context) if callable(scope_factory) else nullcontext():
            try:
                task_client._do_get("/synthetic", {}, {})
            except requests.exceptions.ConnectionError:
                pass
    finally:
        requests.get = original
    return {"scope_installed": callable(scope_factory), "replacement_transport_calls": len(calls),
        "would_wait_seconds": waits, "task_checkpoint_calls": len(checkpoints)}


if __name__ == "__main__":
    proxied, direct = probe(True), probe(False)
    assert proxied["replacement_transport_calls"] == 3 and not proxied["scope_installed"], proxied
    assert direct["replacement_transport_calls"] == 1 and direct["scope_installed"], direct
    print(json.dumps({"network_used": False, "persistent_database_created": False,
        "production_proxy_lookup": proxied, "direct_client_lookup": direct}, indent=2))
