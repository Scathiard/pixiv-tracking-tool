"""Read only selected diagnostic volumes; retain payload-free correlation data."""
import collections
import json
import sys
from pathlib import Path

root = Path(r"D:\PixivTracker\data")
output = Path(__file__).parent
parts = [int(value) for value in sys.argv[1:]] or [39, 40, 41]
allowed = set("event utc seq trace parent span category method route status client_request client_session request_id operation_id session browser_event outcome reason error_type error_source duration_ms wait_ms server_trace server_run at_ms utc_ms attempt count aborted visible headers_ms database_ms queue_ms pool_ms execute_ms statements last_read_stage last_stage_ms slowest_read_stage slowest_stage_ms endpoint".split())
events = []
counts = collections.Counter()
routes = collections.defaultdict(list)
browsers = collections.Counter()
prefixes = collections.Counter()
files = []
for part in parts:
    path = root / f"diagnostic-20261017-232715-11576-973f0fc8794d-part{part:04d}.jsonl"
    if not path.exists():
        continue
    files.append(path.name)
    with path.open("rb") as source:
        for line in source:
            event = line.split(b'"', 4)[3] if line.startswith(b'{"event":"') else b''
            prefixes[event.decode(errors="replace")] += 1
            if event not in (b"browser.event", b"http.request.end", b"local_read.end", b"pixiv.request.end"):
                continue
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            keep = False
            if event == b"http.request.end":
                route = row.get("route", "")
                status = row.get("status", 0)
                counts[(route, status)] += 1
                if "bookmark" in route or "cursor" in route or route in ("/api/works/{work_id}", "/api/library/locate"):
                    routes[route].append(row.get("duration_ms", 0))
                    keep = True
                elif status and status >= 400 and route.startswith("/api/"):
                    keep = True
            elif event == b"browser.event":
                name = row.get("browser_event", "")
                category = row.get("category", "")
                browsers[(name, category, row.get("outcome", ""))] += 1
                keep = name in ("browser.error", "browser.unhandled", "browser.navigation", "browser.start", "browser.visibility") or (category in ("bookmark", "gallery", "detail", "flow") and not name.startswith("media."))
            elif event == b"pixiv.request.end":
                keep = True
            if keep:
                clean = {key: value for key, value in row.items() if key in allowed}
                clean["source_part"] = part
                events.append(clean)
summary = {
    "files": files,
    "event_counts": dict(prefixes.most_common(40)),
    "routes": [{"route": route, "status": status, "count": count} for (route, status), count in counts.most_common()],
    "browser_counts": [{"event": name, "category": category, "outcome": outcome, "count": count} for (name, category, outcome), count in browsers.most_common()],
    "timings": {route: {"n": len(values), "median_ms": sorted(values)[len(values)//2], "max_ms": max(values)} for route, values in routes.items()},
    "selected_events": len(events),
}
(output / "log-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
(output / "log-events.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in events), encoding="utf-8")
print(json.dumps(summary, ensure_ascii=False, indent=2))
