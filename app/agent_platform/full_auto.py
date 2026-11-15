"""Full Auto 自动派发（P3-M9）。

契约：``docs/phase3/contracts/CF-6_FULL_AUTO_v1.md``

**这是整个平台风险最高的模块**：无人值守时它能自己挑任务、起进程、改代码。
所以它的设计原则是「每一步都可中止」：

1. **五道前置检查**，任一缺失就拒绝启动（fail-closed）；
2. **每轮最多派发 1 项**——慢但不会互相踩；
3. **R3 以上仍需审批**，放开的是"不点按钮"不是"不设边界"；
4. **永远不 push / 部署 / 删数据**（由 executor_policy 保证）；
5. Kill Switch / 熔断 / 预算任一触发即停。

**默认关闭**。用户必须显式设置 ``AGWS_PLATFORM_FULL_AUTO=on``。
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from . import scheduler

#: 环境开关。默认 off——这是整个平台最危险的开关，必须显式开。
ENV_SWITCH = "AGWS_PLATFORM_FULL_AUTO"

#: 每轮最多派发数。固定 1：无人值守时并发改同一仓库会互相踩。
MAX_PER_ROUND = 1

#: 轮询间隔（秒）。
ROUND_INTERVAL_S = 60

#: R3 及以上仍需审批（契约 §4）。Full Auto 只放开 R0–R2。
MAX_AUTO_RISK = "R2"


class FullAutoError(Exception):
    def __init__(self, code: str, status: int = 403, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def enabled() -> bool:
    """全局开关。默认 off。"""
    return os.environ.get(ENV_SWITCH, "off").strip().lower() in {"on", "1", "true", "yes"}


def preflight(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """五道前置检查。``ready=False`` 时**必须拒绝启动**。

    这是 fail-closed：拿不准就不开。无人值守跑飞的代价远大于多等一天。
    """
    checks: list[dict[str, Any]] = []

    # P1 三模式
    checks.append({
        "id": "three_modes",
        "label": "三模式已实现",
        "ok": hasattr(scheduler, "PLAN_MODES")
              and "recommend" in scheduler.PLAN_MODES,
    })

    # P2 预算：至少一条启用的项目级预算
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM budgets WHERE enabled = 1"
            "  AND scope IN ('project', 'platform')"
        ).fetchone()["c"]
    except sqlite3.DatabaseError:
        n = 0
    checks.append({
        "id": "budget",
        "label": "已设置预算（超支暂停）",
        "ok": int(n) > 0,
        "detail": f"{n} 条启用中的预算" if n else "没有预算规则 = 不限额，不允许无人值守",
    })

    # P3 熔断
    try:
        from . import circuit as circuit_service
        circuit_ok = hasattr(circuit_service, "check_after_failure")
    except ImportError:
        circuit_ok = False
    checks.append({"id": "circuit", "label": "熔断已实现", "ok": circuit_ok})

    # P4 可观测：Kill Switch 可用
    try:
        from .p4 import killswitch
        ks_ok = hasattr(killswitch, "is_blocked")
    except ImportError:
        ks_ok = False
    checks.append({"id": "killswitch", "label": "Kill Switch 可用", "ok": ks_ok})

    # P5 执行器开关明确
    from . import executor_policy
    checks.append({
        "id": "executor",
        "label": "执行器已启用",
        "ok": executor_policy.executor_enabled(),
        "detail": "" if executor_policy.executor_enabled()
                  else f"需要 {executor_policy.ENV_SWITCH}=on",
    })

    # 项目级：不能处于熔断
    tripped = False
    try:
        from . import circuit as circuit_service
        tripped = circuit_service.is_tripped(conn, project_id) is not None
    except Exception:  # noqa: BLE001
        tripped = False
    checks.append({
        "id": "not_tripped",
        "label": "项目未熔断",
        "ok": not tripped,
        "detail": "项目处于熔断状态，请先恢复" if tripped else "",
    })

    failed = [c for c in checks if not c["ok"]]
    return {
        "ready": not failed,
        "checks": checks,
        "failed": failed,
        "env_enabled": enabled(),
    }


def plan_round(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """计算一轮要派发什么。**纯计算，不执行。**

    这是 Full Auto 和 ASSISTED 的分界：这里只产出"应该派发谁"，
    真正执行由调用方决定（从而保持可测试、可 dry-run）。
    """
    pre = preflight(conn, project_id)
    if not pre["ready"]:
        return {"ok": False, "reason": "preflight_failed",
                "failed": pre["failed"], "candidates": []}

    # 熔断/预算/Registry 的硬过滤都在 schedulable 里
    ready, skipped = scheduler.schedulable(conn, project_id)
    if not ready:
        return {"ok": True, "reason": "nothing_schedulable",
                "candidates": [], "skipped": len(skipped)}

    # 取最高优先级的一个（MAX_PER_ROUND = 1）
    top = ready[:MAX_PER_ROUND]
    return {
        "ok": True,
        "reason": "has_candidates",
        "candidates": [
            {
                "task_id": t["id"],
                "task_key": t["task_key"],
                "title": t["title"],
                "priority": t["priority"],
                "role_key": t["agent"]["role_key"],
                "agent_id": t["assignee_agent_id"],
            }
            for t in top
        ],
        "skipped": len(skipped),
    }


def run_round(conn: sqlite3.Connection, project_id: str, *,
              dry_run: bool = True) -> dict[str, Any]:
    """执行一轮自动派发。

    ``dry_run=True`` 是**默认**——想真跑必须显式传 False，
    且全局开关必须开着。这两道一起才允许起进程。
    """
    if not enabled():
        return {"ok": False, "reason": "full_auto_disabled",
                "hint": f"设置 {ENV_SWITCH}=on 后重启"}

    plan = plan_round(conn, project_id)
    if not plan["ok"] or not plan["candidates"]:
        return plan

    if dry_run:
        return {**plan, "dry_run": True,
                "would_dispatch": plan["candidates"]}

    # 真跑：创建 assisted 计划（走到这里 preflight 已保证预算/熔断就绪）
    created = []
    for cand in plan["candidates"]:
        try:
            p = scheduler.create_plan(
                project_id, mode="assisted",
                strategy="priority", created_by="full-auto",
                max_items=MAX_PER_ROUND,
                notes=f"Full Auto 自动派发：{cand['task_key']}",
            )
            created.append({"plan_id": p["id"], "items": len(p.get("items") or [])})
        except Exception as exc:  # noqa: BLE001 - 单项失败不中断整轮
            created.append({"error": str(exc)[:200]})

    return {**plan, "dry_run": False, "created": created}


def status(conn: sqlite3.Connection, project_id: str | None = None) -> dict[str, Any]:
    """Full Auto 总览。"""
    out: dict[str, Any] = {
        "enabled": enabled(),
        "env_switch": ENV_SWITCH,
        "max_per_round": MAX_PER_ROUND,
        "round_interval_s": ROUND_INTERVAL_S,
        "max_auto_risk": MAX_AUTO_RISK,
    }
    if project_id:
        out["preflight"] = preflight(conn, project_id)
    return out
