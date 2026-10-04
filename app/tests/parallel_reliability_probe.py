"""Production-path synthetic reliability acceptance; never connects to Pixiv.

Run from app/ with the project Python runtime:
  python tests/parallel_reliability_probe.py --artifacts NEW_DIRECTORY --pilot-seconds 120 --check-workers both
  python tests/parallel_reliability_probe.py --artifacts NEW_DIRECTORY --duration-hours 8 --check-workers 1

Only an explicitly new .devtools/test-runs directory is writable. A pilot is
reported as a pilot, even if its maintenance cycle has not finished. A soak
passes only after the requested elapsed duration (at least eight hours), one
complete full-library reconciliation, and every recorded constraint passes.
Synthetic API timing is an explicit probe override, never a shipped default.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid5
import zipfile


APP = Path(__file__).resolve().parents[1]
ROOT = APP.parents[1] if APP.parent.parent.name == ".worktrees" else APP.parent
if APP.parent.parent.name == ".worktrees":
    ROOT = APP.parent.parent.parent


def stable_id(value):
    return uuid5(NAMESPACE_URL, "pixiv-reliability-synthetic/" + str(value)).hex


class Telemetry:
    def __init__(self, out):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.events = (out / "timeline.jsonl").open("x", encoding="utf-8", buffering=1)
        self.samples = (out / "samples.jsonl").open("x", encoding="utf-8", buffering=1)
        self.http = (out / "http.jsonl").open("x", encoding="utf-8", buffering=1)
        self.durations = defaultdict(list)
        self.counts = Counter()
        self.active = Counter()
        self.peak = Counter()
        self.first_execution = {}
        self.first_remote_request = {}
        self.remote_slots = []
        self.intervals = defaultdict(list)
        self.tracked_work_ids = set()
        self.work_active = defaultdict(Counter)
        self.work_intervals = defaultdict(list)
        self.failures = []

    def event(self, name, **values):
        stamp = time.monotonic() - self.started
        record = {"t": stamp, "event": name, **values}
        with self.lock:
            self.events.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.counts[name] += 1
            if name == "remote_send":
                self.remote_slots.append((stamp, values.get("operation_id")))
                if values.get("operation_id"):
                    self.first_remote_request.setdefault(values["operation_id"], stamp)
            if name == "item_start":
                self.first_execution.setdefault(values["operation_id"], stamp)
        return stamp

    def duration(self, name, seconds):
        with self.lock:
            self.counts[name] += 1
            # Keep all ordinary eight-hour HTTP observations; heavily repeated
            # SQL/progress timings use a bounded rolling sample and exact max.
            values = self.durations[name]
            bound = 100000 if name in ("metadata", "media_full", "media_ttfb", "small_admission") or name.startswith("/api/") else 10000
            if len(values) < bound:
                values.append(seconds)
            else:
                slot = self.counts[name] % bound
                values[slot] = seconds
            self.peak[name + "_seconds"] = max(self.peak[name + "_seconds"], seconds)

    def fail(self, message):
        with self.lock:
            if message not in self.failures:
                self.failures.append(message)
        self.event("constraint_failure", message=message)

    @contextmanager
    def span(self, category, **values):
        start = self.event(category + "_start", **values)
        with self.lock:
            self.active[category] += 1
            self.peak[category] = max(self.peak[category], self.active[category])
        try:
            yield
        finally:
            end = self.event(category + "_end", **values)
            with self.lock:
                self.active[category] -= 1
                if len(self.intervals[category]) < 20000:
                    self.intervals[category].append((start, end))
            self.duration(category, end - start)

    def summary(self):
        with self.lock:
            durations = {}
            for name, values in self.durations.items():
                ordered = sorted(values)
                durations[name] = {"count": self.counts[name], "sample_count": len(values),
                    "p50_ms": ordered[int((len(ordered) - 1) * .50)] * 1000,
                    "p95_ms": ordered[int((len(ordered) - 1) * .95)] * 1000,
                    "p99_ms": ordered[int((len(ordered) - 1) * .99)] * 1000,
                    "max_ms": self.peak[name + "_seconds"] * 1000}
            return {"counts": dict(self.counts), "peaks": dict(self.peak),
                    "durations": durations, "failures": list(self.failures)}

    def close(self):
        for handle in (self.events, self.samples, self.http):
            handle.close()


def media_bytes():
    from PIL import Image
    def image(color, format):
        output = io.BytesIO()
        Image.new("RGB", (32, 24), color).save(output, format=format)
        return output.getvalue()
    png, blue, thumbnail = image("red", "PNG"), image("blue", "PNG"), image("red", "WEBP")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("first.png", png)
        archive.writestr("second.png", blue)
    frames = [{"file": "first.png", "delay": 100}, {"file": "second.png", "delay": 200}]
    return png, thumbnail, output.getvalue(), frames


def start_remote(args, telemetry, png, archive, frames):
    """HTTP transport replacement; production request/parsing logic remains."""
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *values):
            pass
        def do_POST(self):
            telemetry.fail("remote POST attempted; this probe permits observations only")
            self.send_error(405)
        def do_GET(self):
            parsed = urlsplit(self.path)
            parameters = {key: value[0] for key, value in parse_qs(parsed.query).items()}
            illust_id = parameters.get("illust_id")
            category = "download" if parsed.path.startswith("/media/") else "remote"
            with telemetry.span(category, endpoint=parsed.path, illust_id=illust_id):
                delay = args.remote_delay if category == "remote" else args.download_delay
                # A reproducible slow artist occupies a checker while another
                # checker, ready download, and local preview can overlap.
                if parsed.path == "/v1/user/illusts" and parameters.get("user_id") == "2":
                    delay = max(delay, .7)
                time.sleep(delay)
                origin = f"http://127.0.0.1:{self.server.server_port}"
                if category == "download":
                    body = archive if parsed.path.endswith(".zip") else png
                    content_type = "application/zip" if parsed.path.endswith(".zip") else "image/png"
                else:
                    value = remote_value(parsed.path, parameters, origin, args.works, args.json_width, frames)
                    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def raw_work(ident, origin, total, width):
    animation = ident <= 24 and ident != 1
    artist = (ident - 1) % 12 + 1 if ident <= total else ident - total
    url = origin + f"/media/{ident}.png"
    return {"id": 100000 + ident, "user": {"id": artist, "name": f"synthetic-{artist}"},
        "title": f"synthetic-work-{ident}", "type": "ugoira" if animation else "illust",
        "page_count": 1, "caption": "synthetic " + "x" * width,
        "tags": [{"name": "synthetic", "translated_name": "合成"}],
        "total_bookmarks": ident % 7, "total_view": ident * 3, "x_restrict": 0,
        "create_date": "2026-01-01T00:00:00+00:00", "is_bookmarked": ident % 2 == 0,
        "image_urls": {"square_medium": url, "medium": url},
        "meta_single_page": {"original_image_url": url}}


def remote_value(endpoint, parameters, origin, total, width, frames):
    if endpoint == "/v1/illust/detail":
        return {"illust": raw_work(int(parameters["illust_id"]) - 100000, origin, total, width)}
    if endpoint == "/v1/ugoira/metadata":
        return {"ugoira_metadata": {"zip_urls": {"medium": origin + f"/media/{parameters['illust_id']}.zip"}, "frames": frames}}
    if endpoint == "/v1/user/detail":
        ident = int(parameters["user_id"])
        return {"user": {"id": ident, "name": f"synthetic-{ident}", "account": f"synthetic-{ident}",
            "comment": "synthetic profile saved through the production handler", "profile_image_urls": {}}}
    if endpoint == "/v1/user/illusts":
        ident = int(parameters["user_id"])
        values = [raw_work(total + ident, origin, total, width)] if parameters.get("type") == "illust" and int(parameters.get("offset", 0)) == 0 else []
        return {"illusts": values, "next_url": None}
    raise ValueError(f"unimplemented synthetic endpoint {endpoint}")


def seed(database, images, args, origin, png, thumbnail, archive, frames):
    from sqlalchemy import insert
    from src.core.models import ArchiveIntent, Artist, Asset, Derivative, Operation, OperationItem, RemoteBookmark, TrackingPolicy, Work, utcnow
    images.mkdir(parents=True, exist_ok=True)
    (images / "seed").mkdir()
    now = utcnow()
    with database.write() as session:
        session.execute(insert(Artist), [{"id": ident, "pixiv_user_id": str(ident), "name": f"synthetic-{ident}"} for ident in range(1, 13)])
        session.execute(insert(TrackingPolicy), [{"artist_id": ident, "mode": "active"} for ident in range(1, 13)])
    for start in range(1, args.works + 1, 500):
        works, intents, assets, derivatives, bookmarks = [], [], [], [], []
        for ident in range(start, min(start + 500, args.works + 1)):
            raw = raw_work(ident, origin, args.works, args.json_width)
            animation = raw["type"] == "ugoira"
            works.append({"id": ident, "pixiv_illust_id": str(raw["id"]), "artist_id": raw["user"]["id"],
                "title": raw["title"], "type": raw["type"], "page_count": 1, "caption": raw["caption"],
                "caption_state": "known", "posted_at": now, "source_metadata": {"urls": [{"original": origin + f"/media/{ident}.png"}], "synthetic_wide": "x" * args.json_width}})
            intents.append({"pixiv_illust_id": str(raw["id"]), "mode": "keep"})
            bookmarks.append({"work_id": ident, "value": ident % 2 == 0, "account_id": "999", "source": "confirmed", "confirmed_at": now})
            if animation:
                for role, suffix, content in (("ugoira_zip", "zip", archive), ("ugoira_timing", "json", json.dumps(frames).encode())):
                    relative = f"seed/{ident}.{suffix}"
                    (images / relative).write_bytes(content)
                    assets.append({"id": stable_id(f"asset-{ident}-{role}"), "work_id": ident,
                        "role": role, "state": "unknown", "relative_path": relative,
                        "source_url": origin + f"/media/{ident}.zip" if suffix == "zip" else None})
            else:
                relative = f"seed/{ident}.png"
                missing = ident % 1000 == 0
                if not missing:
                    (images / relative).write_bytes(png)
                assets.append({"id": stable_id(f"asset-{ident}"), "work_id": ident, "role": "image", "page_index": 0,
                    "relative_path": relative, "source_url": origin + f"/media/{ident}.png", "state": "missing" if missing else "unknown"})
                if ident % 250:
                    relative = f"seed/{ident}.webp"
                    (images / relative).write_bytes(thumbnail)
                    derivatives.append({"id": stable_id(f"thumbnail-{ident}"), "work_id": ident, "kind": "thumbnail",
                        "relative_path": relative, "state": "unknown"})
        with database.write() as session:
            for model, rows in ((Work, works), (ArchiveIntent, intents), (Asset, assets), (Derivative, derivatives), (RemoteBookmark, bookmarks)):
                if rows:
                    # Group heterogeneous keys so optional URL fields do not
                    # silently disappear under SQLAlchemy's executemany shape.
                    groups = defaultdict(list)
                    for row in rows:
                        groups[tuple(sorted(row))].append(row)
                    for group in groups.values():
                        session.execute(insert(model), group)
    history = [stable_id(f"history-{ident}") for ident in range(12)]
    with database.write() as session:
        session.execute(insert(Operation), [{"id": ident, "kind": "reconcile", "parameters": {},
            "source": "maintenance", "dedupe_key": ident, "label": "synthetic past maintenance",
            "state": "finished", "phase": "verifying", "phase_unit": "assets", "phase_total": args.history_steps // 12,
            "created_at": now - timedelta(days=12-index), "finished_at": now - timedelta(days=11-index),
            "result": {"outcome": "partial_failure"}, "needs_attention": True} for index, ident in enumerate(history)])
    with database.write() as session:
        # Seeding runs before workers and HTTP admission. One atomic manifest
        # avoids repeated checkpointing of the same synthetic history indexes.
        for start in range(0, args.history_steps, 1000):
            rows = []
            for index in range(start, min(start + 1000, args.history_steps)):
                state = ("succeeded", "failed", "skipped", "not_executed")[index % 4]
                width = args.json_width if index % 16 == 0 else 256
                rows.append({"id": stable_id(f"history-item-{index}"), "operation_id": history[index % 12],
                    "item_key": f"historical-{index}", "position": index // 12, "kind": "reconcile_asset",
                    "target": {"asset_id": f"historical-{index}", "synthetic_wide": "x" * width},
                    "phase": "verifying", "unit": "assets", "lane": "local", "state": state,
                    "result": {"failure_code": "api_network", "message": "此项未完成，请重试", "synthetic_wide": "x" * width}, "depends_on": []})
            session.execute(insert(OperationItem), rows)
    return {"works": args.works, "historical_steps": args.history_steps, "historical_operations": 12,
        "historical_state_pattern": ["succeeded", "failed", "skipped", "not_executed"],
        "wide_json_bytes": args.json_width, "wide_history_fraction": "1/16",
        "missing_originals": args.works // 1000, "missing_static_previews": args.works // 250,
        "animation_zip_timing_pairs": 23, "deterministic_fixture_key": stable_id(f"{args.works}-{args.history_steps}-{args.json_width}")}


def instrument(runtime, telemetry):
    from sqlalchemy import event
    from src.core.tasks import TaskContext
    for engine, label in ((runtime.database.engine, "read"), (runtime.database.writer_engine, "write")):
        def before(connection, cursor, statement, parameters, context, many, label=label):
            context._probe_started = time.perf_counter()
        def after(connection, cursor, statement, parameters, context, many, label=label):
            telemetry.duration(label + "_sql", time.perf_counter() - context._probe_started)
            if "operation_items" in statement and "GROUP BY" in statement:
                telemetry.duration("progress_group_sql", time.perf_counter() - context._probe_started)
        event.listen(engine, "before_cursor_execute", before)
        event.listen(engine, "after_cursor_execute", after)
    def before_commit(session):
        session.info["probe_commit"] = time.perf_counter()
    def after_commit(session):
        telemetry.duration("flush_commit", time.perf_counter() - session.info.pop("probe_commit"))
    event.listen(runtime.database.writer_session, "before_commit", before_commit)
    event.listen(runtime.database.writer_session, "after_commit", after_commit)
    original_report = TaskContext.report
    original_step_report = TaskContext.report_step
    def report(context, *args, **kwargs):
        began = time.perf_counter()
        try:
            return original_report(context, *args, **kwargs)
        finally:
            telemetry.duration("progress_report", time.perf_counter() - began)
    TaskContext.report = report
    def step_report(context, *args, **kwargs):
        began = time.perf_counter()
        try:
            return original_step_report(context, *args, **kwargs)
        finally:
            telemetry.duration("progress_step_report", time.perf_counter() - began)
    TaskContext.report_step = step_report
    for handler in runtime.queue.handlers.values():
        original = handler.execute
        def execute(item, context, original=original):
            work_id = item.target.get("work_id")
            began = telemetry.event("item_start", operation_id=context.operation_id, kind=item.kind, phase=item.phase, work_id=work_id)
            tracked = work_id in telemetry.tracked_work_ids
            if tracked:
                with telemetry.lock:
                    telemetry.work_active[work_id][context.operation_id] += 1
            try:
                with telemetry.span("phase_" + item.phase, operation_id=context.operation_id, kind=item.kind):
                    return original(item, context)
            finally:
                if tracked:
                    with telemetry.lock:
                        telemetry.work_active[work_id][context.operation_id] -= 1
                        telemetry.work_intervals[work_id].append((began, time.monotonic()-telemetry.started, context.operation_id))
        handler.execute = execute
    def restore():
        TaskContext.report = original_report
        TaskContext.report_step = original_step_report
    return restore


def source_identity():
    sources = sorted((APP / "src").rglob("*.py"))
    digest = hashlib.sha256()
    for path in sources:
        digest.update(str(path.relative_to(APP)).encode())
        digest.update(path.read_bytes())
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=APP, capture_output=True, text=True, check=True).stdout.strip()
    return {"source_commit": commit, "source_tree_sha256": digest.hexdigest(), "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def http_burst(origin, args, telemetry, png, burst):
    import requests
    paths = ["/api/library?limit=100&compact=true", "/api/works/1", "/images/seed/1.png", "/api/settings", "/api/progress", "/api/logs"]
    gate = threading.Barrier(40)
    def request_one(index):
        path = paths[index % len(paths)]
        gate.wait(timeout=10)
        started = time.perf_counter()
        record = {"burst": burst, "index": index, "path": path}
        try:
            with requests.get(origin + path, stream=True, timeout=(5, 8)) as response:
                record["status"] = response.status_code
                chunks = response.iter_content(256 * 1024)
                first = next(chunks, b"")
                record["ttfb_ms"] = (time.perf_counter() - started) * 1000
                body = first + b"".join(chunks)
                elapsed = time.perf_counter() - started
                record.update(elapsed_ms=elapsed * 1000, bytes=len(body))
                if response.status_code != 200:
                    raise AssertionError(f"HTTP {response.status_code}")
                if path.startswith("/images"):
                    assert body == png, "media bytes differ from the known synthetic original"
                    telemetry.duration("media_full", elapsed)
                    telemetry.duration("media_ttfb", record["ttfb_ms"] / 1000)
                else:
                    value = json.loads(body)
                    assert isinstance(value, dict)
                    if path.startswith("/api/library"):
                        assert len(value["items"]) == 100 and value["total"] >= args.works
                    elif path.startswith("/api/works"):
                        assert value["id"] == 1 and value["title"] == "synthetic-work-1"
                    elif path == "/api/settings":
                        assert value["stats"]["works"] >= args.works
                    elif path == "/api/progress":
                        assert value["worker_error"] is None
                    else:
                        assert isinstance(value["lines"], list) and "details_complete" in value
                    telemetry.duration("metadata", elapsed)
                    telemetry.duration(path.split("?")[0], elapsed)
                    if elapsed > 5:
                        raise AssertionError(f"complete metadata took {elapsed:.3f}s > 5s")
                record["ok"] = True
        except Exception as exc:
            record.update(ok=False, error_type=type(exc).__name__, error=str(exc)[:160])
            telemetry.fail(f"burst {burst} {path}: {record['error_type']} {record['error']}")
        with telemetry.lock:
            telemetry.http.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record
    with ThreadPoolExecutor(max_workers=40) as executor:
        return list(executor.map(request_one, range(40)))


def submit_small(origin, runtime, telemetry, serial, args):
    import requests
    candidates = [value for value in range(args.works, args.works-100, -1) if (value-1) % 12 + 1 not in (1, 2)]
    with runtime.queue._scheduler_lock:
        occupied = set(runtime.queue._resources)
    with telemetry.lock:
        free = [value for value in candidates if not any(telemetry.work_active[value].values()) and not occupied.intersection({
            ("work", value), ("asset", stable_id(f"asset-{value}")), ("derivative", stable_id(f"thumbnail-{value}"))})]
    if not free:
        raise AssertionError("no unconflicted small-operation fixture is available")
    work_id = free[serial % len(free)]
    started = time.perf_counter()
    response = requests.post(origin + "/api/tasks", json={"kind": "metadata", "work_ids": [work_id]}, headers={"Origin": origin}, timeout=5)
    elapsed = time.perf_counter() - started
    telemetry.duration("small_admission", elapsed)
    response.raise_for_status()
    value = response.json()
    if elapsed > 2:
        telemetry.fail(f"small operation admission {elapsed:.3f}s > 2s")
    if not value.get("created"):
        telemetry.fail("small operation was deduplicated instead of a distinct accepted task")
    submitted = telemetry.event("small_submitted", operation_id=value["operation_id"], work_id=work_id)
    response = requests.post(origin + f"/api/works/{work_id}/bookmark/refresh", headers={"Origin": origin}, timeout=5)
    response.raise_for_status()
    return {"operation_id": value["operation_id"], "work_id": work_id, "submitted": submitted}


def intervals_overlap(first, second):
    return any(max(a, c) < min(b, d) for a, b in first for c, d in second)


def run_case(out, args, check_workers):
    import psutil
    import requests
    from sqlalchemy import func, select
    import uvicorn
    from src import auth, client as client_module
    from src.core.database import ArchiveDatabase
    from src.core.download import AssetDownloader
    from src.core.models import Asset, Derivative, Operation, OperationItem, RemoteBookmark, PublicationJournal, utcnow
    from src.core.runtime import Runtime
    from src.web import create_app

    telemetry = Telemetry(out)
    telemetry.tracked_work_ids = set(range(args.works-99, args.works+1))
    result = {"mode": "pilot" if args.pilot_seconds is not None else "eight_hour_soak", "check_workers": check_workers,
        "real_account_used": False, "remote_writes_permitted": False, "production_handlers_replaced": False,
        "synthetic_api_interval_seconds": args.api_interval, "synthetic_remote_delay_seconds": args.remote_delay,
        "synthetic_download_delay_seconds": args.download_delay, "burst_width": 40,
        "burst_interval_seconds": args.burst_seconds, **source_identity()}
    png, thumbnail, archive, frames = media_bytes()
    remote_server, remote_thread = start_remote(args, telemetry, png, archive, frames)
    remote_origin = f"http://127.0.0.1:{remote_server.server_port}"
    token = {"user_id": "999", "access_token": "synthetic-probe-value"}
    originals = (auth._load_token, auth.credential_snapshot, client_module.API_HOST, client_module._MIN_INTERVAL)
    token_paths = (auth._token_file, client_module._token_file)
    auth._token_file = client_module._token_file = lambda: out / "synthetic-token.json"
    auth._load_token = lambda: dict(token)
    auth.credential_snapshot = lambda: (dict(token), 0, 0)
    client_module.API_HOST = remote_origin
    client_module._MIN_INTERVAL = args.api_interval
    original_request = requests.sessions.Session.request
    def local_request(session, method, url, *values, **kwargs):
        address = urlsplit(url)
        if address.scheme != "http" or address.hostname != "127.0.0.1":
            telemetry.fail("transport attempted a non-local host")
            raise AssertionError("probe transport permits numeric localhost HTTP only")
        if address.port == remote_server.server_port and method.upper() != "GET":
            telemetry.fail("remote write transport attempted")
            raise AssertionError("remote writes are prohibited in this probe")
        if address.port == remote_server.server_port and not address.path.startswith("/media/"):
            context = getattr(getattr(client, "_task_context", None), "context", None)
            telemetry.event("remote_send", endpoint=address.path,
                operation_id=getattr(context, "operation_id", None))
        return original_request(session, method, url, *values, **kwargs)
    requests.sessions.Session.request = local_request
    runtime = None
    server = None
    serving = None
    restore_instrument = lambda: None
    seed_start = time.monotonic()
    try:
        db = ArchiveDatabase.create(out / "archive.db")
        try:
            result["fixture"] = seed(db, out / "images", args, remote_origin, png, thumbnail, archive, frames)
        finally:
            db.close()
        result["seed_seconds"] = time.monotonic() - seed_start
        client = client_module.PixivClient()
        # The transport host is exclusively localhost. Media downloads preserve
        # the real bounded downloader, file checks, fsync and publication path.
        downloader = AssetDownloader(allowed_hosts=("127.0.0.1",), allow_http=True)
        runtime = Runtime(out / "archive.db", out / "images", client=client, downloader=downloader, check_workers=check_workers)
        runtime.remote.get_account_id()
        restore_instrument = instrument(runtime, telemetry)
        application = create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(application, log_level="error", loop="src.networking:http_loop_factory"))
        serving = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        serving.start()
        until = time.monotonic() + 30
        while not server.started and time.monotonic() < until:
            time.sleep(.05)
        assert server.started, "production HTTP server did not start"
        began = time.monotonic()
        started_wall = utcnow()
        deadline = began + (args.pilot_seconds if args.pilot_seconds is not None else args.duration_hours * 3600)
        # Independent discovery tasks create real new-work downloads and previews
        # while maintenance inspects existing missing originals.
        for artist_id in (1, 2):
            response = requests.post(origin + "/api/tasks", json={"kind": "check", "artist_ids": [artist_id]}, headers={"Origin": origin}, timeout=5)
            response.raise_for_status()
        for kind, work_id in (("repair", 1000), ("thumbnails", 250)):
            response = requests.post(origin + "/api/tasks", json={"kind": kind, "work_ids": [work_id]}, headers={"Origin": origin}, timeout=5)
            response.raise_for_status()
        response = requests.post(origin + "/api/tasks", json={"kind": "reconcile"}, headers={"Origin": origin}, timeout=5)
        response.raise_for_status()
        maintenance_id = response.json()["operation_id"]
        result["maintenance_operation_id"] = maintenance_id
        next_burst = next_small = began
        next_sample = began
        burst = serial = 0
        small = []
        completed = False
        last_settled = None
        progress_samples = []
        process = psutil.Process()
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_small and deadline - now >= 10:
                try:
                    small.append(submit_small(origin, runtime, telemetry, serial, args))
                except Exception as exc:
                    telemetry.fail(f"small submission: {type(exc).__name__}")
                serial += 1
                next_small = now + args.small_seconds
            if now >= next_burst:
                http_burst(origin, args, telemetry, png, burst)
                burst += 1
                next_burst = now + args.burst_seconds
            if now >= next_sample:
                try:
                    with runtime.database.read() as session:
                        operation = session.get(Operation, maintenance_id)
                        settled = session.scalar(select(func.count()).select_from(OperationItem).where(
                            OperationItem.operation_id == maintenance_id, OperationItem.state.in_(("succeeded", "failed", "skipped", "not_executed")))) or 0
                        state, outcome = operation.state, operation.result.get("outcome")
                    completed = completed or (state == "finished" and outcome == "succeeded")
                    if state == "finished" and outcome != "succeeded":
                        telemetry.fail(f"full maintenance finished with {outcome}")
                    wal = Path(str(runtime.database.path) + "-wal")
                    sample = {"elapsed_seconds": now - began, "rss_bytes": process.memory_info().rss,
                        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
                        "read_active": dict(runtime.database.reads.active), "read_pending": dict(runtime.database.reads.pending),
                        "read_connections": runtime.database.engine.pool.checkedout(),
                        "write_connections": runtime.database.writer_engine.pool.checkedout(),
                        "worker_error": runtime.queue.worker_error, "maintenance_state": state,
                        "maintenance_settled": settled, "maintenance_outcome": outcome}
                    telemetry.samples.write(json.dumps(sample) + "\n")
                    for key in ("rss_bytes", "wal_bytes", "read_connections", "write_connections"):
                        telemetry.peak[key] = max(telemetry.peak[key], sample[key])
                    for lane, value in sample["read_active"].items():
                        telemetry.peak["read_active_" + lane] = max(telemetry.peak["read_active_" + lane], value)
                    for lane, value in sample["read_pending"].items():
                        telemetry.peak["read_pending_" + lane] = max(telemetry.peak["read_pending_" + lane], value)
                    if runtime.queue.worker_error:
                        telemetry.fail("production worker error: " + runtime.queue.worker_error)
                    if settled != last_settled:
                        progress_samples.append({"elapsed_seconds": now - began, "settled": settled, "state": state})
                        last_settled = settled
                except Exception as exc:
                    telemetry.fail(f"progress/resource sample: {type(exc).__name__}")
                next_sample = now + 5
            time.sleep(.05)
        result.update(elapsed_seconds=time.monotonic() - began, bursts=burst, maintenance_completed=completed,
                      maintenance_progress=progress_samples)
        for operation in small:
            first = telemetry.first_remote_request.get(operation["operation_id"])
            until = first if first is not None else time.monotonic()-telemetry.started
            conflicts = [(start, end) for start, end, owner in telemetry.work_intervals[operation["work_id"]]
                         if owner != operation["operation_id"] and max(start, operation["submitted"]) < min(end, until)]
            slots = [stamp for stamp, ident in telemetry.remote_slots if operation["submitted"] <= stamp < until
                     and not any(start <= stamp <= end for start, end in conflicts)]
            operation.update(first_item_started_seconds=telemetry.first_execution.get(operation["operation_id"]),
                             first_remote_request_seconds=first, allowed_remote_slots_before_execution=len(slots),
                             same_work_conflict_intervals=conflicts,
                             execution_wait_seconds=None if first is None else first-operation["submitted"])
            if first is None or len(slots) > 2:
                telemetry.fail("small operation did not execute within two available remote slots")
        result["small_operations"] = small
        with runtime.database.read() as session:
            result["production_persistence"] = {
                "verified_assets": session.scalar(select(func.count()).select_from(Asset).where(Asset.state == "verified")),
                "verified_previews": session.scalar(select(func.count()).select_from(Derivative).where(Derivative.state == "verified")),
                "bookmark_observations": session.scalar(select(func.count()).select_from(RemoteBookmark).where(RemoteBookmark.confirmed_at >= started_wall)),
                "publication_journals": session.scalar(select(func.count()).select_from(PublicationJournal)),
            }
        result["overlap"] = {
            "checking_download": intervals_overlap(telemetry.intervals["phase_checking"], telemetry.intervals["phase_downloading"]),
            "checking_preview": intervals_overlap(telemetry.intervals["phase_checking"], telemetry.intervals["phase_converting"] + telemetry.intervals["phase_thumbnails"]),
            "two_checks": telemetry.peak["phase_checking"] >= 2,
        }
        if args.pilot_seconds is None:
            if result["elapsed_seconds"] < max(8 * 3600, args.duration_hours * 3600):
                telemetry.fail("soak duration shorter than eight actual hours")
            if not completed:
                telemetry.fail("full 15000-work maintenance cycle did not complete successfully")
            for key in ("checking_download", "checking_preview"):
                if not result["overlap"][key]:
                    telemetry.fail("production overlap not witnessed: " + key)
    except Exception as exc:
        telemetry.fail(f"probe failed: {type(exc).__name__}: {str(exc)[:160]}")
    finally:
        if server is not None:
            server.should_exit = True
        if serving is not None:
            serving.join(30)
            result["http_server_stopped"] = not serving.is_alive()
            if serving.is_alive():
                telemetry.fail("HTTP server did not stop within 30 seconds")
        if runtime is not None:
            result["runtime_closed"] = runtime.close(30)
            if not result["runtime_closed"]:
                telemetry.fail("runtime workers did not close cleanly")
            result["cleanup"] = {"active_reads": dict(runtime.database.reads.active),
                "pending_reads": dict(runtime.database.reads.pending),
                "worker_error": runtime.queue.worker_error}
            if any(result["cleanup"]["active_reads"].values()) or any(result["cleanup"]["pending_reads"].values()):
                telemetry.fail("read resources remain active after shutdown")
        restore_instrument()
        remote_server.shutdown()
        remote_server.server_close()
        remote_thread.join(5)
        auth._load_token, auth.credential_snapshot, client_module.API_HOST, client_module._MIN_INTERVAL = originals
        auth._token_file, client_module._token_file = token_paths
        requests.sessions.Session.request = original_request
        result.update(telemetry.summary())
        result["source_tree_sha256_after"] = source_identity()["source_tree_sha256"]
        if result["source_tree_sha256_after"] != result["source_tree_sha256"]:
            telemetry.fail("source files changed while this probe was running; results do not verify one stable source tree")
            result.update(telemetry.summary())
        result["status"] = ("pilot_constraints_passed" if args.pilot_seconds is not None else "passed") if not result["failures"] else "failed"
        (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        telemetry.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--duration-hours", type=float, default=8)
    parser.add_argument("--pilot-seconds", type=float)
    parser.add_argument("--check-workers", choices=("1", "2", "both"), default="1")
    parser.add_argument("--works", type=int, default=15000)
    parser.add_argument("--history-steps", type=int, default=150512)
    parser.add_argument("--json-width", type=int, default=4096)
    parser.add_argument("--api-interval", type=float, default=.01)
    parser.add_argument("--remote-delay", type=float, default=.05)
    parser.add_argument("--download-delay", type=float, default=.15)
    parser.add_argument("--burst-seconds", type=float, default=45)
    parser.add_argument("--small-seconds", type=float, default=60)
    args = parser.parse_args()
    if args.works < 15000 or args.history_steps < 150512 or args.json_width < 4096:
        parser.error("acceptance fixtures may not be downsized below 15000 works, 150512 history steps and 4096-byte wide JSON")
    if args.pilot_seconds is None and (args.duration_hours < 8 or args.check_workers == "both"):
        parser.error("soak requires at least eight hours with one chosen checker count; compare both with a pilot first")
    if args.burst_seconds < 30 or args.small_seconds < 15 or args.api_interval < 0:
        parser.error("burst interval >=30 seconds, small interval >=15 seconds, API interval >=0 required")
    out = args.artifacts.resolve()
    allowed = (ROOT / ".devtools" / "test-runs").resolve()
    if not out.is_relative_to(allowed) or not out.name.startswith("b20261004.3-reliability-"):
        parser.error("artifacts must be a fresh main-workspace .devtools/test-runs/b20261004.3-reliability-NN directory")
    out.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(out / "configuration")
    # Prevent config import from reading the checkout's pre-existing .env.
    # This standalone probe owns a fresh isolated configuration directory.
    import dotenv
    dotenv.load_dotenv = lambda *values, **kwargs: False
    sys.path.insert(0, str(APP))
    counts = (1, 2) if args.check_workers == "both" else (int(args.check_workers),)
    results = []
    for count in counts:
        if args.check_workers == "both":
            # Each candidate gets fresh imports, client timing state and process
            # memory. Cached modules from an earlier candidate cannot masquerade
            # as a later source hash after a concurrent implementation change.
            child = out / f"b20261004.3-reliability-case-{count}"
            command = [sys.executable, str(Path(__file__).resolve()), "--artifacts", str(child),
                "--check-workers", str(count)]
            for name in ("pilot_seconds", "duration_hours", "works", "history_steps", "json_width", "api_interval",
                         "remote_delay", "download_delay", "burst_seconds", "small_seconds"):
                value = getattr(args, name)
                if value is not None:
                    command += ["--" + name.replace("_", "-"), str(value)]
            with (out / f"check-workers-{count}.stdout.log").open("x", encoding="utf-8") as log:
                completed = subprocess.run(command, cwd=APP, stdout=log, stderr=subprocess.STDOUT, text=True)
            case = child / f"check-workers-{count}"
            path = case / "result.json"
            if not path.is_file():
                print(json.dumps({"check_workers": count, "status": "failed", "child_exit_code": completed.returncode,
                                  "error": "child probe did not produce a result", "artifacts": str(child)}), flush=True)
                return 1
            results.append(json.loads(path.read_text(encoding="utf-8")))
        else:
            case = out / f"check-workers-{count}"
            case.mkdir()
            results.append(run_case(case, args, count))
        print(json.dumps({"check_workers": count, "status": results[-1]["status"],
            "failures": results[-1]["failures"], "artifacts": str(case)}, ensure_ascii=False), flush=True)
    same_source = len({value["source_tree_sha256"] for value in results}) == 1
    comparison = {"same_source_tree": same_source, "results": [{"check_workers": value["check_workers"], "status": value["status"],
        "maintenance_completed": value.get("maintenance_completed", False), "durations": value["durations"],
        "overlap": value.get("overlap"), "source_tree_sha256": value["source_tree_sha256"]} for value in results]}
    (out / "comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if same_source and all(value["status"] != "failed" for value in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
