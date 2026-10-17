"""Read only the final diagnostic part and emit fixed-field, redacted statistics.

No database, credentials, account requests, or other diagnostic parts are read.
Run from the repository root with the existing isolated Python interpreter.
"""
import collections
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics

SOURCE = Path(r"D:\PixivTracker\data\diagnostic-20261013-215839-6296-7c2dedefc7d6-part0314.jsonl")
OUTPUT = Path(__file__).with_name("tail-summary.json")


def select(row, keys):
    return {key: row[key] for key in keys if key in row}


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {"count": len(ordered), "min": ordered[0], "median": statistics.median(ordered),
            "p95_nearest_rank": ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)], "max": ordered[-1]}


rows = []
invalid_lines = []
digest = hashlib.sha256()
with SOURCE.open("rb") as stream:
    for number, raw in enumerate(stream, 1):
        digest.update(raw)
        try:
            rows.append(json.loads(raw))
        except (ValueError, UnicodeError):
            invalid_lines.append(number)

samples = [row for row in rows if row["event"] == "process.sample"]
first, last = samples[0], samples[-1]
sample_seconds = (datetime.fromisoformat(last["utc"]) - datetime.fromisoformat(first["utc"])).total_seconds()
cumulative = ["log_bytes", "log_records", "log_write_ms", "log_flush_ms", "log_flush_count",
              "writer_cpu_ms", "watch_cpu_ms", "read_bytes", "write_bytes", "cpu_user", "cpu_system"]
delta = {key: last[key] - first[key] for key in cumulative}
process_cpu_delta = delta["cpu_user"] + delta["cpu_system"]
probe_cpu_delta = (delta["writer_cpu_ms"] + delta["watch_cpu_ms"]) / 1000
aggregates = collections.defaultdict(lambda: {"count": 0, "duration_ms": 0, "max_ms": 0})
for row in rows:
    if row["event"] == "event.aggregate":
        dimensions = tuple((key, row[key]) for key in ["observed_event", "lane", "stage", "outcome", "reason", "thread_name"] if key in row)
        summary = aggregates[dimensions]
        summary["count"] += row["count"]
        summary["duration_ms"] += row["duration_ms"]
        summary["max_ms"] = max(summary["max_ms"], row["max_ms"])

span_rows = collections.defaultdict(list)
for row in rows:
    if row["event"] == "watch.active":
        for span in row["spans"]:
            span_rows[span["span"]].append((row["utc"], span))
span_summary = []
for values in span_rows.values():
    initial, final = values[0], values[-1]
    span_summary.append({"samples": len(values), "first_seen_utc": initial[0], "last_seen_utc": final[0],
                         "event": final[1]["event"], "first_age_ms": initial[1]["age_ms"],
                         "last_age_ms": final[1]["age_ms"], "max_age_ms": max(item[1]["age_ms"] for item in values)})

event_counts = collections.Counter(row["event"] for row in rows)
shutdown_events = ["tray.quit_requested", "shutdown.requested", "task.stop_requested", "task.stop_result",
                   "task.worker_exit", "runtime.close.begin", "runtime.close.end", "application.run.end", "diagnostic.stop"]
http = [select(row, ["utc", "route", "category", "status", "duration_ms", "disconnected"])
        for row in rows if row["event"] == "http.request.end"]
sql_slow = [select(row, ["utc", "duration_ms", "error_type"]) for row in rows if row["event"] == "sql.slow"]
sql_aggregate = [row for row in rows if row["event"] == "sql.aggregate"]
checkpoints = [row for row in rows if row["event"] == "wal.checkpoint.end"]
errors = [select(row, ["utc", "event", "observed_event", "outcome", "error_type", "status", "duration_ms"])
          for row in rows if row.get("error_type") or row.get("outcome") == "error"]
