"""Bounded, credential-free text projection of persisted processing results."""
from __future__ import annotations

import re

from sqlalchemy import select

from .models import Operation, OperationItem


KINDS = {"check": "检查新作品", "metadata": "刷新信息", "bookmarks": "同步收藏状态",
         "repair": "补下载", "verify": "全量核验本地文件", "backfill": "补回动图原包", "thumbnails": "生成缩略图"}
STATES = {"queued": "已排队", "planning": "正在清点", "ready": "等待执行", "running": "正在执行",
          "interrupted": "已中断，等待恢复", "finished": "已结束", "failed": "失败", "not_executed": "未执行"}
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


def processing_log(database, limit: int = 100) -> dict:
    limit = max(1, min(limit, 100))
    with database.read() as session:
        found = list(session.scalars(select(Operation).order_by(
            Operation.updated_at.desc(), Operation.id.desc()).limit(limit + 1)))
        operations = found[:limit]
        if not operations:
            return {"lines": ["暂无后台处理记录。"], "truncated": False}
        failures = list(session.scalars(select(OperationItem).where(
            OperationItem.operation_id.in_([op.id for op in operations]),
            OperationItem.state.in_(("failed", "not_executed"))).order_by(
                OperationItem.operation_id, OperationItem.position).limit(201)))
        by_operation = {}
        for item in failures[:200]:
            by_operation.setdefault(item.operation_id, []).append(item)
        lines = []
        for op in operations:
            result = op.result if isinstance(op.result, dict) else {}
            reference = op.id[:8] if re.fullmatch(r"[0-9a-f-]+", op.id) else "任务"
            stamp = (op.finished_at or op.updated_at).isoformat(timespec="seconds") + "Z"
            status = OUTCOMES.get(result.get("outcome"), STATES.get(op.state, "状态未知"))
            count = f"{op.phase_done} / {op.phase_total}" if op.phase_total is not None else f"已处理 {op.phase_done}"
            lines.append(f"{stamp} [{reference}] {KINDS.get(op.kind, '后台处理')} · {status} · "
                         f"{PHASES.get(op.phase, '处理')} {count} {UNITS.get(op.phase_unit, '项')}")
            if op.needs_attention:
                lines.append("  有未完成项，原因见下方；已有本地内容保留，需核对后再决定是否重试。")
            for item in by_operation.get(op.id, []):
                detail = item.result if isinstance(item.result, dict) else {}
                lines.append(f"  第 {item.position + 1} 项 · {STATES[item.state]} · {failure_reason(detail)}")
        return {"lines": lines, "truncated": len(found) > limit or len(failures) > 200}
