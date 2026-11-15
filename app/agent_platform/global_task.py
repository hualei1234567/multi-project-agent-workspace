"""Global Task 统一任务视图（P3-M5）。

契约背景：``docs/phase3/P3_范围决策记录.md`` Q5 —— 跨项目委派暂不做，
本里程碑只保留 Global Task 实体 + Project Task Link + 跨项目统一视图。

**为什么不能复用 ``tasks`` 表**（``P3_API_SCHEMA_EVENT_REUSE_MAP.md`` §4）：
``tasks.project_id`` 是 ``NOT NULL``，而 Global Task 天生跨项目；放宽那个约束
会破坏项目隔离语义（35 项隔离测试与全部既有查询都依赖它）。所以独立建模：

* ``global_tasks`` —— 只有「这件事跨项目叫什么、谁负责、做没做完」；
* ``global_task_links`` —— 把各项目里的普通 ``tasks`` 挂上来。

**不复制状态**：全局状态由 :func:`rollup_status` 现算，读的是各项目 tasks 的
实时状态，不写回、不缓存，避免出现两份状态互相打脸。两条硬规则：

1. 没有任何 link 时返回 ``open`` 且 ``no_links: true``——不假装完成；
2. 人工 ``cancelled`` 覆盖派生结果；其余（含人工 done）一律以派生为准，
   因为"有人在项目里把子任务退回 in_progress"时，全局还显示 done 就是撒谎。
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from typing import Any

from . import store

#: Global Task 状态。cancelled 是唯一由人工覆盖派生结果的状态。
STATUSES = frozenset({"open", "in_progress", "done", "cancelled"})

#: 与 tasks 表一致的优先级集合。
PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})

#: link 角色：实现 / 验证 / 阻塞（计划中的下游工作）。
LINK_ROLES = frozenset({"implements", "verifies", "blocks"})

#: project task 视为「已完成」的唯一状态。
_TASK_DONE = "done"
#: project task 视为「尚未开始」的状态；其它（in_progress / review / blocked /
#: 未知状态）一律算已开始——未知不等于没干活，宁可显示进行中。
_TASK_TODO = "todo"

GLOBAL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


class GlobalTaskError(Exception):
    """稳定错误码；HTTP 层映射为 ``{ok:false,error:code}`` + status。"""

    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id() -> str:
    return f"gt_{uuid.uuid4().hex[:16]}"


def _now() -> str:
    return store.now_iso()


def _audit(conn: sqlite3.Connection, *, action: str, resource_id: str,
           project_id: str | None, actor: str, detail: dict[str, Any]) -> None:
    """只追加审计；与业务写入同事务。失败不阻断业务（尽力而为）。"""
    try:
        conn.execute(
            "INSERT INTO audit_logs (id, project_id, actor, action, resource_type,"
            " resource_id, result, detail_json, created_at)"
            " VALUES (?, ?, ?, ?, 'global_task', ?, 'ok', ?, ?)",
            (uuid.uuid4().hex, project_id, actor, action, resource_id,
             store.dumps_json(detail), _now()),
        )
    except sqlite3.DatabaseError:  # pragma: no cover - 审计不可写不阻断业务
        pass


# ---------------------------------------------------------------------------
# 读取辅助
# ---------------------------------------------------------------------------
def _global_task_row(conn: sqlite3.Connection, global_task_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM global_tasks WHERE id = ? OR global_key = ?",
        (global_task_id, global_task_id),
    ).fetchone()
    if row is None:
        raise GlobalTaskError("not_found", 404, "Global Task 不存在")
    return row


def linked_tasks(conn: sqlite3.Connection, global_task_id: str) -> list[dict[str, Any]]:
    """某个 Global Task 关联的全部项目任务（含项目名与当前状态）。"""
    rows = conn.execute(
        """
        SELECT l.id AS link_id, l.project_id, l.task_id, l.link_role, l.created_at AS linked_at,
               t.task_key, t.title, t.status AS task_status, t.priority AS task_priority,
               t.assignee_agent_id,
               p.code AS project_code, p.name AS project_name
          FROM global_task_links l
          JOIN tasks t ON t.id = l.task_id
          JOIN agent_projects p ON p.id = l.project_id
         WHERE l.global_task_id = ?
         ORDER BY p.name, t.task_key
        """,
        (global_task_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def derive_status(task_statuses: list[str]) -> tuple[str, bool]:
    """(派生状态, 是否无 link)。不做任何美化。

    * 没有 link            → ("open", True)   不假装完成
    * 全部 done            → "done"
    * 全部 todo            → "open"
    * 其余（部分 done / 任一 in_progress / review / blocked / 未知）→ "in_progress"
      未知状态按已开始算：宁可显示进行中，也不能把在干的事显示成未开始。
    """
    if not task_statuses:
        return "open", True
    if all(status == _TASK_DONE for status in task_statuses):
        return "done", False
    if all(status == _TASK_TODO for status in task_statuses):
        return "open", False
    return "in_progress", False


def _task_counts(task_statuses: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in task_statuses:
        counts[status] = counts.get(status, 0) + 1
    return counts


def _effective(stored_status: str, derived_status: str) -> str:
    """cancelled 是人工覆盖；其余以派生为准（派生不会撒谎）。"""
    return "cancelled" if stored_status == "cancelled" else derived_status


def _summarize(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    tasks = linked_tasks(conn, row["id"])
    statuses = [item["task_status"] for item in tasks]
    derived, no_links = derive_status(statuses)
    return {
        "id": row["id"],
        "global_key": row["global_key"],
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "effective_status": _effective(row["status"], derived),
        "rollup_status": derived,
        "no_links": no_links,
        "priority": row["priority"],
        "owner_principal_id": row["owner_principal_id"],
        "link_count": len(tasks),
        "task_counts": _task_counts(statuses),
        "projects": sorted({item["project_id"] for item in tasks}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version": row["version"],
    }


# ---------------------------------------------------------------------------
# Global Task CRUD
# ---------------------------------------------------------------------------
def create_global_task(
    conn: sqlite3.Connection,
    *,
    global_key: str,
    title: str,
    description: str | None = None,
    priority: str = "P2",
    owner_principal_id: str | None = None,
    created_by: str = "local",
) -> dict[str, Any]:
    key = str(global_key or "").strip()
    if not key:
        raise GlobalTaskError("invalid_payload", 422, "global_key 必填")
    if not GLOBAL_KEY_RE.match(key):
        raise GlobalTaskError("invalid_payload", 422,
                              "global_key 非法（字母数字开头，2-40 位，可含 _-）")
    name = str(title or "").strip()
    if not name:
        raise GlobalTaskError("invalid_payload", 422, "title 必填")
    pri = str(priority or "P2").upper()
    if pri not in PRIORITIES:
        raise GlobalTaskError("invalid_payload", 422, "priority 必须是 P0-P3")

    now = _now()
    gid = _new_id()
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO global_tasks (id, global_key, title, description, status,"
                " priority, owner_principal_id, created_at, updated_at, version)"
                " VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, 1)",
                (gid, key, name,
                 (str(description).strip() or None) if description else None,
                 pri, owner_principal_id, now, now),
            )
            _audit(conn, action="global_task.create", resource_id=gid, project_id=None,
                   actor=created_by, detail={"global_key": key, "title": name})
    except sqlite3.IntegrityError:
        raise GlobalTaskError("conflict", 409, "global_key 已存在") from None
    return get_global_task(conn, gid)


def get_global_task(conn: sqlite3.Connection, global_task_id: str) -> dict[str, Any]:
    row = _global_task_row(conn, global_task_id)
    item = _summarize(conn, row)
    item["links"] = linked_tasks(conn, row["id"])
    return item


def list_global_tasks(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    priority: str | None = None,
    project_id: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    """列表：每条都带 link 数与聚合状态。

    ``status`` 过滤按**生效状态**（cancelled 覆盖 / 否则派生）判定，
    而不是只看那一列，否则「已做完但列里还写着 open」会查不出来。
    """
    if status and status not in STATUSES:
        raise GlobalTaskError("invalid_payload", 422, f"status 必须是 {sorted(STATUSES)}")
    if priority and priority.upper() not in PRIORITIES:
        raise GlobalTaskError("invalid_payload", 422, "priority 必须是 P0-P3")

    sql = "SELECT * FROM global_tasks WHERE 1=1"
    args: list[Any] = []
    if priority:
        sql += " AND priority = ?"
        args.append(priority.upper())
    if q:
        sql += " AND (global_key LIKE ? OR title LIKE ? OR description LIKE ?)"
        like = f"%{str(q)[:80]}%"
        args.extend([like, like, like])
    sql += " ORDER BY created_at DESC, global_key"
    items = [_summarize(conn, row) for row in conn.execute(sql, args).fetchall()]

    if project_id:
        items = [item for item in items if project_id in item["projects"]]
    if status:
        items = [item for item in items if item["effective_status"] == status]
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# Project Task Link
# ---------------------------------------------------------------------------
def link_task(
    conn: sqlite3.Connection,
    global_task_id: str,
    project_id: str,
    task_id: str,
    link_role: str = "implements",
    *,
    linked_by: str = "local",
) -> dict[str, Any]:
    """把某项目里的 task 挂到 Global Task 上。

    **必须校验归属**：``task_id`` 支持 id 或 task_key，但它必须属于 ``project_id``。
    任务存在却属于别的项目时返回 422 ``task_project_mismatch``——
    不伪装成 404，否则「挂错项目」会变成静默的幻觉。
    """
    role = str(link_role or "implements").strip()
    if role not in LINK_ROLES:
        raise GlobalTaskError("invalid_payload", 422, f"link_role 必须是 {sorted(LINK_ROLES)}")
    if not project_id:
        raise GlobalTaskError("invalid_payload", 422, "project_id 必填")
    if not task_id:
        raise GlobalTaskError("invalid_payload", 422, "task_id 必填")

    global_row = _global_task_row(conn, global_task_id)

    project = conn.execute(
        "SELECT id FROM agent_projects WHERE id = ?", (project_id,)
    ).fetchone()
    if project is None:
        raise GlobalTaskError("project_not_found", 404, "项目不存在")

    task = conn.execute(
        "SELECT id, project_id FROM tasks WHERE id = ? OR task_key = ?",
        (task_id, task_id),
    ).fetchone()
    if task is None:
        raise GlobalTaskError("task_not_found", 404, "任务不存在")
    if task["project_id"] != project_id:
        raise GlobalTaskError(
            "task_project_mismatch", 422,
            f"任务属于项目 {task['project_id']}，不能挂到项目 {project_id}",
        )

    now = _now()
    link_id = f"gtl_{uuid.uuid4().hex[:16]}"
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO global_task_links (id, global_task_id, project_id, task_id,"
                " link_role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (link_id, global_row["id"], project_id, task["id"], role, now),
            )
            conn.execute(
                "UPDATE global_tasks SET updated_at = ?, version = version + 1 WHERE id = ?",
                (now, global_row["id"]),
            )
            _audit(conn, action="global_task.link", resource_id=global_row["id"],
                   project_id=project_id, actor=linked_by,
                   detail={"link_id": link_id, "task_id": task["id"], "link_role": role})
    except sqlite3.IntegrityError:
        raise GlobalTaskError("conflict", 409, "该任务已挂在此 Global Task 下") from None
    return get_global_task(conn, global_row["id"])


def link_project(
    conn: sqlite3.Connection, link_id: str, *, global_task_id: str | None = None
) -> str | None:
    """link 属于哪个项目；不存在（或不属于给定 Global Task）时返回 None。

    HTTP 层在删除前用它做项目级授权——不能凭一个 link_id 就删掉别的项目的关联。
    """
    if global_task_id:
        row = conn.execute(
            "SELECT project_id FROM global_task_links WHERE id = ? AND global_task_id = ?",
            (link_id, global_task_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT project_id FROM global_task_links WHERE id = ?", (link_id,)
        ).fetchone()
    return row["project_id"] if row else None


def unlink_task(
    conn: sqlite3.Connection,
    link_id: str,
    *,
    global_task_id: str | None = None,
    unlinked_by: str = "local",
) -> dict[str, Any]:
    if global_task_id:
        row = conn.execute(
            "SELECT * FROM global_task_links WHERE id = ? AND global_task_id = ?",
            (link_id, global_task_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM global_task_links WHERE id = ?", (link_id,)
        ).fetchone()
    if row is None:
        raise GlobalTaskError("not_found", 404, "关联不存在")

    now = _now()
    with store.write_tx(conn):
        conn.execute("DELETE FROM global_task_links WHERE id = ?", (link_id,))
        conn.execute(
            "UPDATE global_tasks SET updated_at = ?, version = version + 1 WHERE id = ?",
            (now, row["global_task_id"]),
        )
        _audit(conn, action="global_task.unlink", resource_id=row["global_task_id"],
               project_id=row["project_id"], actor=unlinked_by,
               detail={"link_id": link_id, "task_id": row["task_id"]})
    return {"unlinked": link_id, "global_task_id": row["global_task_id"],
            "project_id": row["project_id"], "task_id": row["task_id"]}


def rollup_status(conn: sqlite3.Connection, global_task_id: str) -> dict[str, Any]:
    """聚合状态：全部 done → done；任一已开始 → in_progress；全 todo → open。

    没有 link 时返回 ``open`` + ``no_links: true``，**不假装完成**。
    cancelled 是人工覆盖，优先于派生。
    """
    row = _global_task_row(conn, global_task_id)
    tasks = linked_tasks(conn, row["id"])
    statuses = [item["task_status"] for item in tasks]
    derived, no_links = derive_status(statuses)
    return {
        "global_task_id": row["id"],
        "global_key": row["global_key"],
        "stored_status": row["status"],
        "status": _effective(row["status"], derived),
        "derived_status": derived,
        "no_links": no_links,
        "link_count": len(tasks),
        "task_counts": _task_counts(statuses),
        "projects": sorted({item["project_id"] for item in tasks}),
    }


# ---------------------------------------------------------------------------
# 跨项目统一视图
# ---------------------------------------------------------------------------
def global_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    """跨项目统一视图：项目分组 + 统计。

    只读聚合，不新增状态。没有 link 的 Global Task 单独计数，
    让「建了但没人接」暴露出来，而不是混进 done 里。
    """
    items = [_summarize(conn, row) for row in conn.execute(
        "SELECT * FROM global_tasks ORDER BY created_at DESC, global_key"
    ).fetchall()]
    by_status: dict[str, int] = {}
    for item in items:
        by_status[item["effective_status"]] = by_status.get(item["effective_status"], 0) + 1

    project_rows = conn.execute(
        "SELECT id, code, name, status FROM agent_projects ORDER BY name"
    ).fetchall()
    projects: list[dict[str, Any]] = []
    for project in project_rows:
        links = conn.execute(
            """
            SELECT l.id AS link_id, l.global_task_id, l.link_role, l.task_id,
                   t.task_key, t.title, t.status AS task_status,
                   g.global_key, g.title AS global_title, g.status AS global_status
              FROM global_task_links l
              JOIN tasks t ON t.id = l.task_id
              JOIN global_tasks g ON g.id = l.global_task_id
             WHERE l.project_id = ?
             ORDER BY g.global_key, t.task_key
            """,
            (project["id"],),
        ).fetchall()
        task_counts: dict[str, int] = {}
        for link in links:
            task_counts[link["task_status"]] = task_counts.get(link["task_status"], 0) + 1
        projects.append({
            "project_id": project["id"],
            "project_code": project["code"],
            "project_name": project["name"],
            "project_status": project["status"],
            "link_count": len(links),
            "global_task_count": len({link["global_task_id"] for link in links}),
            "task_status": task_counts,
            "links": [dict(link) for link in links],
        })

    return {
        "generated_at": _now(),
        "totals": {
            "global_tasks": len(items),
            "by_status": by_status,
            "links": sum(item["link_count"] for item in items),
            "global_tasks_without_links": sum(1 for item in items if item["no_links"]),
            "projects_with_links": sum(1 for p in projects if p["link_count"]),
            "projects": len(projects),
        },
        "global_tasks": items,
        "projects": projects,
    }
