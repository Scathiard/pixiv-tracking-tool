"""复用 Illustration.caption 列保存 caption 获取状态，不再存放正文。"""
from __future__ import annotations

import json
from datetime import datetime, timezone


TERMINAL_WITHOUT_FILE = {"empty", "unavailable"}


def encode_caption_state(status: str, error: str = "") -> str:
    payload = {
        "status": status,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if error:
        payload["error"] = str(error)[:500]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def decode_caption_state(value: str | None) -> dict:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        return {}
    return payload


def should_fetch_caption(file_exists: bool, state_value: str | None) -> bool:
    if file_exists:
        return False
    status = decode_caption_state(state_value).get("status")
    return status not in TERMINAL_WITHOUT_FILE
