"""Bounded, credential-free text projection of persisted processing results."""
from __future__ import annotations

import base64
import heapq
import json
import re

from sqlalchemy import select, text

from .models import Operation


KINDS = {"check": "检查新作品", "reconcile": "检查并补齐图库", "metadata": "刷新信息", "bookmarks": "同步收藏状态",
         "repair": "补下载", "verify": "全量核验本地文件", "backfill": "补回动图原包", "thumbnails": "生成缩略图"}
STATES = {"queued": "已排队", "planning": "正在清点", "ready": "等待执行", "running": "正在执行",
          "interrupted": "已中断，等待恢复", "finished": "已结束", "failed": "失败", "not_executed": "未执行", "skipped": "已跳过"}
OUTCOMES = {"succeeded": "处理完成", "partial_failure": "部分失败", "failed": "失败", "empty": "无需处理"}
PHASES = {"waiting": "等待", "checking": "检查", "inventory": "清点", "metadata": "画师信息",
          "captions": "作品信息", "bookmarks": "收藏状态", "downloading": "下载", "converting": "生成预览",
          "verifying": "核验", "thumbnails": "缩略图", "deleting": "删除"}
UNITS = {"artists": "位画师", "works": "件作品", "pages": "页", "assets": "项原件",
         "previews": "件预览", "files": "个文件", "items": "项"}
# User-visible logs never expose raw stdout, request parameters, arbitrary
# exception strings, paths, account identifiers or stored remote payloads.
MESSAGES = {"此项未完成，请重试", "前置步骤未成功，此项尚未执行", "原件缺失或损坏，可选择补下载",
            "无法核实当前账号，已保留最近收藏缓存", "未登录 Pixiv", "需要先登录 Pixiv"}
ERRORS = {"ConnectionError": "网络连接失败", "Timeout": "请求超时", "TimeoutError": "请求超时", "ReadTimeout": "读取主站回应超时",
          "ConnectTimeout": "连接主站超时", "HTTPError": "主站请求失败", "PermissionError": "没有文件访问权限",
          "FileNotFoundError": "本地文件不存在", "OSError": "文件读写失败", "RuntimeError": "处理未完成",
          "ValueError": "处理数据不符合要求", "OfflineError": "尚未连接 Pixiv",
          "PixivRequestError": "主站请求未完成，具体来源状态尚未确认",
          "InvalidMedia": "本地文件或作品资料不符合要求，已有存档保留待核对",
          "WorkMetadataConflict": "主站资料与本地存档不一致，已有存档保留待核对",
          "DownloadFailure": "原件下载未完成，已有本地文件保留"}

FAILURE_CODES = {
    "work_unavailable": "主站暂时无法获取此作品，未确认删除；已有本地内容保留",
    "work_private": "主站明确提示作品已设为非公开；已有本地内容保留",
    "ugoira_metadata_unavailable": "主站暂时无法提供动图原包或播放时序；已有本地内容保留",
    "work_metadata_conflict": "主站资料与本地存档不一致，已有存档保留待核对",
    "api_private": "主站明确提示作品已设为非公开",
    "api_unauthorized": "Pixiv 登录未通过（401）",
    "api_forbidden": "Pixiv 拒绝访问（403），原因待核查",
    "api_rate_limit": "Pixiv 请求受限，等待稍后重试",
    "api_unavailable": "Pixiv 服务暂时不可用，等待稍后重试",
    "api_network": "Pixiv 网络连接失败，等待稍后重试",
    "api_invalid_response": "主站资料不完整或与请求不符，已有信息保留",
    "api_response_error": "Pixiv 拒绝了此次请求，原因待核查",
    "image_unauthorized": "图片来源未授权访问（401）",
    "image_forbidden": "图片来源拒绝访问（403），原因待核查",
    "image_rate_limit": "图片来源请求受限，等待稍后重试",
    "image_network": "图片连接失败或中断，等待稍后重试",
}


