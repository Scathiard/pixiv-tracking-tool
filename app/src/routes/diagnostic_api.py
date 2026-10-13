"""Small, payload-free browser observations; never enter the business database."""
import asyncio
import json
import math
import re
from time import monotonic

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

from ..core import diagnostics
from ..version import DIAGNOSTIC_BUILD

MAX_BYTES = 32768
EVENTS = frozenset("browser.start browser.navigation browser.visibility browser.error browser.unhandled browser.longtask browser.summary api.begin api.response api.parsed api.end api.retry api.discarded shared.join shared.leave shared.abandoned media.begin media.loaded media.decoded media.error media.cancel".split())
CHOICES = {
    "category": set("gallery detail flow artists settings progress media bookmark task logs startup other".split()),
    "method": set("GET POST PUT PATCH DELETE HEAD other".split()),
    "outcome": set("ok error cancelled timeout pending discarded unsupported".split()),
    "reason": set("caller timeout navigation shared_empty network busy http parse hidden unmounted other".split()),
    "error_type": set("Error TypeError SyntaxError RangeError AbortError TimeoutError ApiReadError Other".split()),
    "error_source": set("js vue resource".split()),
}
NUMBERS = frozenset("at_ms utc_ms duration_ms wait_ms count attempt status bytes width height consumer_count media_ref".split())
BOOLS = frozenset(("visible", "aborted", "shared"))
IDENTITIES = {"request_id": 32, "operation_id": 32, "server_trace": 12, "server_run": 12}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 10 ** 15


def _event(raw):
    if not isinstance(raw, dict) or raw.get("event") not in EVENTS:
        return None
    result = {"browser_event": raw["event"]}
    for key, value in raw.items():
        if key in NUMBERS and _number(value):
            result[key] = value
        elif key in BOOLS and type(value) is bool:
            result[key] = value
        elif key in CHOICES and isinstance(value, str) and value in CHOICES[key]:
            result[key] = value
        elif key in IDENTITIES and isinstance(value, str) and re.fullmatch(r"[a-f0-9]{%d}" % IDENTITIES[key], value):
            result[key] = value
    return result


def create_diagnostic_router():
    router = APIRouter()
    # Per-process diagnostic-only capacity. No task, query, timer or per-client map.
    rate = {"start": monotonic(), "requests": 0}

    @router.get("/api/diagnostics", include_in_schema=False)
    async def capabilities():
        if not DIAGNOSTIC_BUILD or not diagnostics.enabled():
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        return JSONResponse(diagnostics.status(), headers={"Cache-Control": "no-store"})

    @router.post("/api/diagnostics/events", include_in_schema=False)
    async def browser_events(request: Request):
        if not DIAGNOSTIC_BUILD or not diagnostics.enabled():
            return Response(status_code=404)
        now = monotonic()
        if now - rate["start"] >= 1:
            rate.update(start=now, requests=0)
        rate["requests"] += 1
        if rate["requests"] > 16:
            diagnostics.aggregate("browser.batch_rejected", reason="capacity")
            return Response(status_code=429)
        size = request.headers.get("content-length", "")
        if size.isdigit() and int(size) > MAX_BYTES:
            diagnostics.aggregate("browser.batch_rejected", reason="size")
            return Response(status_code=413)
        raw = bytearray()
        try:
            async with asyncio.timeout(1):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > MAX_BYTES:
                        diagnostics.aggregate("browser.batch_rejected", reason="size")
                        return Response(status_code=413)
                    raw.extend(chunk)
            batch = json.loads(raw)
            valid = (isinstance(batch, dict) and isinstance(batch.get("session"), str)
                     and re.fullmatch(r"[a-f0-9]{32}", batch["session"])
                     and isinstance(batch.get("events"), list) and len(batch["events"]) <= 32)
            if not valid:
                raise ValueError("invalid envelope")
            events = [_event(row) for row in batch["events"]]
            if any(row is None for row in events):
                raise ValueError("invalid events")
        except (ValueError, TypeError, OverflowError, RecursionError):
            diagnostics.aggregate("browser.batch_rejected", reason="invalid")
            return Response(status_code=400)
        except TimeoutError:
            diagnostics.aggregate("browser.batch_rejected", reason="timeout")
            return Response(status_code=408)
        except ClientDisconnect:
            diagnostics.aggregate("browser.batch_rejected", reason="cancelled")
            return Response(status_code=499)
        for row in events:
            diagnostics.emit("browser.event", session=batch["session"], **row)
        diagnostics.aggregate("browser.batch_accepted", count=len(events))
        dropped = batch.get("dropped", 0)
        errors = batch.get("observer_errors", 0)
        if (type(dropped) is int and 0 <= dropped <= 10 ** 12
                and type(errors) is int and 0 <= errors <= 10 ** 12 and (dropped or errors)):
            diagnostics.emit("browser.capture_health", session=batch["session"],
                             dropped_records=dropped, observer_errors=errors)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    return router
