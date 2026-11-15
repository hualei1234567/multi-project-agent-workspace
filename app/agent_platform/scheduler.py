"""自主调度：把「可以做的任务」排成执行计划（M3-1 T3-102）。

**本模块只做计算，不启动任何进程。** 执行由 ``executor.py`` 负责，两者分离是为了
让排程逻辑能被独立验证：先确认「谁该做什么」排得对，再谈「怎么跑」。

调度判定一条任务可执行，需要同时满足：
1. 状态为 ``todo``（含从 TASK_BOARD 映射来的 READY/BACKLOG）
2. 所有依赖任务都是 ``done``
3. 已指派 ``assignee_agent_id``，且该 Agent 属于本项目
4. 该任务当前没有活跃调度项（leased/running），避免双跑
5. 任务可写（``source='board'`` 的镜像任务只读，不参与调度）

排序：优先级（P0 最前）→ 依赖深度（浅的在前）→ 创建时间（早的在前）。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from . import store

#: 参与调度的任务状态。review/blocked/done 不可调度。
SCHEDULABLE_STATUSES = frozenset({"todo"})

#: 活跃调度项状态（有这些状态的项存在时，任务不可再次入队）。
ACTIVE_ITEM_STATUSES = ("queued", "leased", "running")

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

#: 执行模式白名单：只有这些模式的角色允许被自动拉起。
#: readonly 角色（架构/UX/QA）负责评审，自动执行会绕过人工判断，因此排除。
EXECUTABLE_MODES = frozenset({"worktree", "subagent"})

PLAN_STATUSES = frozenset({"draft", "approved", "running", "done", "aborted"})

#: 三模式（契约 CF-4）。核心区别：shadow/recommend **不创建 dispatch_item**，
#: 执行器只认 dispatch_items，所以它们在结构上就不可能起进程。
PLAN_MODES = frozenset({"shadow", "recommend", "assisted"})

#: 平台默认模式。RECOMMEND = 展示推荐等人决策，不自动派发。
DEFAULT_MODE = "recommend"

#: 会创建 dispatch_item 的模式（只有 assisted）。
EXECUTABLE_PLAN_MODES = frozenset({"assisted"})
ITEM_STATUSES = frozenset(
    {"queued", "leased", "running", "succeeded", "failed", "cancelled", "skipped"}
)


class SchedulerError(Exception):
    """调度域错误；HTTP 层统一映射为 PlatformError 同形响应。"""

    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


# ---------------------------------------------------------------------------
# 只读判定
# ---------------------------------------------------------------------------
def _task_rows(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, task_key, title, status, priority, assignee_agent_id, source,"
        "       blocked_reason, updated_at, version"
        "  FROM tasks WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _dependency_map(conn: sqlite3.Connection, project_id: str) -> dict[str, set[str]]:
    """task_id -> 依赖的 task_id 集合。

    三期 M3-2：跨项目依赖的 `task_dependencies.project_id` 记录的是**源**项目，
    依赖目标可能在别的项目里，所以这里按 task_id 取全部依赖，再连表确认目标存在。
    """
    deps: dict[str, set[str]] = {}
    rows = conn.execute(
        "SELECT d.task_id, d.depends_on_task_id"
        "  FROM task_dependencies d"
        "  JOIN tasks src ON src.id = d.task_id"
        "  JOIN tasks dst ON dst.id = d.depends_on_task_id"
        " WHERE src.project_id = ?",
        (project_id,),
    )
    for row in rows:
        deps.setdefault(row["task_id"], set()).add(row["depends_on_task_id"])
    return deps


def _dep_tasks(conn: sqlite3.Connection, task_ids: set[str]) -> dict[str, dict[str, Any]]:
    """跨项目依赖的目标任务可能在别的项目里，需要单独取（含状态与 key）。"""
    if not task_ids:
        return {}
    placeholders = ",".join("?" * len(task_ids))
    rows = conn.execute(
        f"SELECT id, task_key, title, status, source, priority FROM tasks"
        f" WHERE id IN ({placeholders})",
        tuple(task_ids),
    )
    return {row["id"]: dict(row) for row in rows}


def _active_task_ids(conn: sqlite3.Connection, project_id: str) -> set[str]:
    placeholders = ",".join("?" * len(ACTIVE_ITEM_STATUSES))
    rows = conn.execute(
        f"SELECT DISTINCT task_id FROM dispatch_items"
        f" WHERE project_id = ? AND task_id IS NOT NULL AND status IN ({placeholders})",
        (project_id, *ACTIVE_ITEM_STATUSES),
    ).fetchall()
    return {row["task_id"] for row in rows}


def _agent_modes(conn: sqlite3.Connection, project_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, role_key, display_name, execution_mode, status FROM project_agents"
        " WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    return {row["id"]: dict(row) for row in rows}


def _depth(task_id: str, deps: dict[str, set[str]], memo: dict[str, int]) -> int:
    """依赖深度：无依赖为 0，否则 1 + 依赖里的最大深度。带环保护。"""
    if task_id in memo:
        return memo[task_id]
    memo[task_id] = 0  # 环上先占位，避免无限递归
    value = 0
    for dep in deps.get(task_id, ()):  # type: ignore[union-attr]
        value = max(value, 1 + _depth(dep, deps, memo))
    memo[task_id] = value
    return value


def schedulable(
    conn: sqlite3.Connection, project_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (可执行任务, 被跳过的任务及原因)。纯读，不写库。"""
    tasks = {task["id"]: task for task in _task_rows(conn, project_id)}
    deps = _dependency_map(conn, project_id)
    # 依赖目标可能跨项目：单独查，但不并入候选集合（否则会被当成可调度任务）
    dep_ids = set().union(*deps.values()) if deps else set()
    dep_tasks = _dep_tasks(conn, dep_ids)
    active = _active_task_ids(conn, project_id)
    agents = _agent_modes(conn, project_id)
    registry_blocked = _registry_blocked_roles(conn)
    memo: dict[str, int] = {}

    ready: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for task in tasks.values():
        reason = _skip_reason(task, tasks, dep_tasks, deps, active, agents,
                              registry_blocked)
        if reason:
            skipped.append({
                "task_id": task["id"],
                "task_key": task["task_key"],
                "title": task["title"],
                "status": task["status"],
                "reason": reason,
            })
            continue
        ready.append({
            **task,
            "depth": _depth(task["id"], deps, memo),
            "agent": agents[task["assignee_agent_id"]],
        })

    ready.sort(key=lambda t: (
        PRIORITY_ORDER.get(t["priority"] or "P2", 9),
        t["depth"],
        t["updated_at"] or "",
        t["task_key"],
    ))
    return ready, skipped