summary = {
    "scope": "Complete final part only; no earlier parts, database, credentials, account requests, startup, or tests",
    "source_name": SOURCE.name, "source_bytes": SOURCE.stat().st_size, "source_sha256": digest.hexdigest(),
    "parsed_records": len(rows), "invalid_line_numbers": invalid_lines,
    "first": select(rows[0], ["event", "utc", "elapsed_ms", "first_sequence", "part", "build"]),
    "last": select(rows[-1], ["event", "utc", "elapsed_ms", "seq"]),
    "part_utc_span_seconds": (datetime.fromisoformat(rows[-1]["utc"]) - datetime.fromisoformat(rows[0]["utc"])).total_seconds(),
    "event_counts": dict(event_counts), "shutdown_event_counts": {key: event_counts[key] for key in shutdown_events},
    "sample_interval": {"first_utc": first["utc"], "last_utc": last["utc"], "seconds": sample_seconds,
                        "first": select(first, cumulative), "last": select(last, cumulative), "delta": delta,
                        "log_bytes_per_second_from_delta": delta["log_bytes"] / sample_seconds,
                        "log_records_per_second_from_delta": delta["log_records"] / sample_seconds,
                        "process_cpu_seconds": process_cpu_delta, "two_probe_threads_cpu_seconds": probe_cpu_delta,
                        "two_probe_threads_one_core_percent": probe_cpu_delta / sample_seconds * 100,
                        "two_probe_threads_share_of_process_cpu_percent": probe_cpu_delta / process_cpu_delta * 100},
    "sample_distributions": {key: distribution([row[key] for row in samples]) for key in
                             ["event_loop_gap_ms", "queue_size", "active_spans", "rss_bytes", "threads", "log_bytes_per_second"]},
    "sample_cumulative_health": {key: {"first": first[key], "last": last[key], "maximum": max(row[key] for row in samples)}
                                 for key in ["dropped_records", "probe_errors", "aggregate_overflow", "queue_peak"]},
    "writer_alive_sample_counts": dict(collections.Counter(str(row["writer_alive"]) for row in samples)),
    "aggregates": [{**dict(key), **value} for key, value in sorted(aggregates.items(), key=lambda item: item[1]["count"], reverse=True)],
    "active_spans": sorted(span_summary, key=lambda item: item["max_age_ms"], reverse=True),
    "http_completed": http, "sql_slow": sql_slow,
    "sql_aggregate_totals": {"count": sum(row["count"] for row in sql_aggregate),
                             "errors": sum(row["errors"] for row in sql_aggregate),
                             "summed_duration_ms": sum(row["duration_ms"] for row in sql_aggregate)},
    "wal_checkpoint": {"count": len(checkpoints), "duration_ms": distribution([row["duration_ms"] for row in checkpoints]),
                        "busy_values": dict(collections.Counter(str(row["busy"]) for row in checkpoints)),
                        "last": select(checkpoints[-1], ["utc", "mode", "busy", "log_frames", "checkpointed_frames", "pending_frames", "status"])},
    "browser_event_counts": [{"browser_event": key[0], "outcome": key[1], "status": key[2], "count": count}
                             for key, count in collections.Counter((row.get("browser_event"), row.get("outcome"), row.get("status"))
                             for row in rows if row["event"] == "browser.event").items()],
    "errors": errors,
    "limitations": ["Aggregates carry activity since the previous sampling boundary and are not an exact UTC window transaction ledger.",
                    "Writer/watchdog CPU counters exclude probe work performed on business threads; no diagnostic-off comparison exists.",
                    "Process I/O counters and buffered log byte counts do not measure physical disk I/O or storage latency.",
                    "No normal shutdown marker was found; absence alone cannot distinguish force termination, host exit, crash, or lost tail writes.",
                    "Polling and WAL activity do not prove item completion; the tail contains no item execution or settlement events."]
}
OUTPUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"output": str(OUTPUT), "records": len(rows), "invalid_lines": len(invalid_lines),
                  "sample_delta": summary["sample_interval"]}, ensure_ascii=False))
