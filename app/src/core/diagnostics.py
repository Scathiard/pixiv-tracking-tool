"""Opt-in process diagnostics. No database, account or request payload reads.

Producers only enqueue bounded records. A separate writer batches disk writes;
the watchdog can describe a blocked producer before its span has returned.
Fast SQL is counted by fingerprint, while slow SQL retains individual timing.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import uuid

_current: ContextVar[tuple[str, str]] = ContextVar("diagnostic_context", default=("", ""))
_recorder = None
DEFAULT_PART_BYTES = 64 * 1024 * 1024
FAST_SPAN_MS = 25.0
_COMPACT_EVENTS = frozenset(("read.connection_attached", "read.connection_detached", "read.budget_created"))
_DIMENSIONS = frozenset(("lane", "endpoint", "stage", "outcome", "reason", "kind", "mode", "thread_name"))
_private = {"token", "password", "secret", "authorization", "cookie", "headers",
    "parameters", "params", "payload", "target", "result", "body", "url", "path",
    "statement", "sql", "message", "account_id"}


def _clean(value, depth=0):
    if depth > 4:
        return "[depth]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        value = re.sub(r"https?://\S+|[A-Za-z]:[\\/][^\s]*", "[redacted]", value)
        return value[:256]
    if isinstance(value, dict):
        return {str(k)[:64]: _clean(v, depth + 1) for k, v in list(value.items())[:64]
                if str(k).lower() not in _private and not any(word in str(k).lower()
                for word in ("token", "password", "authorization", "cookie", "secret"))}
    if isinstance(value, (list, tuple, set)):
        return [_clean(item, depth + 1) for item in list(value)[:64]]
    return type(value).__name__


def _location(frame):
    filename = frame.f_code.co_filename.replace("\\", "/")
    module = frame.f_globals.get("__name__", "")
    if isinstance(module, str) and module.startswith("src."):
        filename = module.replace(".", "/") + ".py"
    elif "/src/" in filename:
        filename = "src/" + filename.rsplit("/src/", 1)[1]
    elif "/site-packages/" in filename:
        filename = filename.rsplit("/site-packages/", 1)[1]
    else:
        filename = filename.rsplit("/", 1)[-1]
    return f"{filename}:{frame.f_lineno}:{frame.f_code.co_name}"


def _caller():
    frame = sys._getframe(2)
    for _ in range(28):
        if frame is None:
            break
        filename = frame.f_code.co_filename.replace("\\", "/")
        module = frame.f_globals.get("__name__", "")
        application_frame = isinstance(module, str) and module.startswith("src.")
        if (application_frame or "/src/" in filename) and not filename.endswith(("diagnostics.py", "database.py")):
            return _location(frame)
        frame = frame.f_back
    return "unknown"


class Recorder:
    def __init__(self, directory, *, build, version, interval=2.0, max_bytes=DEFAULT_PART_BYTES):
        if max_bytes <= 0:
            raise ValueError("Diagnostic part size must be positive")
        self.started = time.monotonic()
        self.run_id = uuid.uuid4().hex[:12]
        self.salt = os.urandom(16)
        self.interval = interval
        self.max_bytes = max_bytes
        self.build, self.version = build, version
        self.queue = queue.Queue(maxsize=16384)
        self.lock = threading.Lock()
        self.active = {}
        self.sql_counts = {}
        self.sql_catalogue = set()
        self.aggregates = {}
        self.aggregate_overflow = 0
        self.probe_errors = 0
        self.queue_peak = 0
        self.written_bytes = 0
        self.written_records = 0
        self.write_ms = 0.0
        self.flush_ms = 0.0
        self.flush_count = 0
        self.writer_cpu_ms = 0.0
        self.watch_cpu_ms = 0.0
        self.last_sample_at = self.started
        self.last_sample_bytes = 0
        self.last_sample_records = 0
        self.stack_fingerprints = {}
        self.sequence = 0
        self.dropped = 0
        self.closed = False
        self.failed = False
        self.stop_event = threading.Event()
        self.heartbeat_at = None
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.part_prefix = f"diagnostic-{stamp}-{os.getpid()}-{self.run_id}"
        self.file = directory / f"{self.part_prefix}-part0001.jsonl"
        self.current_file = self.file
        self.part = 1
        self.part_bytes = 0
        self.part_records = 0
        self.last_sequence = 0
        self.stream = self.file.open("x", encoding="utf-8", buffering=65536)
        self.writer = threading.Thread(target=self._write, name="diagnostic-writer", daemon=True)
        self.watcher = threading.Thread(target=self._watch, name="diagnostic-watchdog", daemon=True)
        self.writer.start()
        self.put("diagnostic.start", build=build, version=version, pid=os.getpid(),
                 python=sys.version.split()[0], interval_seconds=interval, part_bytes=max_bytes,
                 retention="continuous parts; prior files retained", run_id=self.run_id,
                 sql_policy="all statements counted by fingerprint; individual slow or failed statements",
                 fast_span_ms=FAST_SPAN_MS, format_version=2)
        self.watcher.start()

    def put(self, event, **fields):
        if self.closed or self.failed:
            return
        trace, parent = _current.get()
        record = {"event": event, "utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
                  "thread": threading.get_ident(), "trace": trace, "parent": parent,
                  **_clean(fields)}
        with self.lock:
            self.sequence += 1
            record["seq"] = self.sequence
            try:
                self.queue.put_nowait(record)
                self.queue_peak = max(self.queue_peak, self.queue.qsize())
            except queue.Full:
                self.dropped += 1

    def aggregate(self, event, *, duration_ms=0, count=1, **dimensions):
        dimensions = _clean({key: value for key, value in dimensions.items() if key in _DIMENSIONS})
        key = (event, tuple(sorted(dimensions.items())))
        with self.lock:
            if key not in self.aggregates and len(self.aggregates) >= 512:
                self.aggregate_overflow += count
                return
            row = self.aggregates.setdefault(key, {"count": 0, "duration_ms": 0.0, "max_ms": 0.0})
            row["count"] += count
            row["duration_ms"] += duration_ms
            row["max_ms"] = max(row["max_ms"], duration_ms)

    def _write_line(self, line):
        started = time.perf_counter()
        self.stream.write(line)
        self.write_ms += (time.perf_counter() - started) * 1000
        size = len(line.encode("utf-8"))
        self.part_bytes += size
        self.written_bytes += size
        self.written_records += 1

    def _flush(self):
        started = time.perf_counter()
        self.stream.flush()
        self.flush_ms += (time.perf_counter() - started) * 1000
        self.flush_count += 1
        self.writer_cpu_ms = time.thread_time() * 1000

    def _write_marker(self, event, **fields):
        # Writer-owned boundaries have no producer sequence. Their adjacent
        # sequence values connect parts without duplicating business records.
        row = {"event": event, "utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "elapsed_ms": round((time.monotonic() - self.started) * 1000, 3),
               "run_id": self.run_id, "part": self.part, "build": self.build,
               "version": self.version, **fields}
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        self._write_line(line)

    def _rotate(self, next_sequence):
        next_file = self.file.with_name(f"{self.part_prefix}-part{self.part + 1:04d}.jsonl")
        # Open exclusively before sealing the old part, so a collision or disk
        # error cannot overwrite evidence or silently pretend rotation worked.
        next_stream = next_file.open("x", encoding="utf-8", buffering=65536)
        previous_file = self.current_file
        try:
            self._write_marker("diagnostic.part_end", next_file=next_file.name,
                               last_sequence=self.last_sequence, next_sequence=next_sequence)
            self._flush()
            self.stream.close()
        except BaseException:
            next_stream.close()
            raise
        self.stream = next_stream
        self.current_file = next_file
        self.part += 1
        self.part_bytes = 0
        self.part_records = 0
        self._write_marker("diagnostic.part_start", previous_file=previous_file.name,
                           first_sequence=next_sequence)

    def _write(self):
        try:
            self._write_marker("diagnostic.part_start", previous_file=None, first_sequence=1)
            while not self.stop_event.is_set() or not self.queue.empty():
                try:
                    first = self.queue.get(timeout=.2)
                except queue.Empty:
                    self._flush()
                    continue
                batch = [first]
                batch_deadline = time.monotonic() + .2
                for _ in range(255):
                    try:
                        batch.append(self.queue.get(timeout=max(0, batch_deadline - time.monotonic())))
                    except queue.Empty:
                        break
                for row in batch:
                    line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                    size = len(line.encode("utf-8"))
                    if self.part_records and self.part_bytes + size > self.max_bytes:
                        self._rotate(row["seq"])
                    # A single large JSON row stays intact, even if it exceeds
                    # the soft part size; rows are never cut between files.
                    self._write_line(line)
                    self.part_records += 1
                    self.last_sequence = row["seq"]
                self._flush()
        except Exception as error:
            self.failed = True
            try:
                print(f"[diagnostic] 持续日志写入失败，诊断记录已停止：{type(error).__name__}；"
                      "请检查磁盘空间和日志目录权限，现有分卷保留。", file=sys.stderr, flush=True)
            except Exception:
                pass
        finally:
            try:
                self.stream.close()
            except Exception:
                pass

    def sample(self):
        now = time.monotonic()
        with self.lock:
            active = [{**data, "age_ms": round((now - data["started"]) * 1000, 2)}
                      for data in self.active.values()]
            for data in active:
                data.pop("started", None)
            counts, self.sql_counts = self.sql_counts, {}
            aggregates, self.aggregates = self.aggregates, {}
            dropped = self.dropped
            health = {"probe_errors": self.probe_errors, "aggregate_overflow": self.aggregate_overflow,
                      "queue_peak": self.queue_peak}
        for index in range(0, len(active), 48):
            self.put("watch.active", spans=active[index:index + 48], total=len(active))
        for sql_id, counts_row in counts.items():
            self.put("sql.aggregate", sql_id=sql_id, **counts_row)
        for (event, dimensions), values in aggregates.items():
            self.put("event.aggregate", observed_event=event, **dict(dimensions), **values)
        metrics = {"active_spans": len(active), "queue_size": self.queue.qsize(), "dropped_records": dropped}
        elapsed = max(.001, now - self.last_sample_at)
        metrics.update(**health, log_bytes=self.written_bytes, log_records=self.written_records,
                       log_bytes_per_second=round((self.written_bytes - self.last_sample_bytes) / elapsed, 2),
                       log_records_per_second=round((self.written_records - self.last_sample_records) / elapsed, 2),
                       log_write_ms=round(self.write_ms, 3), log_flush_ms=round(self.flush_ms, 3),
                       log_flush_count=self.flush_count, writer_cpu_ms=round(self.writer_cpu_ms, 3),
                       watch_cpu_ms=round(self.watch_cpu_ms, 3), writer_alive=self.writer.is_alive())
        self.last_sample_at, self.last_sample_bytes, self.last_sample_records = now, self.written_bytes, self.written_records
        if self.heartbeat_at is not None:
            metrics["event_loop_gap_ms"] = round((now - self.heartbeat_at) * 1000, 2)
        try:
            import psutil
            process = psutil.Process()
            io = process.io_counters()
            cpu = process.cpu_times()
            metrics.update(rss_bytes=process.memory_info().rss, read_bytes=io.read_bytes,
                           write_bytes=io.write_bytes, cpu_user=cpu.user, cpu_system=cpu.system,
                           threads=process.num_threads())
        except Exception as error:
            metrics["metrics_error_type"] = type(error).__name__
        self.put("process.sample", **metrics)
        # Full Python stacks have no locals, argument values or source text.
        # Capture all threads on stalled work / event-loop lag, including writers.
        if any(row["age_ms"] >= 1000 for row in active) or metrics.get("event_loop_gap_ms", 0) >= 1500:
            names = {thread.ident: thread.name for thread in threading.enumerate()}
            frames = sys._current_frames()
            self.stack_fingerprints = {ident: value for ident, value in self.stack_fingerprints.items() if ident in frames}
            for ident, frame in frames.items():
                stack = []
                while frame is not None and len(stack) < 60:
                    stack.append(_location(frame))
                    frame = frame.f_back
                fingerprint = hashlib.blake2s("\n".join(stack).encode(), digest_size=8).hexdigest()
                if self.stack_fingerprints.get(ident) == fingerprint:
                    self.aggregate("thread.stack.repeat", thread_name=names.get(ident, "unknown"))
                else:
                    self.stack_fingerprints[ident] = fingerprint
                    self.put("thread.stack", inspected_thread=ident, thread_name=names.get(ident, "unknown"),
                             stack_id=fingerprint, stack=stack)

    def _watch(self):
        while not self.stop_event.wait(self.interval):
            if self.failed:
                return
            try:
                self.sample()
            except Exception as error:
                _probe_failed(self)
                self.put("diagnostic.sample_error", error_type=type(error).__name__)
            finally:
                self.watch_cpu_ms = time.thread_time() * 1000

    def close(self):
        if self.closed:
            return
        try:
            self.sample()
            self.put("diagnostic.stop", dropped_records=self.dropped)
        finally:
            self.closed = True
            self.stop_event.set()
            self.watcher.join(timeout=1)
            self.writer.join(timeout=3)


def _live(recorder):
    return recorder is not None and not (recorder.closed or recorder.failed)


def enabled():
    return _live(_recorder)


def status():
    recorder = _recorder
    return {"enabled": _live(recorder), "run_id": recorder.run_id if recorder else None,
            "build": recorder.build if recorder else None}


def _probe_failed(recorder=None):
    recorder = recorder or _recorder
    if recorder is not None:
        with recorder.lock:
            recorder.probe_errors += 1


def aggregate(event, *, duration_ms=0, count=1, **dimensions):
    try:
        if enabled():
            _recorder.aggregate(event, duration_ms=duration_ms, count=count, **dimensions)
    except Exception:
        _probe_failed()


def start(directory, *, build, version, interval=2.0, max_bytes=DEFAULT_PART_BYTES):
    global _recorder
    if enabled():
        return _recorder.file
    try:
        _recorder = Recorder(directory, build=build, version=version, interval=interval, max_bytes=max_bytes)
        return _recorder.file
    except Exception as error:
        print(f"[diagnostic] capture unavailable: {type(error).__name__}", file=sys.stderr)
        return None


def stop():
    global _recorder
    recorder, _recorder = _recorder, None
    if recorder is not None:
        try:
            recorder.close()
        except Exception:
            pass


def emit(event, **fields):
    try:
        recorder = _recorder
        if _live(recorder):
            if event in _COMPACT_EVENTS or (event == "read.completed" and fields.get("outcome") == "ok"
                                           and fields.get("total_ms", 0) < FAST_SPAN_MS):
                recorder.aggregate(event, duration_ms=fields.get("total_ms", 0),
                                   **{key: value for key, value in fields.items() if key in _DIMENSIONS})
                return
            recorder.put(event, **fields)
    except Exception:
        _probe_failed()


def ref(value):
    recorder = _recorder
    if not _live(recorder) or value is None:
        return ""
    try:
        return hashlib.blake2s(str(value).encode("utf-8"), key=recorder.salt, digest_size=8).hexdigest()
    except Exception:
        _probe_failed()
        return ""


def task_ref(value):
    """Stable across restarts for random task UUIDs, never for account/artwork IDs."""
    if not enabled() or value is None:
        return ""
    try:
        parsed = uuid.UUID(str(value))
        return hashlib.blake2s(parsed.bytes, person=b"pt-task", digest_size=16).hexdigest()
    except (ValueError, TypeError, AttributeError):
        return ref(value)


@contextmanager
def span(event, **fields):
    record = {}
    recorder = _recorder
    if not _live(recorder):
        yield record
        return
    token = None
    identity = None
    started = time.monotonic()
    compact = event.startswith("db.read.") or event in {"task.scheduler_step", "http.send"}
    try:
        identity = uuid.uuid4().hex[:12]
        trace, parent = _current.get()
        trace = trace or identity
        token = _current.set((trace, identity))
        started = time.monotonic()
        with recorder.lock:
            recorder.active[identity] = {"span": identity, "event": event, "trace": trace,
                "parent": parent, "thread": threading.get_ident(), "started": started, **_clean(fields)}
        if not compact:
            emit(event + ".begin", **{**fields, "span": identity})
    except Exception:
        # An observer failure must never prevent the business body from running.
        _probe_failed(recorder)
    try:
        yield record
    except BaseException as error:
        record.update(outcome="error", error_type=type(error).__name__)
        raise
    finally:
        try:
            with recorder.lock:
                recorder.active.pop(identity, None)
            duration_ms = round((time.monotonic() - started) * 1000, 3)
            if compact and duration_ms < FAST_SPAN_MS and "error_type" not in record:
                aggregate(event, duration_ms=duration_ms,
                          **{key: value for key, value in {**fields, **record}.items() if key in _DIMENSIONS})
            else:
                emit(event + ".end", **{**fields, **record, "span": identity,
                     "started_elapsed_ms": round((started - recorder.started) * 1000, 3),
                     "begin_recorded": not compact, "duration_ms": duration_ms})
        except Exception:
            _probe_failed(recorder)
        if token is not None:
            try:
                _current.reset(token)
            except Exception:
                _probe_failed(recorder)


def sql_start(connection, statement, executemany=False):
    recorder = _recorder
    if not _live(recorder):
        return None
    try:
        sql = str(statement)
        # Literals and values are never emitted. Cache only fingerprints/catalogue.
        signature = hashlib.blake2s(sql.encode("utf-8"), digest_size=8).hexdigest()
        connection_id = ref(id(connection))
        with recorder.lock:
            first = signature not in recorder.sql_catalogue
            if first:
                recorder.sql_catalogue.add(signature)
        if first:
            structure = re.sub(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*|/\*[\s\S]*?\*/", " ", sql)
            command = structure.lstrip().split(None, 1)[0].upper() if structure.strip() else "UNKNOWN"
            if command not in {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER",
                               "DROP", "PRAGMA", "BEGIN", "COMMIT", "ROLLBACK", "EXPLAIN", "SAVEPOINT", "RELEASE"}:
                command = "OTHER"
            tables = sorted(set(re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+([A-Za-z_][A-Za-z_0-9]*)", structure, re.I)))
            emit("sql.definition", sql_id=signature, command=command, tables=tables, caller=_caller())
        identity = uuid.uuid4().hex[:12]
        trace, parent = _current.get()
        started = time.monotonic()
        with recorder.lock:
            recorder.active[identity] = {"span": identity, "event": "sql", "sql_id": signature,
                "connection": connection_id, "thread": threading.get_ident(), "trace": trace,
                "parent": parent, "started": started}
        return recorder, identity, signature, connection_id, started, bool(executemany)
    except Exception:
        _probe_failed(recorder)
        return None


def sql_finish(token, error=None, rowcount=None):
    if token is None:
        return
    try:
        recorder, identity, signature, connection, started, many = token
        elapsed = (time.monotonic() - started) * 1000
        with recorder.lock:
            recorder.active.pop(identity, None)
            if not _live(recorder):
                return
            row = recorder.sql_counts.setdefault(signature, {"count": 0, "duration_ms": 0.0,
                "max_ms": 0.0, "errors": 0, "rows": 0})
            row["count"] += 1
            row["duration_ms"] = round(row["duration_ms"] + elapsed, 3)
            row["max_ms"] = round(max(row["max_ms"], elapsed), 3)
            row["errors"] += int(error is not None)
            row["rows"] += max(0, rowcount or 0)
        if elapsed >= 25 or error is not None:
            emit("sql.slow" if error is None else "sql.error", sql_id=signature, connection=connection,
                 duration_ms=round(elapsed, 3), executemany=many, rowcount=rowcount,
                 error_type=type(error).__name__ if error is not None else None)
    except Exception:
        _probe_failed()


async def loop_heartbeat():
    import asyncio
    while True:
        recorder = _recorder
        if not _live(recorder):
            return
        recorder.heartbeat_at = time.monotonic()
        await asyncio.sleep(.25)


class DiagnosticHTTPMiddleware:
    """Pure ASGI observer: preserves streaming, cancellation and request bodies."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not enabled() or scope.get("path", "").startswith("/api/diagnostics"):
            return await self.app(scope, receive, send)
        method = scope.get("method", "UNKNOWN")
        category = "api" if scope.get("path", "").startswith("/api/") else "page_or_media"
        headers = dict(scope.get("headers", ()))
        correlation = {}
        for name, key in ((b"x-pixiv-client-request", "client_request"), (b"x-pixiv-client-session", "client_session")):
            raw = headers.get(name, b"")
            if re.fullmatch(rb"[a-f0-9]{32}", raw):
                correlation[key] = raw.decode("ascii")
        started = time.monotonic()
        with span("http.request", method=method, category=category, **correlation) as observation:
            state = scope.setdefault("state", {})
            trace = state["diagnostic_request_id"] = _current.get()[0]
            run = status()["run_id"]
            observation.update(bytes_sent=0, disconnected=False, send_wait_ms=0.0, send_calls=0)
            async def observed_receive():
                message = await receive()
                if message["type"] == "http.disconnect":
                    observation["disconnected"] = True
                    emit("http.disconnect")
                return message
            async def observed_send(message):
                if message["type"] == "http.response.start":
                    observation["status"] = message["status"]
                    route = scope.get("route")
                    observation["route"] = getattr(route, "path", category)
                    observation["headers_ms"] = round((time.monotonic() - started) * 1000, 3)
                    ready = state.get("diagnostic_business_ready_at")
                    if ready is not None:
                        observation["after_business_ms"] = round((time.monotonic() - ready) * 1000, 3)
                    response_headers = list(message.get("headers", ()))
                    response_headers.extend(((b"x-pixiv-request-id", trace.encode("ascii")),
                                             (b"x-pixiv-diagnostic-run", run.encode("ascii")),
                                             (b"server-timing", f'pixiv;desc="{trace}"'.encode("ascii"))))
                    message = {**message, "headers": response_headers}
                    emit("http.response_started", status=message["status"], route=observation["route"],
                         headers_ms=observation["headers_ms"])
                elif message["type"] == "http.response.body":
                    observation["bytes_sent"] += len(message.get("body", b""))
                    observation.setdefault("first_body_ms", round((time.monotonic() - started) * 1000, 3))
                    if not message.get("more_body", False):
                        observation["last_body_ms"] = round((time.monotonic() - started) * 1000, 3)
                sent = time.monotonic()
                try:
                    with span("http.send", message_type=message["type"]):
                        await send(message)
                finally:
                    observation["send_wait_ms"] += round((time.monotonic() - sent) * 1000, 3)
                    observation["send_calls"] += 1
            await self.app(scope, observed_receive, observed_send)