def failure_reason(result: dict) -> str:
    message = result.get("message")
    failure_code = result.get("failure_code")
    if (failure_code == "work_metadata_conflict" and isinstance(message, str)
            and re.fullmatch(r"主站当前 [0-9]{1,7} 页，本地记录 [0-9]{1,7} 页；已有存档保留，需核对", message)):
        pixiv_id = result.get("pixiv_id")
        if isinstance(pixiv_id, str) and re.fullmatch(r"[0-9]{1,20}", pixiv_id):
            return f"作品 {pixiv_id}：{message}"
        return message
    if isinstance(failure_code, str) and failure_code in FAILURE_CODES:
        return FAILURE_CODES[failure_code]
    if isinstance(message, str) and message in MESSAGES and message != "此项未完成，请重试":
        return message
    code = result.get("code")
    status = result.get("status")
    if code in ("PixivRequestError", "DownloadFailure") and type(status) is int and 100 <= status <= 599:
        source = "主站" if code == "PixivRequestError" else "图片来源"
        return f"{source}请求未成功（{status}），具体原因待核对；已有本地内容保留"
    # Earlier builds only recorded these exact controlled messages, not codes.
    if code == "PixivRequestError" and isinstance(message, str):
        match = re.fullmatch(r"Pixiv 请求未成功（([1-5][0-9]{2})）", message)
        if match:
            return f"主站请求未成功（{match[1]}），具体原因待核对；已有本地内容保留"
    if code == "InvalidMedia" and message == "远端作品的归属、类型或页数发生变化，已有存档保留待核对":
        return FAILURE_CODES["work_metadata_conflict"]
    return ERRORS.get(code, "此项未完成，请重试") if isinstance(code, str) else "此项未完成，请重试"


DETAIL_WINDOW = 200
_STATES = ("failed", "not_executed", "skipped")


def _cursor(value):
    try:
        if not isinstance(value, str) or len(value) > 16000:
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(value.encode("ascii")))
        if (not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("ids"), list)
                or not 1 <= len(data["ids"]) <= 100
                or any(not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f-]{1,64}", identity)
                       for identity in data["ids"])
                or data["ids"] != sorted(set(data["ids"]))
                or type(data.get("index")) is not int or not 0 <= data["index"] < len(data["ids"])
                or type(data.get("position")) is not int or data["position"] < -1
                or type(data.get("rowid")) is not int or data["rowid"] < 0
                or type(data.get("older")) is not bool):
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError("日志读取位置无效，请重新打开日志") from exc


def _encode_cursor(data):
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


def _lanes(session, operation_id, state):
    # Seek each distinct prefix instead of DISTINCT/GROUP BY scanning a whole
    # completed operation. This also includes legacy NULL and unknown lanes.
    params = {"op": operation_id, "state": state}
    if session.execute(text("SELECT 1 FROM operation_items INDEXED BY ix_items_candidate "
            "WHERE operation_id=:op AND state=:state AND lane IS NULL LIMIT 1"), params).first():
        yield None
    previous = None
    while True:
        clause = "lane IS NOT NULL" if previous is None else "lane > :previous"
        row = session.execute(text("SELECT lane FROM operation_items INDEXED BY ix_items_candidate "
            f"WHERE operation_id=:op AND state=:state AND {clause} ORDER BY lane LIMIT 1"),
            {**params, "previous": previous}).first()
        if row is None:
            return
        previous = row[0]
        yield previous


def _detail_window(session, operation_id, position, rowid, remaining):
    streams = []
    for state in _STATES:
        for lane in _lanes(session, operation_id, state):
            lane_clause = "lane IS NULL" if lane is None else "lane=:lane"
            # The index's implicit rowid suffix provides a stable tie breaker
            # without sorting every equal-position row or reading wide target /
            # depends_on payloads. Do not filter skipped JSON before LIMIT: an
            # operation with 150k ordinary skips must return a bounded page too.
            rows = session.execute(text(
                "SELECT position, rowid, state, "
                "json_extract(result,'$.code') AS code, json_extract(result,'$.status') AS status, "
                "json_extract(result,'$.failure_code') AS failure_code, "
                "substr(json_extract(result,'$.message'),1,256) AS message, "
                "substr(json_extract(result,'$.pixiv_id'),1,32) AS pixiv_id "
                "FROM operation_items INDEXED BY ix_items_candidate "
                f"WHERE operation_id=:op AND state=:state AND {lane_clause} "
                "AND (position,rowid) > (:position,:rowid) ORDER BY position,rowid LIMIT :limit"),
                {"op": operation_id, "state": state, "lane": lane, "position": position,
                 "rowid": rowid, "limit": remaining + 1}).mappings().all()
            if rows:
                streams.append(rows)
    merged = heapq.merge(*streams, key=lambda item: (item["position"], item["rowid"]))
    rows = []
    for item in merged:
        rows.append(item)
        if len(rows) > remaining:
            break
    return rows[:remaining], len(rows) > remaining


