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
          "ValueError": "处理数据不符合要求", "OfflineError": "尚未连接 Pixiv"}


def failure_reason(result: dict) -> str:
    message = result.get("message")
    if isinstance(message, str) and message in MESSAGES and message != "此项未完成，请重试":
        return message
    code = result.get("code")
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
                lines.append("  有未完成项，请按下面原因检查；可在对应作品或设置中重新提交。")
            for item in by_operation.get(op.id, []):
                detail = item.result if isinstance(item.result, dict) else {}
                lines.append(f"  第 {item.position + 1} 项 · {STATES[item.state]} · {failure_reason(detail)}")
        return {"lines": lines, "truncated": len(found) > limit or len(failures) > 200}
