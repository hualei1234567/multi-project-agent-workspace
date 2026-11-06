"""Workflow 域 V1：定义 / 版本 / 静态校验 / 沙箱测试 / 发布 / 项目绑定（P4-M3）。

边界（V6.0 §9.5、P4-WF-008）：

* 这里只描述"该做什么、先后顺序、哪里要人审"，**不执行**；
* 真正执行由绑定后生成的第三期 ``tasks`` 交给 ``scheduler`` 完成；
* 不定义新的执行状态机，任务状态一律沿用第三期。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .. import store
from . import events

NODE_TYPES = ("task", "agent", "human_gate", "quality_gate")


class WorkflowError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------
# 静态校验
# --------------------------------------------------------------------------
def validate_dsl(dsl: Any) -> list[str]:
    """返回错误列表；空列表表示通过。"""
    errors: list[str] = []
    if not isinstance(dsl, dict):
        return ["dsl 必须是对象"]
    nodes = dsl.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return ["nodes 不能为空"]

    keys: list[str] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append(f"节点 #{index} 不是对象")
            continue
        key = str(node.get("key") or "").strip()
        if not key:
            errors.append(f"节点 #{index} 缺少 key")
            continue
        if key in keys:
            errors.append(f"节点 key 重复：{key}")
        keys.append(key)
        node_type = node.get("type") or "task"
        if node_type not in NODE_TYPES:
            errors.append(f"节点 {key} 类型非法：{node_type}")
        if not str(node.get("title") or "").strip():
            errors.append(f"节点 {key} 缺少 title")
        deps = node.get("depends_on") or []
        if not isinstance(deps, list):
            errors.append(f"节点 {key} 的 depends_on 必须是数组")

    key_set = set(keys)
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for dep in node.get("depends_on") or []:
            if dep not in key_set:
                errors.append(f"节点 {node.get('key')} 依赖了不存在的节点：{dep}")

    if errors:
        return errors

    # 环检测 + 孤立节点检测（无依赖且没有任何后继）
    graph = {node["key"]: list(node.get("depends_on") or []) for node in nodes}
    successors: dict[str, set[str]] = {key: set() for key in graph}
    for key, deps in graph.items():
        for dep in deps:
            successors[dep].add(key)

    state: dict[str, int] = {}

    def _visit(key: str) -> bool:
        state[key] = 1
        for dep in graph.get(key, []):
            if state.get(dep) == 1:
                return False
            if state.get(dep, 0) == 0 and not _visit(dep):
                return False
        state[key] = 2
        return True

    for key in graph:
        if state.get(key, 0) == 0 and not _visit(key):
            errors.append(f"DAG 存在环，涉及节点：{key}")
            break

    if len(graph) > 1:
        for key, deps in graph.items():
            if not deps and not successors[key]:
                errors.append(f"节点 {key} 是孤立节点（无依赖也无后继）")
    return errors


# --------------------------------------------------------------------------
# 定义与版本
# --------------------------------------------------------------------------
def create_definition(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = str(payload.get("key") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not key or not name:
        raise WorkflowError("invalid_payload", 422, "key 与 name 必填")
    now = store.now_iso()
    def_id = _new_id("wf")
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO workflow_definitions (id, project_id, def_key, name,"
                " description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (def_id, project_id, key, name, payload.get("description"), now, now),
            )
    except sqlite3.IntegrityError as exc:
        raise WorkflowError("conflict", 409, "同项目下 workflow key 已存在") from exc
    return get_definition(conn, def_id)


def get_definition(conn: sqlite3.Connection, definition_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM workflow_definitions WHERE id = ?", (definition_id,)
    ).fetchone()
    if row is None:
        raise WorkflowError("not_found", 404, "Workflow 定义不存在")
    return dict(row)


def list_definitions(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT d.*, (SELECT MAX(version) FROM workflow_versions v WHERE v.definition_id = d.id)"
        " AS latest_version FROM workflow_definitions d WHERE d.project_id = ?"
        " ORDER BY d.created_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


def create_version(conn: sqlite3.Connection, definition_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    definition = get_definition(conn, definition_id)
    dsl = payload.get("dsl")
    errors = validate_dsl(dsl)
    if errors:
        raise WorkflowError("invalid_dsl", 422, "；".join(errors))

    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM workflow_versions WHERE definition_id = ?",
        (definition_id,),
    ).fetchone()
    version_no = int(row["v"]) + 1
    version_id = _new_id("wfv")
    dsl_text = json.dumps(dsl, ensure_ascii=False, sort_keys=True)
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO workflow_versions (id, definition_id, version, dsl_json, digest,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, 'draft', ?)",
            (version_id, definition_id, version_no, dsl_text,
             store.stable_id(definition["def_key"], version_no, dsl_text), store.now_iso()),
        )
    return get_version(conn, version_id)


def get_version(conn: sqlite3.Connection, version_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM workflow_versions WHERE id = ?", (version_id,)
    ).fetchone()
    if row is None:
        raise WorkflowError("not_found", 404, "Workflow 版本不存在")
    version = dict(row)
    version["dsl"] = store.loads_json(version.pop("dsl_json"), {})
    return version


def test_version(conn: sqlite3.Connection, version_id: str) -> dict[str, Any]:
    """沙箱测试：只做静态校验与可执行性预检，不创建任何任务。"""
    version = get_version(conn, version_id)
    errors = validate_dsl(version["dsl"])
    nodes = version["dsl"].get("nodes") or []
    result = {
        "valid": not errors,
        "errors": errors,
        "node_count": len(nodes),
        "human_gates": [n["key"] for n in nodes if n.get("type") == "human_gate"],
        "quality_gates": [n["key"] for n in nodes if n.get("type") == "quality_gate"],
    }
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO workflow_test_runs (id, version_id, status, result_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (_new_id("wft"), version_id, "passed" if not errors else "failed",
             store.dumps_json(result), store.now_iso()),
        )
        conn.execute(
            "UPDATE workflow_versions SET status = ? WHERE id = ? AND status = 'draft'",
            ("tested" if not errors else "draft", version_id),
        )
    return result


def publish_version(conn: sqlite3.Connection, version_id: str, *, published_by: str) -> dict[str, Any]:
    version = get_version(conn, version_id)
    if version["status"] not in {"tested", "draft"}:
        raise WorkflowError("invalid_state", 409, f"当前状态 {version['status']} 不能发布")
    errors = validate_dsl(version["dsl"])
    if errors:
        raise WorkflowError("invalid_dsl", 422, "；".join(errors))

    definition = get_definition(conn, version["definition_id"])
    with store.write_tx(conn):
        conn.execute(
            "UPDATE workflow_versions SET status = 'published', published_at = ? WHERE id = ?",
            (store.now_iso(), version_id),
        )
        conn.execute(
            "UPDATE workflow_definitions SET status = 'active', updated_at = ? WHERE id = ?",
            (store.now_iso(), definition["id"]),
        )
        events.emit(conn, "workflow.version.published", project_id=definition["project_id"],
                    resource_type="workflow_version", resource_id=version_id,
                    payload={"version": version["version"], "by": published_by})
    return {"version_id": version_id, "status": "published"}


# --------------------------------------------------------------------------
# 绑定与物化
# --------------------------------------------------------------------------
def bind(conn: sqlite3.Connection, project_id: str, version_id: str, *,
         materialize: bool = True, created_by: str = "local") -> dict[str, Any]:
    """把已发布版本绑定到项目，并物化成第三期任务（可选）。"""
    version = get_version(conn, version_id)
    if version["status"] != "published":
        raise WorkflowError("invalid_state", 409, "只能绑定已发布版本")
    definition = get_definition(conn, version["definition_id"])
    if definition["project_id"] != project_id:
        raise WorkflowError("forbidden", 403, "该 Workflow 不属于本项目")

    binding_id = _new_id("wfb")
    now = store.now_iso()
    created: list[str] = []
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO workflow_bindings (id, project_id, version_id, status,"
            " materialized, created_at) VALUES (?, ?, ?, 'enabled', 0, ?)"
            " ON CONFLICT(project_id, version_id) DO UPDATE SET status = 'enabled'",
            (binding_id, project_id, version_id, now),
        )
        if materialize:
            created = _materialize(conn, project_id, definition, version, created_by, now)
            conn.execute(
                "UPDATE workflow_bindings SET materialized = ? WHERE project_id = ?"
                " AND version_id = ?", (len(created), project_id, version_id),
            )
        events.emit(conn, "workflow.binding.enabled", project_id=project_id,
                    resource_type="workflow_binding", resource_id=binding_id,
                    payload={"version_id": version_id, "tasks": len(created)})
    return {"binding_id": binding_id, "version_id": version_id,
            "tasks_created": created}


def _materialize(conn, project_id: str, definition: dict[str, Any],
                 version: dict[str, Any], created_by: str, now: str) -> list[str]:
    """把 DAG 节点写进第三期 tasks，边写进 task_dependencies。"""
    nodes = version["dsl"].get("nodes") or []
    task_ids: dict[str, str] = {}
    created: list[str] = []

    for node in nodes:
        key = node["key"]
        task_key = f"wf:{definition['def_key']}:{key}"
        existing = conn.execute(
            "SELECT id FROM tasks WHERE project_id = ? AND task_key = ?",
            (project_id, task_key),
        ).fetchone()
        if existing is not None:
            task_ids[key] = existing["id"]
            continue
        task_id = _new_id("task")
        conn.execute(
            "INSERT INTO tasks (id, project_id, task_key, title, status, priority,"
            " source, updated_at, description, created_by)"
            " VALUES (?, ?, ?, ?, 'todo', ?, 'workflow', ?, ?, ?)",
            (task_id, project_id, task_key, node["title"],
             str(node.get("priority") or "P2"), now,
             node.get("description") or f"来自 workflow {definition['def_key']} v{version['version']}",
             created_by),
        )
        task_ids[key] = task_id
        created.append(task_id)

    for node in nodes:
        for dep in node.get("depends_on") or []:
            parent = task_ids.get(dep)
            child = task_ids.get(node["key"])
            if not parent or not child:
                continue
            exists = conn.execute(
                "SELECT 1 FROM task_dependencies WHERE task_id = ? AND depends_on_task_id = ?",
                (child, parent),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO task_dependencies (id, project_id, task_id,"
                " depends_on_task_id, dependency_type, cross_project, created_at)"
                " VALUES (?, ?, ?, ?, 'blocks', 0, ?)",
                (_new_id("dep"), project_id, child, parent, now),
            )
    return created


def list_bindings(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT b.*, d.def_key, v.version FROM workflow_bindings b"
        " JOIN workflow_versions v ON v.id = b.version_id"
        " JOIN workflow_definitions d ON d.id = v.definition_id"
        " WHERE b.project_id = ? ORDER BY b.created_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}
