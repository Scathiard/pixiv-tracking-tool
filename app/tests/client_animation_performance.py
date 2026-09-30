"""Repeatable high-resolution GIF / thumbnail browser comparison, localhost only.

Measurements are observations, not a portable FPS benchmark. Use the same media,
browser, viewport and AB/BA ordering when comparing two frontend builds.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback


def percentile(values, fraction):
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * fraction))] if values else None


def summarize_frames(values):
    return {"frames": len(values), "mean_ms": sum(values) / len(values) if values else None,
            "p50_ms": percentile(values, .50), "p95_ms": percentile(values, .95),
            "p99_ms": percentile(values, .99), "max_ms": max(values) if values else None,
            "over_33ms": sum(value > 33.4 for value in values),
            "over_50ms": sum(value > 50 for value in values)}


def make_media(folder):
    from PIL import Image, ImageDraw
    folder.mkdir(parents=True, exist_ok=False)
    frames = []
    # Full-frame changing dithered geometry prevents a tiny changed rectangle
    # from making this a two-solid-colour decoding smoke test. Bounded below
    # 80 MiB raw pixels per image, with only viewport-visible media requested.
    for frame in range(16):
        picture = Image.new("P", (1280, 960))
        picture.putpalette([channel for i in range(256) for channel in
                            ((i * 17) % 256, (i * 29) % 256, (i * 47) % 256)])
        draw = ImageDraw.Draw(picture)
        for y in range(0, 960, 16):
            for x in range(0, 1280, 16):
                draw.rectangle((x, y, x + 15, y + 15),
                               fill=(x // 16 * 11 + y // 16 * 7 + frame * 19) % 256)
        draw.ellipse((100 + frame * 35, 170, 460 + frame * 35, 650), fill=255 - frame)
        frames.append(picture)
    path = folder / "high-resolution.gif"
    frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:],
                   duration=50, loop=0, optimize=False, disposal=2)
    for frame in frames:
        frame.close()
    with Image.open(path) as media:
        manifest = {"width": media.width, "height": media.height,
                    "frames": media.n_frames, "frame_duration_ms": media.info["duration"],
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "raw_frame_pixel_bytes": media.width * media.height * 4,
                    "all_frames_pixel_bytes_per_image": media.width * media.height * 4 * media.n_frames}
    assert manifest["frames"] == 16 and manifest["bytes"] < 12 * 1024 * 1024
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path, manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--frontend", type=Path, help="Built frontend dist; defaults to this worktree")
    parser.add_argument("--media", type=Path, help="Reuse previously generated synthetic GIF")
    parser.add_argument("--preview-size", type=int, choices=(1280, 360), default=1280,
                        help="Single-variable diagnostic; preserves frame count and timing")
    parser.add_argument("--real-derivative", action="store_true",
                        help="Generate via production build_card_motion, serve unmodified compact API")
    parser.add_argument("--widths", default="1440,390,320")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--trace", action="store_true", help="Preserve CDP trace for every phase")
    parser.add_argument("--assert-animation-budget", action="store_true",
                        help="Red test: fail when animation CPU is >2x static and adds >0.5 CPU-seconds/second")
    args = parser.parse_args()
    assert 1 <= args.rounds <= 4 and 3 <= args.seconds <= 12
    widths = [int(value) for value in args.widths.split(",")]
    assert all(value in (1440, 390, 320) for value in widths)
    root = next(parent for parent in Path(__file__).resolve().parents if parent.name == "pixiv-tracking-tool")
    # Also works from the root checkout; fixtures must stay under the main
    # repository's named test-runs area, never a supplied account/gallery path.
    if root.name != "pixiv-tracking-tool":
        root = Path(__file__).resolve().parents[2]
    artifacts = args.artifacts.resolve()
    assert artifacts.is_relative_to(root / ".devtools/test-runs"), artifacts
    assert artifacts.name.startswith("b9-perf-")
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "fixture")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import uvicorn
    from fastapi.responses import FileResponse
    from PIL import Image, ImageChops, ImageStat
    from playwright.sync_api import sync_playwright
    from client_fixture import make_library
    frontend = (args.frontend or Path(__file__).resolve().parents[1] / "frontend/dist").resolve()
    assert (frontend / "index.html").is_file(), frontend
    if args.media:
        gif_path = args.media.resolve()
        assert gif_path.is_relative_to(root / ".devtools/test-runs") and gif_path.name == "high-resolution.gif"
        with Image.open(gif_path) as picture:
            media = {"width": picture.width, "height": picture.height, "frames": picture.n_frames,
                     "frame_duration_ms": picture.info["duration"], "bytes": gif_path.stat().st_size,
                     "sha256": hashlib.sha256(gif_path.read_bytes()).hexdigest()}
        assert media["frames"] >= 16 and media["width"] >= 1024 and media["bytes"] <= 12 * 1024 * 1024
    else:
        gif_path, media = make_media(artifacts / "media")
    source_media = dict(media)
    if args.preview_size == 360:
        small_frames = []
        with Image.open(gif_path) as picture:
            for index in range(picture.n_frames):
                picture.seek(index)
                small_frames.append(picture.convert("RGB").resize((360, 270), Image.Resampling.LANCZOS))
        reduced = artifacts / "reduced-360.gif"
        small_frames[0].save(reduced, format="GIF", save_all=True, append_images=small_frames[1:],
                             duration=media["frame_duration_ms"], loop=0, optimize=False, disposal=2)
        for frame in small_frames:
            frame.close()
        gif_path = reduced
        with Image.open(gif_path) as picture:
            media = {"width": picture.width, "height": picture.height, "frames": picture.n_frames,
                     "frame_duration_ms": picture.info["duration"], "bytes": gif_path.stat().st_size,
                     "sha256": hashlib.sha256(gif_path.read_bytes()).hexdigest()}
        assert media["frames"] == source_media["frames"] and media["frame_duration_ms"] == source_media["frame_duration_ms"]
    runtime, remote = make_library(artifacts / "fixture", 80)
    if args.real_derivative:
        assert args.preview_size == 1280, "real derivative mode must use unchanged original GIF"
        from sqlalchemy import update
        from src.core.media import CARD_MOTION_SPEC, build_card_motion
        from src.core.models import Derivative, Work
        motion_path = artifacts / "generated-card-motion.webp"
        verified = build_card_motion(gif_path, motion_path)
        with Image.open(motion_path) as picture:
            durations = []
            for index in range(picture.n_frames):
                picture.seek(index)
                picture.load()
                durations.append(picture.info.get("duration"))
            media = {"width": picture.width, "height": picture.height, "frames": picture.n_frames,
                     "duration_ms": sum(durations), "frame_durations_ms": durations,
                     "bytes": verified.byte_size, "sha256": verified.sha256,
                     "generator": "production build_card_motion", "spec_key": CARD_MOTION_SPEC}
        assert media["frames"] > 1 and max(media["width"], media["height"]) <= 360
        assert media["duration_ms"] == source_media["frames"] * source_media["frame_duration_ms"]
        rows = []
        for work_id in range(1, 81):
            for kind, spec, source, manifest in (("gif", "v1", gif_path, source_media),
                                                ("thumbnail", CARD_MOTION_SPEC, motion_path, media)):
                relative = f"perf-{work_id}-{'original.gif' if kind == 'gif' else 'card-motion.webp'}"
                target = runtime.images / relative
                os.link(source, target)
                stat = target.stat()
                rows.append({"work_id": work_id, "kind": kind, "spec_key": spec,
                             "relative_path": relative, "state": "verified", "width": manifest["width"],
                             "height": manifest["height"], "sha256": manifest["sha256"], "byte_size": stat.st_size,
                             "source_version": source_media["sha256"] if kind == "thumbnail" else None,
                             "file_mtime_ns": stat.st_mtime_ns, "file_identity": stat.st_ino})
        with runtime.database.write() as session:
            session.execute(update(Work).values(type="ugoira", page_count=1))
            session.execute(Derivative.__table__.insert(), rows)
        detail = runtime.library.detail(80)
        assert detail["pages"][0]["src"].endswith("original.gif")
        (artifacts / "detail-original.json").write_text(json.dumps(detail["pages"], indent=2), encoding="utf-8")
    from src.auth import _find_system_browser
    import src.web as web
    resource_path = web.resource_path
    web.resource_path = lambda name: str(frontend / name.removeprefix("frontend/dist").lstrip("/")) if name.startswith("frontend/dist") else resource_path(name)
    application = web.create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True)
    requests = Counter()

    @application.get("/performance-fixture/{work_id}.gif")
    def gif(work_id: int):
        requests[work_id] += 1
        return FileResponse(gif_path, media_type="image/gif", headers={"Cache-Control": "public, max-age=3600"})

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
        log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    result = {"media": media, "source_media": source_media, "frontend": str(frontend), "widths": widths,
              "frontend_files_sha256": {str(path.relative_to(frontend)): hashlib.sha256(path.read_bytes()).hexdigest()
                                        for path in sorted(frontend.rglob("*")) if path.is_file()},
              "real_derivative": args.real_derivative,
              "fixture_registration": "production generator once; identical synthetic output hard-linked and registered for 80 works; task queue generation covered separately" if args.real_derivative else "mock preview URLs",
              "rounds": [], "errors": [], "real_library_used": False, "real_account_used": False,
              "limits": "Headless Chromium; rAF gaps observe main-thread scheduling, CPU includes browser/renderer/GPU processes; synthetic geometry, no device/EXE claim"}
    page = None
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            browser_cdp = browser.new_browser_cdp_session()
            result["browser_version"] = browser.version

            def cpu():
                return {str(process["id"]): process for process in browser_cdp.send("SystemInfo.getProcessInfo")["processInfo"]}

            for width in widths:
                for trial in range(args.rounds):
                    # Reverse the order to expose warm-up/thermal/order effects.
                    for mode in (("animation", "static") if trial % 2 == 0 else ("static", "animation")):
                        name = f"{width}-r{trial + 1}-{mode}"
                        context = browser.new_context(viewport={"width": width, "height": 900 if width == 1440 else 844},
                            is_mobile=width < 500, has_touch=width < 500)
                        page = context.new_page()
                        requested_media = []
                        page.on("request", lambda request: requested_media.append(request.url) if "/images/" in request.url or "/performance-fixture/" in request.url else None)
                        page.on("pageerror", lambda error: result["errors"].append(str(error)))

                        def library(route):
                            if args.real_derivative and mode == "animation":
                                route.continue_()
                                return
                            response = route.fetch()
                            data = response.json()
                            for work in data["items"]:
                                if args.real_derivative:
                                    work["preview"] = None
                                    continue
                                work.update(type="ugoira", page_count=1, preview_status="ready",
                                            preview=f"/performance-fixture/{work['id']}.gif" if mode == "animation" else None)
                                work["recovery"] = {"status": "ready", "kind": "preview", "next_retry_at": None, "reason": None}
                            route.fulfill(response=response, body=json.dumps(data, ensure_ascii=False))

                        page.route("**/api/library?*", library)
                        page.goto(f"http://127.0.0.1:{port}")
                        page.wait_for_function("()=>document.querySelectorAll('.work-card img').length>0 && [...document.querySelectorAll('.work-card img')].every(e=>e.complete&&e.naturalWidth>0)")
                        page.wait_for_timeout(2000)
                        pane = page.locator(".gallery-viewport")
                        box = pane.bounding_box()
                        page.mouse.move(box["x"] + box["width"] * .55, box["y"] + box["height"] * .55)
                        observation = page.evaluate("""()=>[...document.querySelectorAll('.work-card img')].map(e=>({src:e.currentSrc,naturalWidth:e.naturalWidth,naturalHeight:e.naturalHeight,source:e.dataset.mediaSource,rect:{width:e.width,height:e.height}}))""")
                        assert any(item["naturalWidth"] == media["width"] for item in observation) if mode == "animation" else all(item["naturalWidth"] <= 640 for item in observation)
                        frame_hashes = []
                        frame_deltas, last_frame = [], None
                        if trial == 0:
                            # The screenshot action scrolls the target into view;
                            # preserve that warm-up frame, then sample after any
                            # visibility transition rather than calling it playback.
                            page.locator(".work-card img").first.screenshot(path=str(artifacts / f"{name}-warmup.png"))
                            page.wait_for_timeout(400)
                            for sample in range(3):
                                pixels = page.locator(".work-card img").first.screenshot(path=str(artifacts / f"{name}-frame-{sample}.png"))
                                with Image.open(io.BytesIO(pixels)) as rendered:
                                    center = rendered.crop((rendered.width // 4, rendered.height // 4,
                                        rendered.width * 3 // 4, rendered.height * 3 // 4)).convert("RGB")
                                    frame_hashes.append(hashlib.sha256(center.tobytes()).hexdigest())
                                    if last_frame is not None:
                                        frame_deltas.append(sum(ImageStat.Stat(ImageChops.difference(center, last_frame)).mean) / 3)
                                    last_frame = center.copy()
                                page.wait_for_timeout(130)
                            # A one-channel change in one screenshot pixel is not
                            # animation. Keep raw hashes and quantify the actual
                            # visible change instead of relying on byte identity.
                            assert max(frame_deltas) > 10 if mode == "animation" else max(frame_deltas) < 1, frame_deltas
                        record = {"name": name, "mode": mode, "width": width, "observed_media": observation,
                                  "frame_hashes": frame_hashes, "frame_mean_channel_deltas": frame_deltas,
                                  "initial_cards": page.locator(".work-card").count(),
                                  "initial_animations": page.locator('img[data-media-source="animation"]').count(), "phases": {}}
                        if args.real_derivative and mode == "animation":
                            page.evaluate("window.__initialAnimation=document.querySelector('img[data-media-source=\"animation\"]')")
                        for phase in ("idle", "scroll", "settled"):
                            before_cpu = cpu()
                            cdp = context.new_cdp_session(page)
                            if args.trace:
                                browser_cdp.send("Tracing.start", {"categories": "devtools.timeline,disabled-by-default-devtools.timeline,cc,blink,gpu", "transferMode": "ReturnAsStream"})
                            cdp.send("Performance.enable")
                            before_perf = {item["name"]: item["value"] for item in cdp.send("Performance.getMetrics")["metrics"]}
                            page.evaluate("""()=>{window.__perf={frames:[],times:[],longTasks:[],active:true,last:null};window.__perfObserver=new PerformanceObserver(list=>window.__perf.longTasks.push(...list.getEntries().map(e=>({start:e.startTime,duration:e.duration}))));window.__perfObserver.observe({type:'longtask',buffered:false});function tick(t){let p=window.__perf;if(!p.active)return;if(p.last!==null)p.frames.push(t-p.last);p.times.push(t);p.last=t;requestAnimationFrame(tick)}requestAnimationFrame(tick)}""")
                            started = time.monotonic()
                            wheel_events = 0
                            scroll_animation_samples = []
                            while (wheel_events < round(args.seconds * 10) if phase == "scroll" else time.monotonic() - started < args.seconds):
                                if phase == "scroll":
                                    # Same distance/event cadence across all modes. Reverse
                                    # each second to keep traversing the same local works.
                                    direction = 1 if (wheel_events // 10) % 2 == 0 else -1
                                    page.mouse.wheel(0, direction * 96)
                                    wheel_events += 1
                                page.wait_for_timeout(80)
                                if phase == "scroll":
                                    scroll_animation_samples.append(page.locator('img[data-media-source="animation"]').count())
                            elapsed = time.monotonic() - started
                            samples = page.evaluate("()=>{window.__perf.active=false;window.__perfObserver.disconnect();return window.__perf}")
                            after_perf = {item["name"]: item["value"] for item in cdp.send("Performance.getMetrics")["metrics"]}
                            after_cpu = cpu()
                            deltas = [{"id": key, "type": process["type"], "cpu_seconds": process["cpuTime"] - before_cpu[key]["cpuTime"]}
                                      for key, process in after_cpu.items() if key in before_cpu]
                            summary = {**summarize_frames(samples["frames"]), "elapsed_seconds": elapsed,
                                       "wheel_events": wheel_events, "cpu_seconds": sum(item["cpu_seconds"] for item in deltas),
                                       "scroll_animation_samples": scroll_animation_samples,
                                       "cpu_by_process": deltas,
                                       "main_thread_task_seconds": after_perf.get("TaskDuration", 0) - before_perf.get("TaskDuration", 0),
                                       "long_tasks": samples["longTasks"],
                                       "cards": page.locator(".work-card").count(),
                                       "animations": page.locator('img[data-media-source="animation"]').count(),
                                       "scroll_top": pane.evaluate("e=>e.scrollTop")}
                            if args.trace:
                                completed = []
                                browser_cdp.once("Tracing.tracingComplete", lambda event: completed.append(event))
                                browser_cdp.send("Tracing.end")
                                deadline = time.monotonic() + 15
                                while not completed and time.monotonic() < deadline:
                                    page.wait_for_timeout(50)
                                assert completed, "CDP trace did not finish"
                                handle = completed[0]["stream"]
                                chunks = []
                                while True:
                                    chunk = browser_cdp.send("IO.read", {"handle": handle})
                                    chunks.append(chunk["data"])
                                    if chunk.get("eof"):
                                        break
                                browser_cdp.send("IO.close", {"handle": handle})
                                trace_text = "".join(chunks)
                                (artifacts / f"{name}-{phase}-trace.json").write_text(trace_text, encoding="utf-8")
                                events = json.loads(trace_text)["traceEvents"]
                                breakdown = {}
                                for event in events:
                                    event_name = event.get("name", "")
                                    if any(word in event_name.lower() for word in ("decode", "paint", "raster", "drawframe")):
                                        bucket = breakdown.setdefault(event_name, {"events": 0, "duration_ms": 0})
                                        bucket["events"] += 1
                                        bucket["duration_ms"] += event.get("dur", 0) / 1000
                                summary["trace_render_decode"] = breakdown
                            cdp.detach()
                            (artifacts / f"{name}-{phase}-raw.json").write_text(json.dumps(samples, indent=2), encoding="utf-8")
                            record["phases"][phase] = summary
                            print(json.dumps({"name": name, "phase": phase, "metrics": summary}, ensure_ascii=False), flush=True)
                        record["requested_media"] = requested_media
                        if args.real_derivative:
                            assert not any("original.gif" in url for url in requested_media), "Homepage requested full original GIF"
                            assert max(phase["cards"] for phase in record["phases"].values()) <= 45
                            if mode == "animation":
                                assert all(count == 0 for count in record["phases"]["scroll"]["scroll_animation_samples"]), "Animations did not pause during continuous scrolling"
                                assert record["phases"]["settled"]["animations"] > 0, "Animations did not resume after scrolling"
                                assert page.evaluate("!window.__initialAnimation.isConnected || !window.__initialAnimation.hasAttribute('src')"), "Old animation element kept its source"
                                pane.evaluate("e=>e.scrollTop=0")
                                page.wait_for_timeout(700)
                                returned = []
                                for sample in range(3):
                                    pixels = page.locator('img[data-media-source="animation"]').first.screenshot(path=str(artifacts / f"{name}-returned-{sample}.png"))
                                    with Image.open(io.BytesIO(pixels)) as rendered:
                                        returned.append(rendered.convert("RGB").copy())
                                    page.wait_for_timeout(130)
                                returned_deltas = [sum(ImageStat.Stat(ImageChops.difference(returned[index], returned[index - 1])).mean) / 3 for index in range(1, len(returned))]
                                assert max(returned_deltas) > 10, "Returned viewport did not visibly resume playback"
                                record["returned_frame_mean_channel_deltas"] = returned_deltas
                                assert not any("original.gif" in url for url in requested_media)
                        result["rounds"].append(record)
                        (artifacts / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                        context.close()
            browser.close()
        assert remote.write_count == 0 and not result["errors"]
        violations = []
        for width in widths:
            for trial in range(args.rounds):
                pair = {item["mode"]: item for item in result["rounds"] if item["width"] == width and item["name"].startswith(f"{width}-r{trial + 1}-")}
                for phase in ("idle", "scroll", "settled"):
                    animation, static = pair["animation"]["phases"][phase], pair["static"]["phases"][phase]
                    a = animation["cpu_seconds"] / animation["elapsed_seconds"]
                    s = static["cpu_seconds"] / static["elapsed_seconds"]
                    if a > max(s * 2, s + .5):
                        violations.append({"width": width, "round": trial + 1, "phase": phase, "animation_cpu_cores": a, "static_cpu_cores": s})
        result["budget_violations"] = violations
        if args.assert_animation_budget:
            assert not violations, f"Animation CPU budget exceeded: {violations}"
    except Exception:
        result["errors"].append(traceback.format_exc())
        if page is not None:
            try:
                page.screenshot(path=str(artifacts / "failure.png"))
            except Exception:
                pass
        raise
    finally:
        server.should_exit = True
        thread.join(20)
        result["gif_requests"] = dict(requests)
        (artifacts / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()

