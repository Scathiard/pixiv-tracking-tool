"""Read exactly parts 0001/0002 and retain fixed-field evidence for two traces."""
import collections
from datetime import datetime
import hashlib
import json
from pathlib import Path

BASE = Path(r"D:\PixivTracker\data")
PREFIX = "diagnostic-20261013-215839-6296-7c2dedefc7d6-part"
TRACES = {
    "6e368863ea0b": {"endpoint": "cursor_library", "start": "2026-10-13T14:00:05", "end": "2026-10-13T14:01:57"},
    "ffd35ba4d335": {"endpoint": "settings", "start": "2026-10-13T14:08:38", "end": "2026-10-13T14:12:44"},
}
SAFE = {"event", "utc", "seq", "trace", "parent", "span", "thread", "elapsed_ms", "duration_ms",
        "started_elapsed_ms", "begin_recorded", "endpoint", "lane", "stage", "last_read_stage", "last_stage_ms",
        "total_ms", "queue_ms", "pool_ms", "database_ms", "execute_ms", "statement_count", "dispatch_wait_ms",
        "selected_count", "row_count", "decode_ms", "assemble_ms", "outcome", "error_type", "sql_id", "rowcount",
        "command", "tables", "caller", "active_count", "pending_count", "capacity", "consumer_count", "cancelled",
        "after_cancel_ms", "timed_out", "task_done", "route", "status", "disconnected", "headers_ms", "send_wait_ms",
        "send_calls", "after_business_ms", "first_body_ms", "last_body_ms", "category", "method", "bytes_sent"}
incidents = {trace: {"endpoint": config["endpoint"], "events": [], "active_span_summary": [], "worker_stacks": [], "process_samples": []}
             for trace, config in TRACES.items()}
definitions = {}
active = collections.defaultdict(list)
metadata = []
for part in (1, 2):
    source = BASE / f"{PREFIX}{part:04d}.jsonl"
    digest = hashlib.sha256()
    count = 0
    invalid = []
    first = last = None
    with source.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            digest.update(raw)
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError):
                invalid.append(number)
                continue
            count += 1
            first = first or row["utc"]
            last = row["utc"]
            trace = row.get("trace")
            if row["event"] == "sql.definition":
                definitions[row["sql_id"]] = {key: row[key] for key in ["sql_id", "command", "tables", "caller"]}
            if trace in TRACES:
                incidents[trace]["events"].append({key: value for key, value in row.items() if key in SAFE})
            if row["event"] == "watch.active":
                for span in row["spans"]:
                    if span.get("trace") in TRACES:
                        key = (span["trace"], span["event"], span.get("sql_id"), span.get("thread"))
                        active[key].append((row["utc"], span["age_ms"]))
            for trace, config in TRACES.items():
                if config["start"] <= row["utc"] <= config["end"]:
                    if row["event"] == "thread.stack" and row.get("inspected_thread") == 29724:
                        incidents[trace]["worker_stacks"].append({key: row[key] for key in ["utc", "thread_name", "inspected_thread", "stack_id", "stack"]})
                    if row["event"] == "process.sample":
                        incidents[trace]["process_samples"].append({key: row[key] for key in
                            ["utc", "event_loop_gap_ms", "active_spans", "queue_size", "dropped_records", "probe_errors", "rss_bytes",
                             "cpu_user", "cpu_system", "read_bytes", "write_bytes", "writer_cpu_ms", "watch_cpu_ms"]})
    metadata.append({"source_name": source.name, "source_bytes": source.stat().st_size, "sha256": digest.hexdigest(),
                     "records": count, "invalid_line_numbers": invalid, "first_utc": first, "last_utc": last})
for (trace, event, sql_id, thread), values in active.items():
    incidents[trace]["active_span_summary"].append({"event": event, "sql_id": sql_id, "thread": thread,
        "samples": len(values), "first_seen_utc": values[0][0], "last_seen_utc": values[-1][0],
        "first_age_ms": values[0][1], "last_age_ms": values[-1][1], "max_age_ms": max(item[1] for item in values)})
for incident in incidents.values():
    sql_ids = {row["sql_id"] for row in incident["events"] if "sql_id" in row}
    incident["sql_definitions"] = [definitions[key] for key in sorted(sql_ids) if key in definitions]
result = {"scope": "Complete parts 0001 and 0002 only; fixed fields from two approved slow-read traces; no SQL text, params, credentials, database or account requests",
          "sources": metadata, "incidents": incidents,
          "limitations": ["SQL execution instrumentation surrounds cursor execution, not fetch-all result consumption; native work can include disk waits and engine work.",
                          "sql.definition tables are parser-extracted references and can include CTE names, not only physical tables.",
                          "Full stacks are emitted on fingerprint changes; watch.active supplies repeat observations but does not resolve native SQLite wait causes.",
                          "Measurements show where time was spent, not whether CPU, storage, query planning, locks or competing activity caused native delays."]}
output = Path(__file__).with_name("incident-summary.json")
output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"sources": metadata, "incidents": {trace: {"events": len(value["events"]), "worker_stacks": len(value["worker_stacks"]),
    "sql_definitions": len(value["sql_definitions"])} for trace, value in incidents.items()}}, ensure_ascii=False))
