"""筛选参数和本地跳转 URL 的统一语义。"""
from __future__ import annotations

from urllib.parse import urlencode, urlsplit


def parse_csv_ints(value: str | None) -> set[int] | None:
    """None 表示未指定（全选），空字符串表示明确空选。"""
    if value is None:
        return None
    result: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def parse_csv_choices(value: str | None, allowed: set[str]) -> set[str] | None:
    """解析受限字符串集合，同时保留“全选”和“空选”的区别。"""
    if value is None:
        return None
    return {
        part.strip()
        for part in value.split(",")
        if part.strip() in allowed
    }


def build_local_url(path: str, params: dict[str, object | None]) -> str:
    """构建已编码的站内 URL；None 删除参数，空字符串会被保留。"""
    pairs = [(key, str(value)) for key, value in params.items() if value is not None]
    query = urlencode(pairs)
    return f"{path}?{query}" if query else path


def safe_redirect_target(value: str | None, fallback: str = "/") -> str:
    """只接受单斜杠开头的站内路径，拒绝 //host 和反斜杠变体。"""
    if not value or not value.startswith("/") or value.startswith("//"):
        return fallback
    if "\\" in value or any(ord(char) < 32 for char in value):
        return fallback
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return fallback
    return value