def processing_log(database, limit: int = 100, cursor: str | None = None) -> dict:
    """Recent summaries and one bounded, resumable failure-detail scan.

    Ordinary skipped items consume the same scan budget as errors. A short
    response therefore never promises completeness merely because no matching
    failures happened to appear in this window.
    """
    limit = max(1, min(limit, 100))
    continuation = _cursor(cursor) if cursor is not None else None
    columns = (Operation.id, Operation.kind, Operation.state, Operation.phase,
        Operation.phase_unit, Operation.phase_done, Operation.phase_total,
        Operation.finished_at, Operation.updated_at, Operation.needs_attention,
        Operation.result["outcome"].as_string().label("outcome"),
        Operation.result["counts"]["skipped"].as_integer().label("skipped"))
    with database.read() as session:
        if continuation is None:
            found = session.execute(select(*columns).order_by(
                Operation.updated_at.desc(), Operation.id.desc()).limit(limit + 1)).all()
            operations = found[:limit]
            if not operations:
                return {"lines": ["暂无后台处理记录。"], "truncated": False,
                        "details_complete": True, "next_cursor": None}
            continuation = {"v": 1, "ids": sorted(op.id for op in operations), "index": 0,
                            "position": -1, "rowid": 0, "older": len(found) > limit}
        else:
            operations = []
        by_operation = {}
        remaining = DETAIL_WINDOW
        while continuation["index"] < len(continuation["ids"]) and remaining:
            identity = continuation["ids"][continuation["index"]]
            rows, more = _detail_window(session, identity, continuation["position"],
                                        continuation["rowid"], remaining)
            remaining -= len(rows)
            for item in rows:
                if item["state"] != "skipped" or item["failure_code"] in ("work_unavailable", "work_private"):
                    by_operation.setdefault(identity, []).append(item)
            if more:
                continuation["position"], continuation["rowid"] = rows[-1]["position"], rows[-1]["rowid"]
                break
            continuation["index"] += 1
            continuation["position"], continuation["rowid"] = -1, 0
        lines = []
        for op in operations:
            reference = op.id[:8] if re.fullmatch(r"[0-9a-f-]+", op.id) else "任务"
            stamp = (op.finished_at or op.updated_at).isoformat(timespec="seconds") + "Z"
            status = OUTCOMES.get(op.outcome, STATES.get(op.state, "状态未知"))
            count = f"{op.phase_done} / {op.phase_total}" if op.phase_total is not None else f"已处理 {op.phase_done}"
            count += f" {UNITS.get(op.phase_unit, '项')}"
            skipped = op.skipped
            if type(skipped) is int and skipped > 0:
                count += f"，跳过 {skipped} 项"
            lines.append(f"{stamp} [{reference}] {KINDS.get(op.kind, '后台处理')} · {status} · "
                         f"{PHASES.get(op.phase, '处理')} {count}")
            if op.needs_attention:
                lines.append("  有未完成项，原因见下方；已有本地内容保留，需核对后再决定是否重试。")
            for item in by_operation.get(op.id, []):
                lines.append(f"  第 {item['position'] + 1} 项 · {STATES[item['state']]} · {failure_reason(item)}")
        if cursor is not None:
            for identity, items in by_operation.items():
                lines.append(f"[{identity[:8]}] 更多处理结果")
                lines.extend(f"  第 {item['position'] + 1} 项 · {STATES[item['state']]} · {failure_reason(item)}"
                             for item in items)
        complete = continuation["index"] >= len(continuation["ids"])
        return {"lines": lines, "truncated": continuation["older"],
                "details_complete": complete,
                "next_cursor": None if complete else _encode_cursor(continuation)}
