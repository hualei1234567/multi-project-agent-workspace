"""预算与效能（P3-M7）。

契约：``docs/phase3/contracts/CF-5_BUDGET_v1.md``

**为什么 Full Auto 需要这个**：无人值守时最怕没有刹车。超支只暂停**新任务**，
不强杀运行中的——V4.0 明文要求，杀正在跑的会留下半成品工作树，
代价比多花点钱大。

成本口径复用 ``cost.py`` 的三档（ACTUAL / ESTIMATED / LEGACY），
不重复造。预算判定优先 actual，没有则用 estimated，
两者都没有则跳过但**报出来**——否则预算数字看着精确却不可信。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import store

#: 预算作用域。
SCOPES = frozenset({"platform", "project", "task"})

#: 时间窗口。
WINDOWS = frozenset({"daily", "monthly", "total"})

#: 预算状态。exceeded 时暂停新任务（不强杀运行中的）。
STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_EXCEEDED = "exceeded"

#: 默认预警比例（80%）。
DEFAULT_WARNING_RATIO = 0.8


class BudgetError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _now() -> str:
    return store.now_iso()


def _window_start(window: str) -> str:
    """窗口起点的 ISO 时间。"""
    now = datetime.now(timezone.utc)
    if window == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif window == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        # total：不限时间，用一个足够早的起点
        start = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return start.isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 预算 CRUD
# ---------------------------------------------------------------------------
def set_budget(
    conn: sqlite3.Connection,
    *,
    scope: str,
    scope_id: str | None,
    window: str,
    limit_usd: float,
    warning_ratio: float = DEFAULT_WARNING_RATIO,
    note: str | None = None,
    created_by: str = "local",
) -> dict[str, Any]:
    """设置预算。同作用域+窗口已存在则更新（不叠加）。"""
    if scope not in SCOPES:
        raise BudgetError("invalid_scope", 400, f"scope 必须是 {sorted(SCOPES)}")
    if window not in WINDOWS:
        raise BudgetError("invalid_window", 400, f"window 必须是 {sorted(WINDOWS)}")
    if scope != "platform" and not scope_id:
        raise BudgetError("scope_id_required", 400,
                          f"{scope} 预算必须指定 scope_id")
    if limit_usd < 0:
        raise BudgetError("invalid_limit", 400, "限额不能为负")
    if not (0 < warning_ratio <= 1):
        raise BudgetError("invalid_warning_ratio", 400,
                          "预警比例必须在 (0, 1]")

    scope_key = scope_id if scope != "platform" else None
    existing = conn.execute(
        "SELECT id FROM budgets WHERE scope = ? AND"
        "  (scope_id IS ? OR scope_id = ?) AND window = ?",
        (scope, scope_key, scope_key, window),
    ).fetchone()

    now = _now()
    if existing is not None:
        with store.write_tx(conn):
            conn.execute(
                "UPDATE budgets SET limit_usd = ?, warning_ratio = ?, note = ?,"
                " updated_at = ? WHERE id = ?",
                (float(limit_usd), float(warning_ratio), note, now, existing["id"]),
            )
        return get_budget(conn, existing["id"])

    bid = _new_id("budget")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO budgets (id, scope, scope_id, window, limit_usd,"
            " warning_ratio, enabled, note, created_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (bid, scope, scope_key, window, float(limit_usd), float(warning_ratio),
             note, created_by, now, now),
        )
    return get_budget(conn, bid)


def get_budget(conn: sqlite3.Connection, budget_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM budgets WHERE id = ?", (budget_id,)).fetchone()
    if row is None:
        raise BudgetError("not_found", 404, "预算不存在")
    return dict(row)


def list_budgets(conn: sqlite3.Connection, *,
                 scope: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM budgets WHERE 1=1"
    args: list[Any] = []
    if scope:
        sql += " AND scope = ?"
        args.append(scope)
    sql += " ORDER BY scope, window"
    return [dict(r) for r in conn.execute(sql, args)]


def delete_budget(conn: sqlite3.Connection, budget_id: str) -> None:
    get_budget(conn, budget_id)
    with store.write_tx(conn):
        conn.execute("DELETE FROM budgets WHERE id = ?", (budget_id,))


# ---------------------------------------------------------------------------
# 花费统计
# ---------------------------------------------------------------------------
def _spent(conn: sqlite3.Connection, *, scope: str, scope_id: str | None,
           window: str) -> dict[str, Any]:
    """窗口内的花费。

    返回 ``{spent, sample, estimated_share}``：
    - ``spent``：实际计入的金额
    - ``sample``：参与统计的记录数（为 0 时预算判定无意义）
    - ``estimated_share``：估算金额占比（0..1）。必须暴露——
      否则预算数字看着精确却不可信。
    """
    since = _window_start(window)
    sql = (
        "SELECT"
        "  COALESCE(SUM(COALESCE(e.actual_cost_usd, e.estimated_cost_usd)), 0) AS total,"
        "  COUNT(*) AS n,"
        "  COALESCE(SUM(CASE WHEN e.actual_cost_usd IS NULL"
        "                    AND e.estimated_cost_usd IS NOT NULL"
        "               THEN e.estimated_cost_usd ELSE 0 END), 0) AS est"
        " FROM executions e"
        " WHERE e.started_at >= ?"
        "   AND (e.actual_cost_usd IS NOT NULL OR e.estimated_cost_usd IS NOT NULL)"
    )
    args: list[Any] = [since]
    if scope == "project" and scope_id:
        sql += " AND e.project_id = ?"
        args.append(scope_id)
    elif scope == "task" and scope_id:
        sql += " AND e.task_id = ?"
        args.append(scope_id)

    row = conn.execute(sql, args).fetchone()
    total = float(row["total"] or 0)
    est = float(row["est"] or 0)
    n = int(row["n"] or 0)
    return {
        "spent": round(total, 6),
        "sample": n,
        "estimated_share": round(est / total, 4) if total > 0 else 0.0,
    }


def evaluate(conn: sqlite3.Connection, budget_id: str) -> dict[str, Any]:
    """评估单条预算的当前状态。"""
    budget = get_budget(conn, budget_id)
    if not budget["enabled"]:
        return {**budget, "status": STATUS_OK, "spent_usd": 0.0,
                "usage_ratio": 0.0, "sample": 0, "estimated_share": 0.0,
                "reason": "budget_disabled"}

    spent = _spent(conn, scope=budget["scope"], scope_id=budget["scope_id"],
                   window=budget["window"])
    limit = float(budget["limit_usd"])
    ratio = (spent["spent"] / limit) if limit > 0 else 0.0

    if limit > 0 and spent["spent"] >= limit:
        status = STATUS_EXCEEDED
    elif ratio >= float(budget["warning_ratio"]):
        status = STATUS_WARNING
    else:
        status = STATUS_OK

    return {
        **budget,
        "status": status,
        "spent_usd": spent["spent"],
        "usage_ratio": round(ratio, 4),
        "sample": spent["sample"],
        "estimated_share": spent["estimated_share"],
        "reason": "",
    }


def status_for(
    conn: sqlite3.Connection, *, project_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """某项目/任务的有效预算状态。**取最严格的那个**（契约 §2）。

    返回 ``{status, blockers, budgets}``。任一层 exceeded 即整体 exceeded。
    """
    candidates: list[dict[str, Any]] = []

    rows = conn.execute(
        "SELECT id FROM budgets WHERE enabled = 1 AND scope = 'platform'"
    ).fetchall()
    for r in rows:
        candidates.append(evaluate(conn, r["id"]))

    if project_id:
        rows = conn.execute(
            "SELECT id FROM budgets WHERE enabled = 1 AND scope = 'project'"
            "  AND scope_id = ?", (project_id,),
        ).fetchall()
        for r in rows:
            candidates.append(evaluate(conn, r["id"]))
    if task_id:
        rows = conn.execute(
            "SELECT id FROM budgets WHERE enabled = 1 AND scope = 'task'"
            "  AND scope_id = ?", (task_id,),
        ).fetchall()
        for r in rows:
            candidates.append(evaluate(conn, r["id"]))

    # 取最严格：exceeded > warning > ok
    order = {STATUS_EXCEEDED: 2, STATUS_WARNING: 1, STATUS_OK: 0}
    worst = max(candidates, key=lambda b: order[b["status"]], default=None)
    status = worst["status"] if worst else STATUS_OK

    blockers = []
    if status == STATUS_EXCEEDED:
        exceeded = [b for b in candidates if b["status"] == STATUS_EXCEEDED]
        for b in exceeded:
            blockers.append(
                f"{b['scope']}/{b['window']} 预算已用尽："
                f"${b['spent_usd']:.4f} / ${b['limit_usd']:.2f}"
            )

    return {"status": status, "blockers": blockers, "budgets": candidates}


def record_transition(conn: sqlite3.Connection, *,
                      previous: str, current: str) -> None:
    """状态跃迁时写事件。只在跃迁时写，不是每次都写。"""
    if previous == current:
        return
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO budget_events (id, budget_id, event_type, spent_usd,"
                " limit_usd, detail_json, created_at)"
                " VALUES (?, 'system', ?, 0, 0, ?, ?)",
                (_new_id("bev"), f"{previous}->{current}",
                 store.dumps_json({"from": previous, "to": current}), _now()),
            )
    except sqlite3.DatabaseError:
        # 事件写入失败不能改变判定结果
        return


# ---------------------------------------------------------------------------
# 效能指标
# ---------------------------------------------------------------------------
def efficiency(conn: sqlite3.Connection, project_id: str,
               *, days: int = 30) -> dict[str, Any]:
    """效能指标。样本不足显示 UNKNOWN，不算 0（与新鲜度同一原则）。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)
             ).isoformat(timespec="seconds")

    # Lead Time：创建 → 完成
    lead = conn.execute(
        "SELECT AVG(julianday(t.completed_at) - julianday(t.updated_at)) * 86400 AS avg_s,"
        "       COUNT(*) AS n"
        " FROM tasks t WHERE t.project_id = ? AND t.status = 'done'"
        "   AND t.completed_at IS NOT NULL AND t.updated_at >= ?",
        (project_id, since),
    ).fetchone()
    lead_n = int(lead["n"] or 0) if lead else 0

    # 一次通过率 & 重试率
    items = conn.execute(
        "SELECT COUNT(*) AS total,"
        "       SUM(CASE WHEN attempt = 1 AND status = 'succeeded' THEN 1 ELSE 0 END) AS first_ok,"
        "       SUM(CASE WHEN attempt > 1 THEN 1 ELSE 0 END) AS retried"
        " FROM dispatch_items WHERE project_id = ? AND created_at >= ?",
        (project_id, since),
    ).fetchone()
    total_items = int(items["total"] or 0) if items else 0

    # 人工介入率：审批数 / 执行数
    appr = conn.execute(
        "SELECT COUNT(*) AS n FROM approvals WHERE project_id = ? AND created_at >= ?",
        (project_id, since),
    ).fetchone()
    appr_n = int(appr["n"] or 0) if appr else 0

    def ratio(num: int, den: int) -> float | str:
        if den <= 0:
            return "UNKNOWN"
        return round(num / den, 4)

    return {
        "window_days": days,
        "lead_time_seconds": round(float(lead["avg_s"]), 1) if lead_n and lead["avg_s"] else "UNKNOWN",
        "lead_time_sample": lead_n,
        "first_pass_rate": ratio(int(items["first_ok"] or 0), total_items),
        "retry_rate": ratio(int(items["retried"] or 0), total_items),
        "human_intervention_rate": ratio(appr_n, total_items),
        "dispatch_items_sample": total_items,
        "approvals_sample": appr_n,
    }


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """全平台预算总览。"""
    budgets = list_budgets(conn)
    evaluated = [evaluate(conn, b["id"]) for b in budgets]
    counts = {STATUS_OK: 0, STATUS_WARNING: 0, STATUS_EXCEEDED: 0}
    for b in evaluated:
        counts[b["status"]] = counts.get(b["status"], 0) + 1
    return {
        "total": len(evaluated),
        "by_status": counts,
        "items": evaluated,
    }
