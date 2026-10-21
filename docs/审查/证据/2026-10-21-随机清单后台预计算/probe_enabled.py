"""One enabled-instance observation, without account writes or library maintenance."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser()
parser.add_argument("--launch", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
args = parser.parse_args()
launch = json.loads(args.launch.read_text(encoding="utf-8"))
origin = "http://127.0.0.1:1919"
result = {"build": "20261021.4", "observed_at": datetime.now().astimezone().isoformat(),
          "errors": [], "real_library": True, "account_writes": False, "full_library_repeated_query": False}

def read(path, method="GET", timeout=20):
    started = time.monotonic()
    with urlopen(Request(origin + path, method=method, headers={"Origin": origin}), timeout=timeout) as response:
        value = json.load(response)
        return value, round(time.monotonic() - started, 6), response.headers

def events(run_id):
    found = []
    for path in sorted(Path("D:/PixivTracker/data").glob(f"diagnostic-*-{run_id}-part*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if '"event":"reading.prepar' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # Writer may still be finishing its last line.
                found.append({key: row[key] for key in ("event", "utc", "elapsed_ms", "seq", "generation",
                    "count", "duration_ms", "total_ms", "outcome", "error_type") if key in row})
    return found

token = None
try:
    deadline = time.monotonic() + 240
    while True:
        try:
            _, _, headers = read("/api/status", timeout=3)
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError("Enabled instance did not become reachable")
            time.sleep(1)
    result["observed_ready_seconds_after_launch"] = round(
        (datetime.now().astimezone() - datetime.fromisoformat(launch["launched_at"])).total_seconds(), 3)
    run_id = headers.get("x-pixiv-diagnostic-run")
    assert run_id, "Enabled diagnostic run header missing"
    result["diagnostic_run"] = run_id
    deadline = time.monotonic() + 240
    while True:
        observed = events(run_id)
        if any(row["event"] == "reading.preparation.full" for row in observed) and any(
                row["event"] == "reading.preparation.prepared" for row in observed):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("Background preparation not observed within observation window")
        time.sleep(2)
    result["before_manual_session_events"] = observed
    visit, elapsed, _ = read("/api/reading-sessions", "POST")
    token = visit["session_id"]
    result["prepared_session"] = {"works": visit["total"], "pages": visit["page_total"], "seconds": elapsed}
    entry, _, _ = read(f"/api/reading-sessions/{token}/entry?index=0&mode=random")
    assert entry["total"] == visit["total"] and entry["work_id"] > 0
    result["random_entry_valid"] = True
    gallery, elapsed, _ = read("/api/library/cursor?limit=24")
    result["gallery"] = {"items": len(gallery["items"]), "seconds": elapsed}
    settings, _, _ = read("/api/settings")
    result["settings"] = {"login": settings["login"]["status"], **{key: settings[key] for key in (
        "automatic_paused", "current_port", "check_interval_minutes", "data_root")}}
    result["final_preparation_events"] = events(run_id)
except Exception as exc:
    result["errors"].append(f"{type(exc).__name__}: {exc}")
finally:
    if token:
        try:
            read(f"/api/reading-sessions/{token}", "DELETE")
        except Exception as exc:
            result["errors"].append(f"Release: {type(exc).__name__}: {exc}")
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(bool(result["errors"]))
