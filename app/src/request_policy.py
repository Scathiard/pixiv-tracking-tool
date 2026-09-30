"""Small, bounded request timing helpers shared by API and image requests."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


MAX_RETRY_AFTER = 86400  # The persisted task result accepts at most one day.


class ShutdownRequested(BaseException):
    """Cooperative stop, kept out of handlers' ordinary failure classification."""


def retry_after_seconds(headers, *, now=None):
    """Read RFC Retry-After seconds or an HTTP date without retaining headers."""
    raw = headers.get("Retry-After") if headers else None
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
    if not isinstance(raw, str) or not raw:
        return None
    if raw.isascii() and raw.isdecimal():
        try:
            return min(int(raw), MAX_RETRY_AFTER)
        except ValueError:
            return MAX_RETRY_AFTER  # A very long decimal must not break error handling.
    try:
        date = parsedate_to_datetime(raw)
        if date.tzinfo is None:
            return None
        current = now or datetime.now(timezone.utc)
        seconds = math.ceil((date - current).total_seconds())
        return min(seconds, MAX_RETRY_AFTER) if seconds >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def is_rate_limit(body):
    """Only the known Pixiv error wording counts as a JSON rate limit."""
    if not isinstance(body, dict) or "error" not in body:
        return False
    error = body["error"]
    if isinstance(error, dict):
        error = " ".join(str(error.get(key, "")) for key in ("message", "user_message", "reason"))
    if not isinstance(error, str):
        return False
    normalized = error.lower().replace("_", " ").replace("-", " ")
    return "rate limit" in normalized or "too many requests" in normalized
