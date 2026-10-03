"""Measure real localhost admission while a synthetic remote step waits."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    source, out = args.source_db.resolve(), args.artifacts.resolve()
    allowed = next(parent / ".devtools" / "test-runs" for parent in Path(__file__).resolve().parents
                   if (parent / ".devtools" / "test-runs").is_dir()).resolve()
    # Source fixtures live in the main repository's explicitly isolated area.
    assert source.is_relative_to(allowed) and source.name == "archive.db"
    assert out.is_relative_to(allowed)
    out.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(out / "configuration")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.core.models import Artist, ArchiveIntent, Work
    from src.core.runtime import Runtime
    from src.core.tasks import ItemResult, ItemSpec
    from src.web import create_app
    import requests
    import uvicorn
    shutil.copyfile(source, out / "archive.db")
    class Remote:
        def get_account_id(self):
            return "9"
    entered, release = threading.Event(), threading.Event()
    class Held:
        def plan(self, parameters, context):
            return [ItemSpec("checked", "checked", {}, "checking")]
        def execute(self, item, context):
            entered.set()
            assert release.wait(60), "synthetic gate timed out"
            return ItemResult()
    runtime = Runtime(out / "archive.db", out / "images", client=Remote())
    with runtime.database.write() as session:
        session.add(Artist(id=1, pixiv_user_id="1", name="synthetic"))
        session.flush()
        for ident in range(1, 21):
            session.add_all([Work(id=ident, pixiv_illust_id=str(100 + ident), artist_id=1, title=f"local-{ident}"),
                             ArchiveIntent(pixiv_illust_id=str(100 + ident))])
    runtime.queue.handlers["reconcile"] = runtime.queue.handlers["metadata"] = Held()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False,
        schedule=False, reading_ui=True), host="127.0.0.1", port=port, log_level="error",
        loop="src.networking:http_loop_factory"))
    serving = threading.Thread(target=server.run, daemon=True)
    serving.start()
    held = threading.Thread(target=lambda: runtime.queue._step("check"), daemon=True)
    result = {"source": "isolated synthetic 100000-item copy", "real_account_used": False,
              "submit_ms": [], "browse_ms": [], "counts_ms": []}
    try:
        deadline = time.monotonic() + 25
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        held.start()
        assert entered.wait(15)
        ids = set()
        with requests.Session() as client:
            for ident in range(1, 21):
                assert held.is_alive() and not release.is_set()
                started = time.perf_counter()
                response = client.post(origin + "/api/tasks", json={"kind": "metadata", "work_ids": [ident]},
                                       headers={"Origin": origin}, timeout=10)
                result["submit_ms"].append((time.perf_counter() - started) * 1000)
                response.raise_for_status()
                accepted = response.json()
                assert accepted["created"] is True and accepted["state"] == "queued", accepted
                assert accepted["operation_id"] not in ids
                ids.add(accepted["operation_id"])
                started = time.perf_counter()
                response = client.get(origin + f"/api/works/{ident}", timeout=10)
                result["browse_ms"].append((time.perf_counter() - started) * 1000)
                response.raise_for_status()
                assert response.json()["title"] == f"local-{ident}"
                if ident % 5 == 0:
                    started = time.perf_counter()
                    response = client.get(origin + "/api/tasks/scale-op?limit=1", timeout=10)
                    result["counts_ms"].append((time.perf_counter() - started) * 1000)
                    response.raise_for_status()
                    assert sum(response.json()["counts"].values()) == 100000
        assert held.is_alive() and not release.is_set()
        for field in ("submit", "browse", "counts"):
            values = sorted(result[field + "_ms"])
            result[field + "_p95_ms"] = values[int((len(values) - 1) * .95)]
            result[field + "_max_ms"] = max(values)
        result["distinct_created"] = len(ids)
        result["remote_still_waiting_during_all_calls"] = True
        assert result["submit_p95_ms"] <= 500
        assert result["browse_max_ms"] < 1000
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        release.set()
        held.join(15)
        server.should_exit = True
        serving.join(15)
        assert not held.is_alive() and not serving.is_alive()


if __name__ == "__main__":
    main()
