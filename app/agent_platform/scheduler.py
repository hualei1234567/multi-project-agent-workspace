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
    memo: dict[str, int] = {}

    ready: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for task in tasks.values():
        reason = _skip_reason(task, tasks, dep_tasks, deps, active, agents)
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


def _skip_reason(
    task: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    dep_tasks: dict[str, dict[str, Any]],
    deps: dict[str, set[str]],
    active: set[str],
    agents: dict[str, dict[str, Any]],
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
# 计划
# ---------------------------------------------------------------------------
def create_plan(
    project_id: str,
    *,
    strategy: str = "priority",
    created_by: str = "local",
    max_items: int | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """生成一份 draft 计划（纯计算后落库，不执行）。"""
    if strategy not in {"manual", "priority", "dependency"}:
        raise SchedulerError("invalid_payload", 422, "strategy 非法")

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
                " (id, project_id, status, strategy, created_by, notes, created_at, version)"
                " VALUES (?, ?, 'draft', ?, ?, ?, ?, 1)",
                (plan_id, project_id, strategy, created_by, notes, now),
            )
            for task in ready:
                conn.execute(
                    "INSERT INTO dispatch_items"
                    " (id, plan_id, project_id, task_id, agent_id, role_key, status, priority,"
                    "  attempt, max_attempts, result_json, created_at, updated_at)"
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
        return get_plan(project_id, plan_id, conn=conn)
    finally:
        conn.close()


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
        return plan
    finally:
        if own:
            conn.close()


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
