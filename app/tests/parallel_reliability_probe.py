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
from datetime import datetime, timedelta, timezone
import hashlib
import heapq
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import random
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
        self.slow_sql = (out / "slow-sql.jsonl").open("x", encoding="utf-8", buffering=1)
        self.slow_sql_samples = 0
        self.slow_sql_candidates = []
        self.durations = defaultdict(list)
        self.counts = Counter()
        self.active = Counter()
        self.peak = Counter()
        self.sampled_peak = Counter()
        self.resource_peak_events = {}
        self.first_execution = {}
        self.first_remote_request = {}
        self.remote_slots = []
        self.intervals = defaultdict(list)
        self.tracked_work_ids = set()
        self.work_active = defaultdict(Counter)
        self.work_intervals = defaultdict(list)
        self.failures = []
        self.slow_segments = defaultdict(list)
        self.sample_random = {}
        self.slow_samples_saved = False

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
        ended = time.monotonic() - self.started
        with self.lock:
            self.counts[name] += 1
            # Fixed-seed reservoirs represent the entire run, including late
            # periods, while counts and maxima remain exact.
            values = self.durations[name]
            bound = 100000 if name in ("metadata", "media_full", "media_ttfb", "small_admission") or name.startswith("/api/") else 10000
            if len(values) < bound:
                values.append(seconds)
            else:
                rng = self.sample_random.get(name)
                if rng is None:
                    rng = self.sample_random[name] = random.Random(
                        int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], 'big'))
                slot = rng.randrange(self.counts[name])
                if slot < bound:
                    values[slot] = seconds
            self.peak[name + "_seconds"] = max(self.peak[name + "_seconds"], seconds)
            if seconds >= .1:
                record = {"t": ended, "started": ended-seconds, "ended": ended,
                    "event": "probe_slow_segment", "segment": name,
                    "seconds": seconds, "thread_id": threading.get_ident()}
                self._keep_largest(self.slow_segments[name], 25, seconds, self.counts[name], record)

    @staticmethod
    def _keep_largest(heap, bound, seconds, serial, record):
        entry = (seconds, serial, record)
        if len(heap) < bound:
            heapq.heappush(heap, entry)
        elif entry[:2] > heap[0][:2]:
            heapq.heapreplace(heap, entry)

    def record_slow_sql(self, label, seconds, statement, many, error):
        if seconds < .1:
            return
        ended = time.monotonic()-self.started
        with self.lock:
            self.slow_sql_samples += 1
            record = {"t": ended, "started": ended-seconds, "ended": ended,
                "engine": label, "seconds": seconds, "sql": statement[:16000],
                "executemany": many, "interrupted_or_failed": error,
                "thread_id": threading.get_ident()}
            self._keep_largest(self.slow_sql_candidates, 100, seconds, self.slow_sql_samples, record)

    def resource_peak(self, name, value):
        # Admission calls this from inside the controller's existing condition;
        # pool callbacks use the existing telemetry lock. Neither acquires an
        # extra controller/DB lease. Retain one final maximum per resource.
        if value > self.peak[name]:
            self.peak[name] = value
            self.resource_peak_events[name] = {"t": time.monotonic()-self.started,
                "event": "probe_resource_peak", "resource": name, "value": value}

    def fail(self, message):
        with self.lock:
            if message not in self.failures:
                self.failures.append(message)
        self.event("constraint_failure", message=message)

    def has_failures(self):
        with self.lock:
            return bool(self.failures)

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
                    "sampling": "all_observations" if len(values) == self.counts[name] else "fixed_seed_reservoir_entire_run",
                    "p50_ms": ordered[int((len(ordered) - 1) * .50)] * 1000,
                    "p95_ms": ordered[int((len(ordered) - 1) * .95)] * 1000,
                    "p99_ms": ordered[int((len(ordered) - 1) * .99)] * 1000,
                    "max_ms": self.peak[name + "_seconds"] * 1000}
            return {"counts": dict(self.counts), "peaks": dict(self.peak),
                    "sampled_peaks": dict(self.sampled_peak),
                    "resource_peak_measurement": "admission counter mutations and pool checkout/checkin events",
                    "durations": durations, "failures": list(self.failures),
                    "slow_sql_observations": self.slow_sql_samples,
                    "slow_sql_retained": len(self.slow_sql_candidates),
                    "slow_segment_sampling": "largest_25_per_category_with_start_end"}

    def close(self):
        if not self.slow_samples_saved:
            for record in sorted(self.resource_peak_events.values(), key=lambda row: row['t']):
                self.events.write(json.dumps(record)+"\n")
            segments = [entry[2] for heap in self.slow_segments.values() for entry in heap]
            for record in sorted(segments, key=lambda row: row['ended']):
                self.events.write(json.dumps(record)+"\n")
            for _, _, record in sorted(self.slow_sql_candidates, key=lambda entry: entry[2]['ended']):
                self.slow_sql.write(json.dumps(record)+"\n")
            self.slow_samples_saved = True
        for handle in (self.events, self.samples, self.http, self.slow_sql):
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
    from sqlalchemy import insert, select
    from sqlalchemy.orm import selectinload
    from src.client import PixivClient
    from src.core.models import ArchiveIntent, Artist, Asset, Derivative, Operation, OperationItem, RemoteBookmark, TrackingPolicy, Work, WorkTag, utcnow
    from src.core.revisions import ensure_legacy_revision
    from src.core.tag_translations import merge_tag_translations
    images.mkdir(parents=True, exist_ok=True)
    (images / "seed").mkdir()
    now = utcnow()
    with database.write() as session:
        session.execute(insert(Artist), [{"id": ident, "pixiv_user_id": str(ident), "name": f"synthetic-{ident}"} for ident in range(1, 13)])
        session.execute(insert(TrackingPolicy), [{"artist_id": ident, "mode": "active"} for ident in range(1, 13)])
    for start in range(1, args.works + 1, 500):
        works, intents, assets, derivatives, bookmarks, tags = [], [], [], [], [], []
        for ident in range(start, min(start + 500, args.works + 1)):
            raw = raw_work(ident, origin, args.works, args.json_width)
            parsed = PixivClient._parse_illust(raw)
            posted = datetime.fromisoformat(parsed["posted_at"])
            animation = raw["type"] == "ugoira"
            source = merge_tag_translations({"urls": parsed["urls"], "synthetic_wide": "x" * args.json_width},
                parsed["tag_details"], checked_at="2026-01-01T00:00:00+00:00")
            if animation:
                source["ugoira_source"] = {"zip_url": origin + f"/media/{raw['id']}.zip",
                    "timing_url": f"https://app-api.pixiv.net/v1/ugoira/metadata?illust_id={raw['id']}",
                    "frames": frames}
            works.append({"id": ident, "pixiv_illust_id": str(raw["id"]), "artist_id": raw["user"]["id"],
                "title": raw["title"], "type": raw["type"], "page_count": 1, "caption": raw["caption"],
                "caption_state": "available", "posted_at": posted.astimezone(timezone.utc).replace(tzinfo=None),
                "time_basis": "utc", "bookmark_count": parsed["bookmark_count"], "view_count": parsed["view_count"],
                "x_restrict": parsed["x_restrict"], "restriction_verified": True, "source_metadata": source})
            tags.extend({"work_id": ident, "tag": tag} for tag in parsed["tags"])
            intents.append({"pixiv_illust_id": str(raw["id"]), "mode": "keep"})
            bookmarks.append({"work_id": ident, "value": ident % 2 == 0, "account_id": "999", "source": "confirmed", "confirmed_at": now})
            if animation:
                for role, suffix, content in (("ugoira_zip", "zip", archive), ("ugoira_timing", "json", json.dumps({"frames": frames}).encode())):
                    relative = f"seed/{ident}.{suffix}"
                    (images / relative).write_bytes(content)
                    assets.append({"id": stable_id(f"asset-{ident}-{role}"), "work_id": ident,
                        "role": role, "state": "unknown", "relative_path": relative,
                        "source_url": source["ugoira_source"]["zip_url"] if suffix == "zip" else source["ugoira_source"]["timing_url"]})
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
            for model, rows in ((Work, works), (WorkTag, tags), (ArchiveIntent, intents), (Asset, assets), (Derivative, derivatives), (RemoteBookmark, bookmarks)):
                if rows:
                    # Group heterogeneous keys so optional URL fields do not
                    # silently disappear under SQLAlchemy's executemany shape.
                    groups = defaultdict(list)
                    for row in rows:
                        groups[tuple(sorted(row))].append(row)
                    for group in groups.values():
                        session.execute(insert(model), group)
    with database.write() as session:
        # Establish all 0003 revision references before timing the workload.
        # The production helper owns signature, bindings, facts and projections;
        # one transaction avoids introducing per-work commit pressure in setup.
        for start in range(1, args.works + 1, 500):
            works = list(session.scalars(select(Work).where(Work.id >= start, Work.id < start + 500)
                .options(selectinload(Work.tags), selectinload(Work.assets), selectinload(Work.derivatives))))
            for work in works:
                ensure_legacy_revision(session, work)
            session.flush()
            session.expunge_all()
    structure_validation = validate_seed(database, images, args, origin, frames)
    history = [stable_id(f"history-{ident}") for ident in range(12)]
    with database.write() as session:
        session.execute(insert(Operation), [{"id": ident, "kind": "reconcile", "parameters": {},
            "source": "maintenance", "dedupe_key": ident, "label": "synthetic past maintenance",
            "state": "finished", "phase": "verifying", "phase_unit": "assets", "phase_total": args.history_steps // 12,
            "created_at": now - timedelta(days=12-index), "finished_at": now - timedelta(days=11-index),
            "result": {"outcome": "partial_failure"}, "needs_attention": True} for index, ident in enumerate(history)])
    history_wide_counts = Counter()
    history_state_counts = Counter()
    with database.write() as session:
        # Seeding runs before workers and HTTP admission. One atomic manifest
        # avoids repeated checkpointing of the same synthetic history indexes.
        for start in range(0, args.history_steps, 1000):
            rows = []
            for index in range(start, min(start + 1000, args.history_steps)):
                state = ("succeeded", "failed", "skipped", "not_executed")[index % 4]
                wide = (index // 4) % 16 == 0
                width = args.json_width if wide else 256
                history_state_counts[state] += 1
                if wide:
                    history_wide_counts[state] += 1
                rows.append({"id": stable_id(f"history-item-{index}"), "operation_id": history[index % 12],
                    "item_key": f"historical-{index}", "position": index // 12, "kind": "reconcile_asset",
                    "target": {"asset_id": f"historical-{index}", "synthetic_wide": "x" * width},
                    "phase": "verifying", "unit": "assets", "lane": "local", "state": state,
                    "result": {"failure_code": "api_network", "message": "此项未完成，请重试", "synthetic_wide": "x" * width}, "depends_on": []})
            session.execute(insert(OperationItem), rows)
    assert set(history_wide_counts) == {"succeeded", "failed", "skipped", "not_executed"}
    assert all(history_wide_counts[state] > 0 for state in history_state_counts)
    return {"works": args.works, "historical_steps": args.history_steps, "historical_operations": 12,
        "historical_state_pattern": ["succeeded", "failed", "skipped", "not_executed"],
        "wide_json_bytes": args.json_width,
        "wide_history_pattern": "each 64-row block has one wide row in every state",
        "historical_state_counts": dict(history_state_counts), "wide_history_state_counts": dict(history_wide_counts),
        "wide_history_fraction": f"{sum(history_wide_counts.values())}/{args.history_steps}",
        "missing_originals": args.works // 1000, "missing_static_previews": args.works // 250,
        "animation_zip_timing_pairs": 23, "structure_validation": structure_validation,
        "deterministic_fixture_key": stable_id(f"{args.works}-{args.history_steps}-{args.json_width}-native-0003")}


def validate_seed(database, images, args, origin, frames):
    """Check actual 0003 references and production same-observation semantics."""
    from sqlalchemy import func, select
    from src.client import PixivClient
    from src.core.media import read_timing, verify_media, verify_ugoira_pair
    from src.core.models import Asset, Derivative, RevisionAsset, RevisionDerivative, Work, WorkRevision
    from src.core.revisions import observe_revision, selected_revision
    from src.core.tag_translations import merge_tag_translations
    with database.read() as session:
        shape = {"work_revisions": session.scalar(select(func.count()).select_from(WorkRevision)),
            "default_references": session.scalar(select(func.count()).select_from(Work).where(Work.default_revision_id.is_not(None))),
            "latest_references": session.scalar(select(func.count()).select_from(Work).where(Work.latest_revision_id.is_not(None))),
            "assets": session.scalar(select(func.count()).select_from(Asset)),
            "asset_bindings": session.scalar(select(func.count()).select_from(RevisionAsset)),
            "derivatives": session.scalar(select(func.count()).select_from(Derivative)),
            "derivative_bindings": session.scalar(select(func.count()).select_from(RevisionDerivative)),
            "missing_original_records": session.scalar(select(func.count()).select_from(Asset).where(Asset.state == "missing"))}
    assert shape["work_revisions"] == shape["default_references"] == shape["latest_references"] == args.works, shape
    assert shape["assets"] == shape["asset_bindings"] == args.works + 23, shape
    assert shape["derivatives"] == shape["derivative_bindings"] == args.works - 23 - args.works//250, shape
    assert shape["missing_original_records"] == args.works//1000, shape
    samples = []
    sample_ids = sorted({ident for ident in (1, 2, 24, 250, 1000, args.works) if ident <= args.works})
    with database.write() as session:
        for ident in sample_ids:
            work = session.get(Work, ident)
            revision = selected_revision(session, ident)
            assert revision and revision.id == work.default_revision_id == work.latest_revision_id
            parsed = PixivClient._parse_illust(raw_work(ident, origin, args.works, args.json_width))
            source = merge_tag_translations(dict(revision.source_metadata), parsed["tag_details"], checked_at="2026-10-08T00:00:00+00:00")
            source["urls"] = parsed["urls"]
            if work.type == "ugoira":
                source["ugoira_source"] = {"zip_url": origin+f"/media/{100000+ident}.zip",
                    "timing_url": f"https://app-api.pixiv.net/v1/ugoira/metadata?illust_id={100000+ident}", "frames": frames}
                assert read_timing(images/f"seed/{ident}.json") == frames
                verify_media(images/f"seed/{ident}.zip", "ugoira_zip")
                verify_media(images/f"seed/{ident}.json", "ugoira_timing")
                verify_ugoira_pair(images/f"seed/{ident}.zip", images/f"seed/{ident}.json")
            elif ident % 1000:
                verify_media(images/f"seed/{ident}.png", "image")
            else:
                assert not (images/f"seed/{ident}.png").exists()
            if work.type != "ugoira":
                if ident % 250 == 0:
                    assert not (images/f"seed/{ident}.webp").exists()
                else:
                    verify_media(images/f"seed/{ident}.webp", "image")
            snapshot = dict(title=parsed["title"], caption=parsed["caption"], caption_state="available",
                type=parsed["type"], page_count=parsed["page_count"], tags=sorted(parsed["tags"]), source_metadata=source)
            observed, changed = observe_revision(session, work, snapshot)
            assert not changed and observed.id == revision.id, "identical synthetic remote observation created a revision"
            samples.append({"work_id": ident, "type": work.type,
                "same_remote_observation_creates_revision": False,
                "original_intentionally_missing": ident%1000 == 0,
                "static_preview_intentionally_missing": work.type != "ugoira" and ident%250 == 0})
    return {"shape": shape, "sample_ids": sample_ids, "samples": samples,
        "timing_format": "production frames envelope", "revision_initialization": "production ensure_legacy_revision; one transaction"}


def instrument(runtime, telemetry):
    from sqlalchemy import event
    from src.core.tasks import TaskContext
    restored_listeners = []
    restored_methods = []
    def listen(owner, name, callback):
        event.listen(owner, name, callback)
        restored_listeners.append((owner, name, callback))
    controller = runtime.database.reads
    original_counters = {}
    class ObservedCounts(dict):
        def __init__(self, values, prefix):
            super().__init__(values)
            self.prefix = prefix
        def __setitem__(self, lane, value):
            super().__setitem__(lane, value)
            # The real mutation already holds ReadController._condition.
            # Sampling after _try_acquire returns could miss a short lease.
            telemetry.resource_peak(self.prefix+lane, value)
    for attribute in ("active", "pending"):
        original_counters[attribute] = getattr(controller, attribute)
        observed = ObservedCounts(original_counters[attribute], "read_"+attribute+"_")
        setattr(controller, attribute, observed)
        for lane, value in observed.items():
            telemetry.resource_peak(observed.prefix+lane, value)
    for engine, label in ((runtime.database.engine, "read"), (runtime.database.writer_engine, "write")):
        telemetry.active[label+"_connections"] = 0
        def checkout(connection, record, proxy, label=label):
            with telemetry.lock:
                name = label+"_connections"
                telemetry.active[name] += 1
                telemetry.resource_peak(name, telemetry.active[name])
        def checkin(connection, record, label=label):
            with telemetry.lock:
                telemetry.active[label+"_connections"] -= 1
        listen(engine, "checkout", checkout)
        listen(engine, "checkin", checkin)
        def before(connection, cursor, statement, parameters, context, many, label=label):
            context._probe_started = time.perf_counter()
        def measured_sql(statement, context, many, *, label=label, error=False):
            elapsed = time.perf_counter() - context._probe_started
            telemetry.duration(label + "_sql", elapsed)
            telemetry.record_slow_sql(label, elapsed, statement, many, error)
            if "operation_items" in statement and "GROUP BY" in statement:
                telemetry.duration("progress_group_sql", time.perf_counter() - context._probe_started)
        def after(connection, cursor, statement, parameters, context, many, measured_sql=measured_sql):
            measured_sql(statement, context, many)
        def failed(error_context, measured_sql=measured_sql):
            context = error_context.execution_context
            if context is not None and hasattr(context, "_probe_started"):
                measured_sql(error_context.statement or "", context, context.executemany, error=True)
        listen(engine, "handle_error", failed)
        listen(engine, "before_cursor_execute", before)
        listen(engine, "after_cursor_execute", after)
    def before_flush(session, context, instances):
        session.info["probe_flush"] = time.perf_counter()
    def after_flush(session, context):
        telemetry.duration("writer_flush", time.perf_counter() - session.info.pop("probe_flush"))
    def before_commit(session):
        session.info["probe_commit"] = time.perf_counter()
    def after_commit(session):
        telemetry.duration("flush_commit", time.perf_counter() - session.info.pop("probe_commit"))
    listen(runtime.database.writer_session, "before_flush", before_flush)
    listen(runtime.database.writer_session, "after_flush", after_flush)
    listen(runtime.database.writer_session, "before_commit", before_commit)
    listen(runtime.database.writer_session, "after_commit", after_commit)
    # Time the existing DBAPI commit without causing an extra checkout or
    # touching a SQLAlchemy Connection after the transaction has closed it.
    dialect = runtime.database.writer_engine.dialect
    original_commit = dialect.do_commit
    def sqlite_commit(connection):
        began = time.perf_counter()
        try:
            return original_commit(connection)
        finally:
            telemetry.duration("sqlite_commit", time.perf_counter()-began)
    restored_methods.append((dialect, "do_commit", original_commit))
    dialect.do_commit = sqlite_commit
    for engine, label in ((runtime.database.engine, "read"), (runtime.database.writer_engine, "write")):
        original_connect = engine.connect
        def connect(*values, original_connect=original_connect, label=label, **kwargs):
            began = time.perf_counter()
            try:
                return original_connect(*values, **kwargs)
            finally:
                telemetry.duration(label + "_pool_lease", time.perf_counter()-began)
        restored_methods.append((engine, "connect", original_connect))
        engine.connect = connect
    original_write = runtime.database.write
    @contextmanager
    def write():
        began = time.perf_counter()
        try:
            with original_write() as session:
                telemetry.duration("writer_lock_pool_begin", time.perf_counter()-began)
                yield session
        finally:
            telemetry.duration("writer_transaction_total", time.perf_counter()-began)
    restored_methods.append((runtime.database, "write", original_write))
    runtime.database.write = write
    original_read = runtime.database.read
    @contextmanager
    def read():
        began = time.perf_counter()
        try:
            with original_read() as session:
                telemetry.duration("read_sync_admission_pool_begin", time.perf_counter()-began)
                yield session
        finally:
            # Includes fetching rows, ORM/JSON decoding and rollback, unlike
            # cursor execution events, which only time DBAPI execute itself.
            telemetry.duration("read_transaction_total", time.perf_counter()-began)
    restored_methods.append((runtime.database, "read", original_read))
    runtime.database.read = read
    original_execute = runtime.database.reads._execute
    async def admitted_execute(budget, function):
        began = time.perf_counter()
        def admitted_work():
            telemetry.duration("read_admission_worker_dispatch", time.perf_counter()-began)
            return function()
        try:
            return await original_execute(budget, admitted_work)
        finally:
            telemetry.duration("read_admission_execution_total", time.perf_counter()-began)
    restored_methods.append((runtime.database.reads, "_execute", original_execute))
    runtime.database.reads._execute = admitted_execute
    original_fsync = os.fsync
    def fsync(descriptor):
        began = time.perf_counter()
        try:
            return original_fsync(descriptor)
        finally:
            telemetry.duration("media_python_fsync", time.perf_counter()-began)
    os.fsync = fsync
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
        restored_methods.append((handler, "execute", original))
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
        assert not any(controller.active.values()) and not any(controller.pending.values()), "restore requires drained read admissions"
        assert not telemetry.active["read_connections"] and not telemetry.active["write_connections"], "restore requires returned pool leases"
        TaskContext.report = original_report
        TaskContext.report_step = original_step_report
        os.fsync = original_fsync
        for owner, name, method in restored_methods:
            setattr(owner, name, method)
        for owner, name, callback in reversed(restored_listeners):
            event.remove(owner, name, callback)
        for attribute, original in original_counters.items():
            original.clear()
            original.update(getattr(controller, attribute))
            setattr(controller, attribute, original)
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


class ActivityCoverage:
    """Bounded measurements: elapsed time alone cannot prove continuous use."""
    def __init__(self, began, wall_started):
        self.began = began
        self.last = {name: began for name in ("heartbeat", "sample", "burst")}
        self.counts = Counter()
        self.gaps = Counter()
        self.wall_last = wall_started
        self.wall_gap = 0.0

    def observe(self, name, stamp):
        self.gaps[name] = max(self.gaps[name], stamp-self.last[name])
        self.last[name] = stamp
        self.counts[name] += 1

    def heartbeat(self, stamp, wall_stamp):
        self.observe("heartbeat", stamp)
        self.wall_gap = max(self.wall_gap, wall_stamp-self.wall_last)
        self.wall_last = wall_stamp

    def finish(self, stamp, wall_stamp, args):
        duration = args.pilot_seconds if args.pilot_seconds is not None else args.duration_hours*3600
        gaps = {name: max(self.gaps[name], stamp-self.last[name]) for name in self.last}
        wall_gap = max(self.wall_gap, wall_stamp-self.wall_last)
        # Allow one missing boundary burst; all actual responses must still
        # satisfy their existing exact content/status/deadline assertions.
        required_bursts = max(1, math.ceil(duration/args.burst_seconds)-1)
        required_samples = max(1, math.floor(duration/10))
        limits = {"heartbeat": 30.0, "sample": 30.0, "burst": args.burst_seconds+30.0}
        violations = [f"{name} gap {gaps[name]:.3f}s > {limit:.3f}s"
                      for name, limit in limits.items() if gaps[name] > limit]
        if wall_gap > 30:
            violations.append(f"wall-clock heartbeat gap {wall_gap:.3f}s > 30s")
        for name, minimum in (("burst", required_bursts), ("sample", required_samples)):
            if self.counts[name] < minimum:
                violations.append(f"{name} coverage {self.counts[name]} < {minimum}")
        return {"observations": dict(self.counts), "max_gap_seconds": gaps,
            "max_wall_heartbeat_gap_seconds": wall_gap, "gap_limits_seconds": limits,
            "minimum_bursts": required_bursts, "minimum_successful_resource_samples": required_samples,
            "minimum_mixed_http_requests": required_bursts*40, "violations": violations,
            "enforced_for_soak": args.pilot_seconds is None}


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
        "localhost_proxy_policy": "trust_env=false; session and explicit proxies empty; environment unchanged",
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
        # Confined to guarded synthetic localhost requests. Disabling trust_env
        # also prevents reading the user's existing netrc credentials.
        session.trust_env = False
        session.proxies = {}
        kwargs["proxies"] = {}
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
        coverage = ActivityCoverage(began, time.time())
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
        process.cpu_percent(interval=None)
        while time.monotonic() < deadline:
            now = time.monotonic()
            coverage.heartbeat(now, time.time())
            if now >= next_small and deadline - now >= 10:
                try:
                    small.append(submit_small(origin, runtime, telemetry, serial, args))
                except Exception as exc:
                    telemetry.fail(f"small submission: {type(exc).__name__}")
                serial += 1
                next_small = now + args.small_seconds
            if now >= next_burst:
                http_burst(origin, args, telemetry, png, burst)
                coverage.observe("burst", time.monotonic())
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
                    sampled_at = time.monotonic()
                    coverage.observe("sample", sampled_at)
                    cpu = process.cpu_times()
                    sample = {"elapsed_seconds": sampled_at - began, "rss_bytes": process.memory_info().rss,
                        "cpu_user_seconds": cpu.user, "cpu_system_seconds": cpu.system,
                        "cpu_percent_since_last_sample": process.cpu_percent(interval=None),
                        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
                        "read_active": dict(runtime.database.reads.active), "read_pending": dict(runtime.database.reads.pending),
                        "read_connections": runtime.database.engine.pool.checkedout(),
                        "write_connections": runtime.database.writer_engine.pool.checkedout(),
                        "worker_error": runtime.queue.worker_error, "maintenance_state": state,
                        "maintenance_settled": settled, "maintenance_outcome": outcome}
                    telemetry.samples.write(json.dumps(sample) + "\n")
                    for key in ("rss_bytes", "wal_bytes", "read_connections", "write_connections"):
                        telemetry.sampled_peak[key] = max(telemetry.sampled_peak[key], sample[key])
                    for lane, value in sample["read_active"].items():
                        telemetry.sampled_peak["read_active_" + lane] = max(telemetry.sampled_peak["read_active_" + lane], value)
                    for lane, value in sample["read_pending"].items():
                        telemetry.sampled_peak["read_pending_" + lane] = max(telemetry.sampled_peak["read_pending_" + lane], value)
                    if runtime.queue.worker_error:
                        telemetry.fail("production worker error: " + runtime.queue.worker_error)
                    if settled != last_settled:
                        progress_samples.append({"elapsed_seconds": now - began, "settled": settled, "state": state})
                        last_settled = settled
                except Exception as exc:
                    telemetry.fail(f"progress/resource sample: {type(exc).__name__}")
                next_sample = now + 5
            if args.pilot_seconds is None and telemetry.has_failures():
                # A failed soak cannot become a pass by waiting eight hours.
                # Preserve the evidence and use the normal finally cleanup;
                # the full duration/completion gates below remain mandatory.
                result["stopped_early_after_failure"] = True
                break
            time.sleep(.05)
        result.update(elapsed_seconds=time.monotonic() - began, bursts=burst, maintenance_completed=completed,
                      maintenance_progress=progress_samples)
        result["activity_coverage"] = coverage.finish(time.monotonic(), time.time(), args)
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
            for violation in result["activity_coverage"]["violations"]:
                telemetry.fail("continuous soak coverage: " + violation)
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
