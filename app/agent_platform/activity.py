"""执行状态判定：只认事件证据，服务层与体验层共用同一套口径。

规则（页面文案与此保持一致）：

* 只有 ``ok=1`` 才算「成功」；
* 近期（默认 15 分钟）有活动但没有明确结果 → 「运行中」；
* 超过窗口仍没有明确结果 → 「未验证」；
* ``ok=0`` → 「失败」；
* 没有任何执行证据 → ``none``（页面显示「无执行记录」），绝不默认正常。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

RUNNING_MINUTES = 15
STATES = ("running", "failed", "unverified", "ok")
LABELS = {
    "running": "运行中",
    "failed": "失败",
    "unverified": "未验证",
    "ok": "成功",
    "none": "无执行记录",
}
EVENT_TYPES = ("subagent_run", "session_activity", "status_report", "tool_use", "tool_result")
#: 自报状态（ai-team/status/*.json 的 state）→ 中文标签。
REPORTED_LABELS = {
    "idle": "空闲",
    "running": "运行中",
    "working": "运行中",
    "in_progress": "运行中",
    "blocked": "阻塞",
    "done": "已完成",
    "failed": "异常",
    "error": "异常",
    "offline": "离线",
}
#: 自报成「在做」的状态；只有自报且仍在窗口内才算「运行中」，过期仍是「未验证」。
REPORTED_RUNNING = ("running", "working", "in_progress")
#: 只有这些事件代表「有谁真的跑过」；项目/任务创建等生命周期事件不算执行证据。
EVENT_TYPE_LABELS = {
    "subagent_run": "Subagent 运行",
    "session_activity": "会话活动",
    "status_report": "状态自报",
    "tool_use": "工具调用",
    "tool_result": "工具结果",
}


def fresh_cutoff(now: datetime | None = None) -> str:
    """「运行中」窗口起点（ISO 字符串，可直接与事件时间做字典序比较）。"""
    base = now or datetime.now(timezone.utc)
    return (base - timedelta(minutes=RUNNING_MINUTES)).isoformat(timespec="seconds")


def reported_label(state: Any) -> str | None:
    """自报状态的中文标签；未知值原样返回，空值返回 None。"""
    text = str(state or "").strip()
    if not text:
        return None
    return REPORTED_LABELS.get(text.lower(), text)


def event_state(event: dict[str, Any], cutoff: str) -> str:
    """单个事件的状态；缺证据一律「未验证」，不猜成功。

    状态自报、会话活动本身不带执行结果：自报「运行中」且仍在窗口内算「运行中」，
    过期或自报空闲/阻塞的仍是「未验证」——自报是回显，不是成功证据。
    """
    ok = event.get("ok")
    fresh = str(event.get("occurred_at") or "") >= cutoff
    reported = str(event.get("reported_state") or "").strip().lower()
    if reported:
        if reported in REPORTED_RUNNING:
            return "running" if fresh else "unverified"
        # 自报空闲/阻塞/完成：只是回显，既不是证据，也不能算「运行中」。
        return "unverified"
    if event.get("event_type") == "subagent_run":
        if ok == 1:
            return "ok"
        if ok == 0:
            return "running" if fresh else "unverified"
    if ok is None:
        return "running" if fresh else "unverified"
    return "ok" if ok else "failed"
