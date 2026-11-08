"""工作空间体验层：项目组合、我的工作台、统一消息中心、全局搜索。

设计约束（来自产品补充规范 §14）：

* **只读既有事实源** —— Project / Task / Delivery / Approval / Agent / Execution
  都直接查既有表，本模块不复制任何业务状态。
* **唯一新增的持久化是个人偏好** —— 消息已读状态（``notification_states``）。
* **权限先于检索** —— 所有查询先算 ``visible_project_ids``，跨项目聚合不会
  泄露无权项目。
* **未知即未知** —— 缺数据返回 ``None`` / ``unknown``，不假装 0 或成功。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from . import store

LOCAL_OWNER = "local-owner"

# 项目多久没有活动算「不新鲜」（天）
STALE_DAYS = 14


def _has(row: sqlite3.Row, key: str) -> bool:
    """sqlite3.Row 的列名判断（``key in row`` 判的是值，不是列名）。"""
    cols = row.keys()
    return key in cols


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_days(value: Any) -> float | None:
    dt = _parse(value)
    if dt is None:
        return None
    return round((_now() - dt).total_seconds() / 86400, 2)


def _scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> int:
    row = conn.execute(sql, tuple(params)).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# ---------------------------------------------------------------------------
# 权限：可见项目集合
# ---------------------------------------------------------------------------

def visible_project_ids(conn: sqlite3.Connection, principal_id: str | None) -> list[str]:
    """返回该主体可见的项目 id（未归档）。

    平台所有者（``is_platform_owner=1``）可见全部；其他主体只看
    ``project_members`` 里登记过的项目。未知主体返回空集 —— 不做兜底放行。
    """
    pid = principal_id or LOCAL_OWNER
    row = conn.execute(
        "SELECT is_platform_owner, status FROM principals WHERE id = ?", (pid,)
    ).fetchone()
    if row is not None and row["status"] != "active":
        return []
    is_owner = bool(row["is_platform_owner"]) if row is not None else pid == LOCAL_OWNER
    if is_owner:
        return [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM agent_projects WHERE status != 'archived' ORDER BY updated_at DESC"
            )
        ]
    return [
        r["project_id"]
        for r in conn.execute(
            "SELECT m.project_id FROM project_members m "
            "JOIN agent_projects p ON p.id = m.project_id "
            "WHERE m.principal_id = ? AND p.status != 'archived'",
            (pid,),
        )
    ]


def _projects(conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, code, name, status, description, repo_root, updated_at "
        f"FROM agent_projects WHERE id IN ({marks})",
        tuple(ids),
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


# ---------------------------------------------------------------------------
# 项目组合
# ---------------------------------------------------------------------------

def portfolio(conn: sqlite3.Connection | None = None,
              principal_id: str | None = None) -> dict[str, Any]:
    """项目组合视图：每个项目一条健康快照。

    健康度**不做黑箱打分**，只给可解释的分项与由显式规则推出的风险等级，
    权重与阈值留给 P4-M0 由业务 Owner 冻结（见产品补充规范 §3.3）。
    """
    own = conn is None
    conn = conn or store.connect()
    try:
        store.init_db(conn)
        ids = visible_project_ids(conn, principal_id)
        projects = _projects(conn, ids)
        items: list[dict[str, Any]] = []
        for pid in ids:
            meta = projects.get(pid)
            if meta is None:
                continue
            counts = {
                r["status"]: r["n"]
                for r in conn.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status",
                    (pid,),
                )
            }
            total = sum(counts.values())
            done = counts.get("done", 0)
            blocked = counts.get("blocked", 0)
            in_progress = counts.get("in_progress", 0)
            pending_deliveries = _scalar(
                conn,
                "SELECT COUNT(*) FROM deliveries WHERE project_id = ? AND status IN ('submitted','in_review')",
                (pid,),
            )
            pending_approvals = _scalar(
                conn,
                "SELECT COUNT(*) FROM approvals WHERE project_id = ? AND status = 'pending'",
                (pid,),
            )
            last_event = conn.execute(
                "SELECT MAX(occurred_at) FROM agent_events WHERE project_id = ?", (pid,)
            ).fetchone()
            last_event_at = last_event[0] if last_event else None
            candidates = [x for x in (meta.get("updated_at"), last_event_at) if x]
            last_active_at = max(candidates) if candidates else None
            idle_days = _age_days(last_active_at)

            # 显式风险规则（可解释，不隐藏权重）
            reasons: list[str] = []
            if blocked:
                reasons.append(f"{blocked} 个任务阻塞")
            if pending_deliveries:
                reasons.append(f"{pending_deliveries} 个交付待验收")
            if pending_approvals:
                reasons.append(f"{pending_approvals} 个审批待处理")
            if idle_days is not None and idle_days >= STALE_DAYS and total:
                reasons.append(f"{idle_days:.0f} 天无活动")
            if blocked or pending_approvals:
                risk = "high"
            elif reasons:
                risk = "medium"
            else:
                risk = "low"

            items.append({
                "id": pid,
                "code": meta.get("code"),
                "name": meta.get("name"),
                "status": meta.get("status"),
                "updated_at": meta.get("updated_at"),
                "last_active_at": last_active_at,
                "idle_days": idle_days,
                "tasks": {
                    "total": total,
                    "done": done,
                    "in_progress": in_progress,
                    "blocked": blocked,
                    "todo": counts.get("todo", 0),
                    "review": counts.get("review", 0),
                    "completion": (round(done / total, 4) if total else None),
                },
                "pending_deliveries": pending_deliveries,
                "pending_approvals": pending_approvals,
                "risk": risk,
                "risk_reasons": reasons,
                "data_quality": (
                    "stale" if idle_days is not None and idle_days >= STALE_DAYS else "fresh"
                ),
            })
        items.sort(key=lambda x: ({"high": 0, "medium": 1, "low": 2}[x["risk"]],
                                  -(x["idle_days"] or 0)))
        return {
            "generated_at": _iso(_now()),
            "principal_id": principal_id or LOCAL_OWNER,
            "project_count": len(items),
            "summary": {
                "risk_high": sum(1 for x in items if x["risk"] == "high"),
                "risk_medium": sum(1 for x in items if x["risk"] == "medium"),
                "risk_low": sum(1 for x in items if x["risk"] == "low"),
                "blocked_tasks": sum(x["tasks"]["blocked"] for x in items),
                "pending_deliveries": sum(x["pending_deliveries"] for x in items),
                "pending_approvals": sum(x["pending_approvals"] for x in items),
            },
            "projects": items,
            "note": "风险等级由显式规则推出（阻塞/待验收/待审批/久无活动），"
                    "综合健康度权重待 P4-M0 由业务 Owner 冻结。",
        }
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 我的工作台
# ---------------------------------------------------------------------------

def my_work(conn: sqlite3.Connection | None = None,
            principal_id: str | None = None,
            limit: int = 20) -> dict[str, Any]:
    """个人工作聚合：我的任务 / 我的审批 / 待我验收的交付 / 最近执行。"""
    own = conn is None
    conn = conn or store.connect()
    pid = principal_id or LOCAL_OWNER
    try:
        store.init_db(conn)
        ids = visible_project_ids(conn, pid)
        if not ids:
            return {
                "principal_id": pid, "projects": [], "tasks": [], "approvals": [],
                "deliveries": [], "executions": [], "counts": {},
            }
        marks = ",".join("?" for _ in ids)
        tasks = [
            dict(r) for r in conn.execute(
                f"""
                SELECT t.id, t.project_id, p.code AS project_code, t.task_key, t.title,
                       t.status, t.priority, t.updated_at, t.assignee_agent_id
                FROM tasks t JOIN agent_projects p ON p.id = t.project_id
                WHERE t.project_id IN ({marks})
                  AND t.status IN ('in_progress', 'blocked', 'review')
                ORDER BY CASE t.status WHEN 'blocked' THEN 0 WHEN 'review' THEN 1 ELSE 2 END,
                         t.updated_at DESC
                LIMIT ?
                """,
                (*ids, limit),
            )
        ]
        approvals = [
            dict(r) for r in conn.execute(
                f"""
                SELECT a.id, a.project_id, p.code AS project_code, a.resource_type,
                       a.resource_id, a.action, a.requested_by, a.created_at, a.expires_at
                FROM approvals a JOIN agent_projects p ON p.id = a.project_id
                WHERE a.project_id IN ({marks}) AND a.status = 'pending'
                ORDER BY a.created_at DESC LIMIT ?
                """,
                (*ids, limit),
            )
        ]
        deliveries = [
            dict(r) for r in conn.execute(
                f"""
                SELECT d.id, d.project_id, p.code AS project_code, d.title, d.kind,
                       d.status, d.reviewer, d.created_at, d.task_id
                FROM deliveries d JOIN agent_projects p ON p.id = d.project_id
                WHERE d.project_id IN ({marks}) AND d.status IN ('submitted', 'in_review')
                ORDER BY d.created_at DESC LIMIT ?
                """,
                (*ids, limit),
            )
        ]
        executions = [
            dict(r) for r in conn.execute(
                f"""
                SELECT e.id, e.project_id, p.code AS project_code, e.agent_id,
                       e.status, e.started_at, e.finished_at
                FROM executions e JOIN agent_projects p ON p.id = e.project_id
                WHERE e.project_id IN ({marks})
                ORDER BY e.started_at DESC LIMIT ?
                """,
                (*ids, limit),
            )
        ]
        return {
            "generated_at": _iso(_now()),
            "principal_id": pid,
            "projects": ids,
            "tasks": tasks,
            "approvals": approvals,
            "deliveries": deliveries,
            "executions": executions,
            "counts": {
                "tasks": len(tasks),
                "blocked_tasks": sum(1 for t in tasks if t["status"] == "blocked"),
                "approvals": len(approvals),
                "deliveries": len(deliveries),
                "executions": len(executions),
            },
        }
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 统一消息中心
# ---------------------------------------------------------------------------

def _notification_items(conn: sqlite3.Connection, ids: list[str],
                        limit: int) -> list[dict[str, Any]]:
    """把审批 / 交付 / 阻塞任务 / 关键事件汇总成统一消息。

    只做读取聚合，消息状态与业务状态分离：已读不代表业务状态变化。
    """
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    items: list[dict[str, Any]] = []

    for r in conn.execute(
        f"""
        SELECT a.id, a.project_id, p.code AS project_code, a.resource_type, a.resource_id,
               a.action, a.requested_by, a.created_at
        FROM approvals a JOIN agent_projects p ON p.id = a.project_id
        WHERE a.project_id IN ({marks}) AND a.status = 'pending'
        ORDER BY a.created_at DESC LIMIT ?
        """,
        (*ids, limit),
    ):
        items.append({
            "key": f"approval:{r['id']}",
            "type": "approval",
            "level": "important",
            "title": f"待审批：{r['resource_type']} {r['resource_id']} · {r['action']}",
            "project_id": r["project_id"],
            "project_code": r["project_code"],
            "resource_type": r["resource_type"],
            "resource_id": r["resource_id"],
            "created_at": r["created_at"],
            "link": f"#/p/{r['project_id']}/approvals",
        })

    for r in conn.execute(
        f"""
        SELECT d.id, d.project_id, p.code AS project_code, d.title, d.status, d.created_at
        FROM deliveries d JOIN agent_projects p ON p.id = d.project_id
        WHERE d.project_id IN ({marks}) AND d.status IN ('submitted', 'in_review')
        ORDER BY d.created_at DESC LIMIT ?
        """,
        (*ids, limit),
    ):
        items.append({
            "key": f"delivery:{r['id']}",
            "type": "delivery",
            "level": "normal",
            "title": f"交付待验收：{r['title']}",
            "project_id": r["project_id"],
            "project_code": r["project_code"],
            "resource_type": "delivery",
            "resource_id": r["id"],
            "created_at": r["created_at"],
            "link": f"#/p/{r['project_id']}/deliveries",
        })

    for r in conn.execute(
        f"""
        SELECT t.id, t.project_id, p.code AS project_code, t.title, t.updated_at
        FROM tasks t JOIN agent_projects p ON p.id = t.project_id
        WHERE t.project_id IN ({marks}) AND t.status = 'blocked'
        ORDER BY t.updated_at DESC LIMIT ?
        """,
        (*ids, limit),
    ):
        items.append({
            "key": f"task:{r['id']}",
            "type": "task",
            "level": "important",
            "title": f"任务阻塞：{r['title']}",
            "project_id": r["project_id"],
            "project_code": r["project_code"],
            "resource_type": "task",
            "resource_id": r["id"],
            "created_at": r["updated_at"],
            "link": f"#/p/{r['project_id']}/tasks",
        })

    for r in conn.execute(
        f"""
        SELECT e.id, e.project_id, p.code AS project_code, e.event_type, e.message, e.occurred_at
        FROM agent_events e JOIN agent_projects p ON p.id = e.project_id
        WHERE e.project_id IN ({marks}) AND e.ok = 0
        ORDER BY e.occurred_at DESC LIMIT ?
        """,
        (*ids, limit),
    ):
        items.append({
            "key": f"event:{r['id']}",
            "type": "execution",
            "level": "important",
            "title": f"执行异常：{r['event_type']} {r['message'] or ''}".strip(),
            "project_id": r["project_id"],
            "project_code": r["project_code"],
            "resource_type": "event",
            "resource_id": r["id"],
            "created_at": r["occurred_at"],
            "link": f"#/p/{r['project_id']}/executions",
        })

    items.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return items[:limit]


def notifications(conn: sqlite3.Connection | None = None,
                  principal_id: str | None = None,
                  status: str = "all",
                  project_id: str | None = None,
                  limit: int = 100) -> dict[str, Any]:
    own = conn is None
    conn = conn or store.connect()
    pid = principal_id or LOCAL_OWNER
    try:
        store.init_db(conn)
        ids = visible_project_ids(conn, pid)
        if project_id:
            ids = [i for i in ids if i == project_id]
        items = _notification_items(conn, ids, limit)
        states = {
            r["notification_key"]: r["state"]
            for r in conn.execute(
                "SELECT notification_key, state FROM notification_states WHERE principal_id = ?",
                (pid,),
            )
        }
        for item in items:
            item["state"] = states.get(item["key"], "unread")
            item["read"] = item["state"] != "unread"
        if status in {"unread", "read"}:
            want_read = status == "read"
            items = [i for i in items if i["read"] == want_read]
        return {
            "generated_at": _iso(_now()),
            "principal_id": pid,
            "count": len(items),
            "unread": sum(1 for i in items if not i["read"]),
            "items": items,
        }
    finally:
        if own:
            conn.close()


def mark_notifications(conn: sqlite3.Connection, principal_id: str | None,
                       keys: Iterable[str], state: str = "read") -> dict[str, Any]:
    """标记消息已读/未读。只写体验层状态，不触碰业务表。"""
    pid = principal_id or LOCAL_OWNER
    if state not in {"read", "unread"}:
        raise ValueError("state 只能是 read 或 unread")
    now = _iso(_now())
    touched = 0
    with store.write_tx(conn):
        for key in keys:
            if not key:
                continue
            conn.execute(
                "INSERT INTO notification_states (id, principal_id, notification_key, state, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(principal_id, notification_key)"
                " DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at",
                (store.stable_id(pid, key), pid, str(key), state, now),
            )
            touched += 1
    return {"ok": True, "updated": touched, "state": state}


# ---------------------------------------------------------------------------
# 全局搜索
# ---------------------------------------------------------------------------

SEARCH_TYPES = ("project", "task", "agent", "delivery", "workflow", "knowledge", "release")


def search(conn: sqlite3.Connection | None = None,
           q: str = "",
           types: Iterable[str] | None = None,
           project_id: str | None = None,
           limit: int = 30,
           principal_id: str | None = None) -> dict[str, Any]:
    own = conn is None
    conn = conn or store.connect()
    pid = principal_id or LOCAL_OWNER
    try:
        store.init_db(conn)
        term = (q or "").strip()
        if not term:
            return {"query": q, "count": 0, "results": [], "note": "请输入关键词"}
        ids = visible_project_ids(conn, pid)
        if project_id:
            ids = [i for i in ids if i == project_id]
        wanted = {t for t in (types or SEARCH_TYPES) if t in SEARCH_TYPES}
        like = f"%{term}%"
        results: list[dict[str, Any]] = []
        if not ids:
            return {"query": term, "count": 0, "results": [], "searched_projects": []}
        marks = ",".join("?" for _ in ids)

        def add(kind: str, rows: Iterable[sqlite3.Row], title_key: str,
                sub_key: str | None = None, link_prefix: str = "") -> None:
            for r in rows:
                results.append({
                    "type": kind,
                    "id": r["id"],
                    "project_id": r["project_id"],
                    "project_code": r["project_code"] if _has(r, "project_code") else None,
                    "title": r[title_key],
                    "subtitle": r[sub_key] if sub_key and _has(r, sub_key) else None,
                    "link": f"#/p/{r['project_id']}/{link_prefix}",
                })

        if "project" in wanted:
            for r in conn.execute(
                f"SELECT id, id AS project_id, code AS project_code, name AS title,"
                f" status AS subtitle FROM agent_projects WHERE id IN ({marks})"
                f" AND (name LIKE ? OR code LIKE ? OR COALESCE(description,'') LIKE ?) LIMIT ?",
                (*ids, like, like, like, limit),
            ):
                results.append({
                    "type": "project", "id": r["id"], "project_id": r["id"],
                    "project_code": r["code"] if _has(r, "code") else r["project_code"],
                    "title": r["title"], "subtitle": r["subtitle"], "link": f"#/p/{r['id']}",
                })

        if "task" in wanted:
            add("task", conn.execute(
                f"SELECT t.id, t.project_id, p.code AS project_code, t.title, t.status AS subtitle"
                f" FROM tasks t JOIN agent_projects p ON p.id = t.project_id"
                f" WHERE t.project_id IN ({marks}) AND (t.title LIKE ? OR t.task_key LIKE ?"
                f" OR COALESCE(t.description,'') LIKE ?) ORDER BY t.updated_at DESC LIMIT ?",
                (*ids, like, like, like, limit),
            ), "title", "subtitle", "tasks")

        if "agent" in wanted:
            add("agent", conn.execute(
                f"SELECT a.id, a.project_id, p.code AS project_code, a.role_key AS title,"
                f" a.status AS subtitle FROM project_agents a JOIN agent_projects p ON p.id = a.project_id"
                f" WHERE a.project_id IN ({marks}) AND (a.role_key LIKE ? OR a.instance_key LIKE ?)"
                f" LIMIT ?",
                (*ids, like, like, limit),
            ), "title", "subtitle", "agents")

        if "delivery" in wanted:
            add("delivery", conn.execute(
                f"SELECT d.id, d.project_id, p.code AS project_code, d.title, d.status AS subtitle"
                f" FROM deliveries d JOIN agent_projects p ON p.id = d.project_id"
                f" WHERE d.project_id IN ({marks}) AND d.title LIKE ?"
                f" ORDER BY d.created_at DESC LIMIT ?",
                (*ids, like, limit),
            ), "title", "subtitle", "deliveries")

        if "workflow" in wanted:
            try:
                add("workflow", conn.execute(
                    "SELECT w.id, w.project_id, p.code AS project_code, w.name AS title,"
                    " w.status AS subtitle FROM p4_workflow_definitions w"
                    " JOIN agent_projects p ON p.id = w.project_id"
                    f" WHERE w.project_id IN ({marks}) AND w.name LIKE ? LIMIT ?",
                    (*ids, like, limit),
                ), "title", "subtitle", "workflows")
            except sqlite3.OperationalError:
                pass

        if "knowledge" in wanted:
            try:
                add("knowledge", conn.execute(
                    "SELECT k.id, k.project_id, p.code AS project_code, k.title,"
                    " k.status AS subtitle FROM p4_knowledge_items k"
                    " JOIN agent_projects p ON p.id = k.project_id"
                    f" WHERE k.project_id IN ({marks}) AND k.title LIKE ? LIMIT ?",
                    (*ids, like, limit),
                ), "title", "subtitle", "knowledge")
            except sqlite3.OperationalError:
                pass

        if "release" in wanted:
            try:
                add("release", conn.execute(
                    "SELECT r.id, r.project_id, p.code AS project_code, r.name AS title,"
                    " r.status AS subtitle FROM p4_releases r"
                    " JOIN agent_projects p ON p.id = r.project_id"
                    f" WHERE r.project_id IN ({marks}) AND r.name LIKE ? LIMIT ?",
                    (*ids, like, limit),
                ), "title", "subtitle", "releases")
            except sqlite3.OperationalError:
                pass

        return {
            "query": term,
            "types": sorted(wanted),
            "searched_projects": ids,
            "count": len(results),
            "results": results[:limit],
        }
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 个人保存视图（只存筛选条件，不复制任何业务事实）
# ---------------------------------------------------------------------------
VIEW_SCOPES = ("portfolio", "deliveries", "tasks")


class ViewError(Exception):
    """保存视图的参数错误；HTTP 层按 code/status 映射。"""

    def __init__(self, message: str, code: str = "invalid_payload", status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message


def _view_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["filters"] = store.loads_json(item.pop("filters_json", "{}"), {})
    return item


def list_views(conn: sqlite3.Connection | None = None,
               principal_id: str | None = None,
               scope: str | None = None) -> dict[str, Any]:
    own = conn is None
    conn = conn or store.connect()
    pid = principal_id or LOCAL_OWNER
    try:
        store.init_db(conn)
        if scope and scope not in VIEW_SCOPES:
            raise ViewError("scope 只能是 " + "/".join(VIEW_SCOPES))
        where = ["principal_id = ?"]
        params: list[Any] = [pid]
        if scope:
            where.append("scope = ?")
            params.append(scope)
        rows = conn.execute(
            "SELECT id, scope, name, filters_json, created_at, updated_at FROM saved_views"
            f" WHERE {' AND '.join(where)} ORDER BY updated_at DESC, name ASC",
            tuple(params),
        ).fetchall()
        return {"items": [_view_dict(row) for row in rows], "total": len(rows), "scope": scope}
    finally:
        if own:
            conn.close()


def save_view(conn: sqlite3.Connection, principal_id: str | None,
              payload: dict[str, Any]) -> dict[str, Any]:
    """同主体 + 同 scope + 同名视为更新，不产生重复视图。"""
    pid = principal_id or LOCAL_OWNER
    if not isinstance(payload, dict):
        raise ViewError("payload 必须是对象")
    scope = str(payload.get("scope") or "").strip()
    name = str(payload.get("name") or "").strip()
    filters = payload.get("filters")
    if scope not in VIEW_SCOPES:
        raise ViewError("scope 只能是 " + "/".join(VIEW_SCOPES))
    if not name or len(name) > 60:
        raise ViewError("name 必填且不超过 60 字")
    if not isinstance(filters, dict):
        raise ViewError("filters 必须是对象")
    # 只保留扁平标量，避免把任意结构塞进个人偏好
    clean = {
        str(key)[:40]: str(value)[:80]
        for key, value in filters.items()
        if isinstance(value, (str, int, float, bool)) and str(value) != ""
    }
    now = _iso(_now())
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO saved_views (id, principal_id, scope, name, filters_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(principal_id, scope, name) DO UPDATE SET"
            " filters_json = excluded.filters_json, updated_at = excluded.updated_at",
            (store.stable_id("view", pid, scope, name), pid, scope, name,
             store.dumps_json(clean), now, now),
        )
    row = conn.execute(
        "SELECT id, scope, name, filters_json, created_at, updated_at FROM saved_views"
        " WHERE principal_id = ? AND scope = ? AND name = ?",
        (pid, scope, name),
    ).fetchone()
    return {"view": _view_dict(row)}


def delete_view(conn: sqlite3.Connection, principal_id: str | None, view_id: str) -> dict[str, Any]:
    pid = principal_id or LOCAL_OWNER
    with store.write_tx(conn):
        cur = conn.execute(
            "DELETE FROM saved_views WHERE id = ? AND principal_id = ?", (view_id, pid),
        )
    if cur.rowcount == 0:
        raise ViewError("视图不存在或无权删除", code="not_found", status=404)
    return {"ok": True, "deleted": view_id}