def _registry_blocked_roles(conn: sqlite3.Connection) -> dict[str, str]:
    """Registry 里状态不是 active 的角色 → 拒绝原因。

    只查一次给整批候选使用。表不存在时（老库未迁移）返回空——
    不能因为 Registry 没建就把整个调度打死。
    """
    try:
        rows = conn.execute(
            "SELECT role_key, status FROM registry_agent_roles WHERE status <> 'active'"
        ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    out: dict[str, str] = {}
    for row in rows:
        if row["status"] == "revoked":
            out[row["role_key"]] = "角色在 Registry 中已撤销"
        else:
            out[row["role_key"]] = f"角色在 Registry 中状态为 {row['status']}"
    return out


def _skip_reason(
    task: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    dep_tasks: dict[str, dict[str, Any]],
    deps: dict[str, set[str]],
    active: set[str],
    agents: dict[str, dict[str, Any]],
    registry_blocked: dict[str, str] | None = None,
) -> str | None:
    if task["status"] not in SCHEDULABLE_STATUSES:
        return f"状态 {task['status']} 不可调度"
    if task.get("source") == "board":
        return "镜像任务只读"
    if not task.get("assignee_agent_id"):
        return "未指派执行角色"
    agent = agents.get(task["assignee_agent_id"])
    if agent is None:
        return "指派的角色不属于本项目"
    if agent["execution_mode"] not in EXECUTABLE_MODES:
        return f"角色执行模式 {agent['execution_mode']} 不允许自动执行"
    # P3-M2 Registry 硬过滤：角色被撤销/废弃的不进候选。
    # 这是"注册中心真的影响调度"的接线点——建了不用的 Registry 等于没建。
    if registry_blocked and agent["role_key"] in registry_blocked:
        return registry_blocked[agent["role_key"]]
    if task["id"] in active:
        return "已有在途调度项"
    for dep_id in deps.get(task["id"], ()):  # type: ignore[union-attr]
        dep = dep_tasks.get(dep_id) or tasks.get(dep_id)
        if dep is None:
            return "依赖任务不存在"
        if dep["status"] != "done":
            return f"依赖 {dep['task_key']} 未完成"
    return None


# ---------------------------------------------------------------------------
# 候选评分（契约 CF-4 §3）
# ---------------------------------------------------------------------------
def _score_candidates(
    conn: sqlite3.Connection, ready: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """给候选打分并生成「为什么推荐它」的理由。

    维度：优先级 40 / 依赖就绪 20 / 角色能力 25 / 等待时长 15。

    Registry 能力分为 0 时**不减分**，只标 ``capability_unknown``——
    不能因为没登记能力就永远排最后，那是用数据缺失惩罚功能。
    """
    from . import registry as registry_service

    out: list[dict[str, Any]] = []
    now = store.now_iso()
    for task in ready:
        agent = task.get("agent") or {}
        role_key = agent.get("role_key") or ""
        prio = PRIORITY_ORDER.get(task.get("priority") or "P2", 2)
        prio_score = 40 - prio * 10  # P0=40 P1=30 P2=20 P3=10

        # 能走到这里说明依赖已就绪（硬过滤在前），给满分。
        dep_score = 20

        # 角色能力：取该角色最高能力分做归一（0..5 → 0..25）
        cap_score = 0.0
        cap_note = "capability_unknown"
        role_id = agent.get("registry_role_id") or f"role_{role_key}"
        try:
            rows = conn.execute(
                "SELECT capability FROM registry_agent_capabilities WHERE role_id = ?",
                (role_id,),
            ).fetchall()
            if rows:
                best = max(
                    (registry_service.capability_score(conn, role_id, r["capability"])
                     for r in rows),
                    default=0.0,
                )
                cap_score = (best / 5.0) * 25.0
                cap_note = f"capability_best={best:.1f}"
        except sqlite3.DatabaseError:
            # Registry 表不存在（老库未迁移）时不影响评分，
            # 只是拿不到能力分——不给功能挖坑。
            cap_score = 0.0

        # 等待时长：越旧越靠前，最多 15 分（24h 封顶）
        wait_score = 0.0
        updated = task.get("updated_at")
        if updated:
            from datetime import datetime, timezone
            try:
                age_h = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(updated)).total_seconds() / 3600
                wait_score = min(age_h / 24.0, 1.0) * 15.0
            except ValueError:
                pass

        total = round(prio_score + dep_score + cap_score + wait_score, 2)
        reason_bits = [
            f"优先级 {task.get('priority') or 'P2'}（{prio_score} 分）",
            "依赖已就绪（20 分）",
        ]
        if cap_score > 0:
            reason_bits.append(f"角色能力匹配（{cap_score:.1f} 分）")
        else:
            reason_bits.append("角色未登记能力（不加分也不减分）")
        if wait_score > 0:
            reason_bits.append(f"等待时长（{wait_score:.1f} 分）")

        out.append({
            "task_id": task["id"],
            "agent_id": task.get("assignee_agent_id"),
            "role_key": role_key,
            "score": total,
            "score_detail": {
                "priority": prio_score,
                "dependency": dep_score,
                "capability": round(cap_score, 2),
                "wait": round(wait_score, 2),
                "capability_note": cap_note,
                "computed_at": now,
            },
            "reason": " · ".join(reason_bits),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------
def create_plan(
    project_id: str,
    *,
    strategy: str = "priority",
    created_by: str = "local",
    max_items: int | None = None,
    notes: str | None = None,
    mode: str = DEFAULT_MODE,
) -> dict[str, Any]:
    """生成一份 draft 计划（纯计算后落库，不执行）。

    ``mode`` 决定落库形态（契约 CF-4）：
    - ``shadow`` / ``recommend``：只写 ``dispatch_plan_candidates``，
      **不写 ``dispatch_items``**——执行器看不到，结构上不可能派发。
    - ``assisted``：写 ``dispatch_items``，需审批后执行。
    """
    if strategy not in {"manual", "priority", "dependency"}:
        raise SchedulerError("invalid_payload", 422, "strategy 非法")
    if mode not in PLAN_MODES:
        raise SchedulerError("invalid_mode", 422,
                             f"mode 必须是 {sorted(PLAN_MODES)} 之一")

    conn = store.connect()
    try:
        store.init_db(conn)
        row = conn.execute("SELECT id FROM agent_projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise SchedulerError("project_not_found", 404, "项目不存在")

        ready, _skipped = schedulable(conn, project_id)
        if max_items is not None and max_items >= 0:
            ready = ready[:max_items]

        now = store.now_iso()
        plan_id = uuid.uuid4().hex
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO dispatch_plans"
                " (id, project_id, status, strategy, mode, created_by, notes,"
                "  created_at, version)"
                " VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, 1)",
                (plan_id, project_id, strategy, mode, created_by, notes, now),
            )
            if mode in EXECUTABLE_PLAN_MODES:
                # ASSISTED：写 dispatch_items，走审批闸门后执行。
                for task in ready:
                    conn.execute(
                        "INSERT INTO dispatch_items"
                        " (id, plan_id, project_id, task_id, agent_id, role_key, status,"
                        "  priority, attempt, max_attempts, result_json, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, 0, ?, '{}', ?, ?)",
                        (
                            uuid.uuid4().hex,
                            plan_id,
                            project_id,
                            task["id"],
                            task["assignee_agent_id"],
                            task["agent"]["role_key"],
                            task["priority"] or "P2",
                            _max_attempts(conn, project_id),
                            now,
                            now,
                        ),
                    )
            else:
                # SHADOW / RECOMMEND：只写候选，不建 item。
                for rank, item in enumerate(_score_candidates(conn, ready), start=1):
                    conn.execute(
                        "INSERT INTO dispatch_plan_candidates"
                        " (id, plan_id, project_id, task_id, agent_id, role_key,"
                        "  score, score_detail, reason, rank, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            uuid.uuid4().hex,
                            plan_id,
                            project_id,
                            item["task_id"],
                            item["agent_id"],
                            item["role_key"],
                            item["score"],
                            store.dumps_json(item["score_detail"]),
                            item["reason"],
                            rank,
                            now,
                        ),
                    )
        _safe_audit(conn, project_id, "dispatch.plan.create", "dispatch_plan", plan_id, {
            "strategy": strategy,
            "mode": mode,
            "items": len(ready),
            "skipped": len(_skipped),
            "created_by": created_by,
            "creates_dispatch_items": mode in EXECUTABLE_PLAN_MODES,
        })
        return get_plan(project_id, plan_id, conn=conn)
    finally:
        conn.close()


def _safe_audit(
    conn: sqlite3.Connection,
    project_id: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None,
    detail: dict[str, Any],
    *,
    result: str = "ok",
) -> None:
    """写统一审计；失败不阻断业务（审计尽力而为）。"""
    try:
        from . import service

        service.audit(conn, project_id, action, resource_type, resource_id,
                      detail=detail, result=result)
    except Exception:  # noqa: BLE001,S110 - 审计失败不能影响主流程
        pass


def _max_attempts(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        "SELECT settings_json FROM agent_projects WHERE id = ?", (project_id,)
    ).fetchone()
    settings = store.loads_json(row["settings_json"] if row else None, {}) or {}
    try:
        return max(1, min(5, int(settings.get("dispatch_max_attempts", 2))))
    except (TypeError, ValueError):
        return 2


def get_plan(
    project_id: str, plan_id: str, *, conn: sqlite3.Connection | None = None
) -> dict[str, Any]:
    own = conn is None
    conn = conn or store.connect()
    try:
        if own:
            store.init_db(conn)
        row = conn.execute(
            "SELECT * FROM dispatch_plans WHERE id = ? AND project_id = ?",
            (plan_id, project_id),
        ).fetchone()
        if row is None:
            raise SchedulerError("not_found", 404, "调度计划不存在")
        plan = dict(row)
        items = [
            dict(item)
            for item in conn.execute(
                "SELECT * FROM dispatch_items WHERE plan_id = ?"
                " ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,"
                "          created_at, id",
                (plan_id,),
            )
        ]
        for item in items:
            item["result"] = store.loads_json(item.pop("result_json", None), {})
        plan["items"] = items
        plan["counts"] = _item_counts(items)
        # SHADOW / RECOMMEND 没有 items，候选在候选表里（契约 CF-4）。
        plan["candidates"] = _plan_candidates(conn, plan_id)
        plan["creates_dispatch_items"] = plan.get("mode") in EXECUTABLE_PLAN_MODES
        return plan
    finally:
        if own:
            conn.close()


def _plan_candidates(conn: sqlite3.Connection, plan_id: str) -> list[dict[str, Any]]:
    """计划的候选集（SHADOW / RECOMMEND 才有）。含评分明细与理由。"""
    try:
        rows = conn.execute(
            "SELECT * FROM dispatch_plan_candidates WHERE plan_id = ? ORDER BY rank",
            (plan_id,),
        ).fetchall()
    except sqlite3.DatabaseError:
        return []
    out = []
    for r in rows:
        item = dict(r)
        item["score_detail"] = store.loads_json(item.get("score_detail"), {}) or {}
        out.append(item)
    return out


def list_plans(
    project_id: str, *, page: int = 1, page_size: int = 20
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    conn = store.connect()
    try:
        store.init_db(conn)
        total = conn.execute(
            "SELECT COUNT(*) FROM dispatch_plans WHERE project_id = ?", (project_id,)
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT p.*,"
            "  (SELECT COUNT(*) FROM dispatch_items i WHERE i.plan_id = p.id) AS item_total"
            " FROM dispatch_plans p WHERE p.project_id = ?"
            " ORDER BY p.created_at DESC LIMIT ? OFFSET ?",
            (project_id, page_size, (page - 1) * page_size),
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": int(total or 0),
            "page": page,
            "page_size": page_size,
        }
    finally:
        conn.close()


def _item_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def get_item(project_id: str, item_id: str) -> dict[str, Any]:
    conn = store.connect()
    try:
        store.init_db(conn)
        row = conn.execute(
            "SELECT * FROM dispatch_items WHERE id = ? AND project_id = ?",
            (item_id, project_id),
        ).fetchone()
        if row is None:
            raise SchedulerError("not_found", 404, "调度项不存在")
        item = dict(row)
        item["result"] = store.loads_json(item.pop("result_json", None), {})
        item["logs"] = [
            dict(log)
            for log in conn.execute(
                "SELECT stream, seq, content, created_at FROM dispatch_logs"
                " WHERE item_id = ? ORDER BY seq, created_at",
                (item_id,),
            )
        ]
        item["events"] = [
            dict(event)
            for event in conn.execute(
                "SELECT event_type, from_status, to_status, detail_json, created_at"
                " FROM dispatch_events WHERE item_id = ? ORDER BY created_at",
                (item_id,),
            )
        ]
        return item
    finally:
        conn.close()
