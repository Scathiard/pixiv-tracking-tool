"""Read a bounded snapshot of existing diagnostics; never open the catalogue."""
import datetime
import json
import os
from pathlib import Path
import sys


source, destination = map(Path, sys.argv[1:3])
with source.open("rb") as stream:
    snapshot_size = os.fstat(stream.fileno()).st_size
    data = stream.read(snapshot_size)
rows = []
for line in data.splitlines():
    try:
        rows.append(json.loads(line))
    except (ValueError, UnicodeDecodeError):
        pass
del data

traces = {
    row["trace"]
    for row in rows
    if row.get("endpoint") == "reading_session" and row.get("trace")
}
request_ids = {
    row.get("client_request")
    for row in rows
    if row.get("event") == "http.request.begin" and row.get("trace") in traces
}
workers = {
    row["thread"]
    for row in rows
    if row.get("event") == "read.worker.begin" and row.get("trace") in traces
}
definitions = {
    row["sql_id"]: row
    for row in rows
    if row.get("event") == "sql.definition"
}
trace_rows = [
    row for row in rows
    if row.get("trace") in traces
    and row.get("event") not in ("sql.aggregate", "event.aggregate")
]
active_by_trace = {}
for row in rows:
    if row.get("event") == "watch.active":
        for span in row.get("spans", []):
            if span.get("trace") in traces:
                active_by_trace[span["trace"]] = {
                    "utc": row["utc"],
                    "seq": row["seq"],
                    "spans": [
                        item for item in row["spans"]
                        if item.get("trace") == span["trace"]
                    ],
                }
stack_by_worker = {}
for row in rows:
    if row.get("event") == "thread.stack" and row.get("inspected_thread") in workers:
        stack_by_worker[row["inspected_thread"]] = row

used_sql = {
    row["sql_id"] for row in trace_rows if row.get("sql_id")
}
for value in active_by_trace.values():
    used_sql.update(span["sql_id"] for span in value["spans"] if span.get("sql_id"))

result = {
    "source": str(source),
    "snapshot_bytes": snapshot_size,
    "captured_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "record_count": len(rows),
    "run_start": next((row for row in rows if row.get("event") == "diagnostic.start"), None),
    "reading_traces": sorted(traces),
    "trace_events": trace_rows,
    "browser_flow": [
        row for row in rows
        if row.get("event") == "browser.event"
        and (row.get("category") == "flow" or row.get("request_id") in request_ids)
    ],
    "last_active": active_by_trace,
    "last_worker_stacks": stack_by_worker,
    "manifest_stacks": [
        row for row in rows
        if row.get("event") == "thread.stack"
        and any("reading_manifest" in frame for frame in row.get("stack", []))
    ],
    "tag_completions": [
        row for row in rows
        if row.get("event") == "read.completed" and row.get("endpoint") == "tags"
    ],
    "sql_definitions": {key: definitions.get(key) for key in sorted(used_sql)},
    "last_sample": next((row for row in reversed(rows) if row.get("event") == "process.sample"), None),
}
destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({
    "snapshot_bytes": snapshot_size,
    "records": len(rows),
    "traces": sorted(traces),
    "completions": [row for row in trace_rows if row.get("event") == "read.completed"],
    "manifest_stacks": result["manifest_stacks"],
    "tag_completions": result["tag_completions"],
}, ensure_ascii=False, indent=2))
