"""Workflow 域 V1：定义 / 版本 / 静态校验 / 沙箱测试 / 发布 / 项目绑定（P4-M3）。

边界（V6.0 §9.5、P4-WF-008）：

* 这里只描述"该做什么、先后顺序、哪里要人审"，**不执行**；
* 真正执行由绑定后生成的第三期 ``tasks`` 交给 ``scheduler`` 完成；
* 不定义新的执行状态机，任务状态一律沿用第三期。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from typing import Any

from .. import store
from . import audited, events

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


@audited("workflow.version.publish", "workflow_version", id_arg="version_id")
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
        # 节点可以声明 role（平台角色 key）。不指派角色的任务调度器不会调度，
        # 所以这里按 role 解析出项目里的 agent 实例并写进 assignee_agent_id。
        role_key = str(node.get("role") or "").strip()
        agent_id = None
        if role_key:
            agent = conn.execute(
                "SELECT id FROM project_agents WHERE project_id = ? AND role_key = ?"
                " ORDER BY instance_key LIMIT 1", (project_id, role_key),
            ).fetchone()
            if agent is not None:
                agent_id = agent["id"]
        conn.execute(
            "INSERT INTO tasks (id, project_id, task_key, title, status, priority,"
            " source, updated_at, description, created_by, assignee_agent_id)"
            " VALUES (?, ?, ?, ?, 'todo', ?, 'workflow', ?, ?, ?, ?)",
            (task_id, project_id, task_key, node["title"],
             str(node.get("priority") or "P2"), now,
             node.get("description") or f"来自 workflow {definition['def_key']} v{version['version']}",
             created_by, agent_id),
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

# --------------------------------------------------------------------------
# 运行实例与 Human Gate 真阻断（R3 / M3）
# --------------------------------------------------------------------------
#: 运行时状态。
RUN_STATUSES = (
    "running", "waiting_approval", "completed", "failed", "cancelled",
)
NODE_STATUSES = (
    "pending", "running", "waiting_approval", "approved", "rejected",
    "completed", "failed", "skipped",
)

#: 阻塞型节点：没有对应条件就不能进入下游。
BLOCKING_NODE_TYPES = frozenset({"human_gate", "quality_gate"})


def _audit(conn: sqlite3.Connection, project_id: str, action: str,
           resource_id: str, detail: dict[str, Any], *,
           actor: str = "local") -> None:
    """写审计；失败不阻断流程（与 service.audit 语义一致）。"""
    try:
        from .. import service

        service.audit(conn, project_id, action, "workflow_run", resource_id,
                      actor=actor, detail=detail)
    except Exception as exc:  # noqa: BLE001 - 审计不可用不该让闸门失效
        print(f"[p4.workflow] 审计写入失败 {action}: {exc}", file=sys.stderr)


def _digest_inputs(payload: Any) -> str:
    """把节点输入规范化后取摘要：审批绑的是这个值，输入一变审批自动失效。"""
    import hashlib

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def start_run(conn: sqlite3.Connection, project_id: str, binding_id: str, *,
              inputs: dict[str, Any] | None = None,
              correlation_id: str | None = None) -> dict[str, Any]:
    """启动一次 workflow 运行，把节点铺成 pending，并推进到第一个可执行点。

    **不执行任何业务动作**：本域只维护状态机与审批闸门。真正干活的是被物化出来的
    ``tasks``，由第三期 scheduler 执行。这里负责的是「能不能往下走」。
    """
    binding = conn.execute(
        "SELECT * FROM workflow_bindings WHERE id = ?", (binding_id,)
    ).fetchone()
    if binding is None:
        raise WorkflowError("not_found", 404, "绑定不存在")
    if binding["project_id"] != project_id:
        raise WorkflowError("forbidden", 403, "绑定不属于该项目")
    if binding["status"] != "enabled":
        raise WorkflowError("invalid_state", 409, f"绑定状态 {binding['status']} 不可运行")

    version = get_version(conn, binding["version_id"])
    nodes = version["dsl"].get("nodes") or []
    if not nodes:
        raise WorkflowError("invalid_state", 409, "workflow 没有节点")

    run_id = _new_id("wfr")
    now = store.now_iso()
    digest = _digest_inputs(inputs or {})
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO workflow_runs (id, project_id, binding_id, version_id, status,"
            " current_node, input_digest, correlation_id, started_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'running', NULL, ?, ?, ?, ?, ?)",
            (run_id, project_id, binding_id, binding["version_id"], digest,
             correlation_id or _new_id("corr"), now, now, now),
        )
        for node in nodes:
            key = str(node.get("key") or "").strip()
            if not key:
                continue
            conn.execute(
                "INSERT INTO workflow_node_runs (id, run_id, node_key, node_type,"
                " status, input_digest, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
                (_new_id("wfn"), run_id, key, str(node.get("type") or "task"),
                 digest, now, now),
            )
        events.emit(conn, "workflow.run.started", project_id=project_id,
                    resource_type="workflow_run", resource_id=run_id,
                    payload={"version_id": binding["version_id"], "nodes": len(nodes)},
                    correlation_id=correlation_id)
    return advance_run(conn, run_id)


def _node_rows(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM workflow_node_runs WHERE run_id = ?", (run_id,)
    )]


def _run_row(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise WorkflowError("not_found", 404, "运行实例不存在")
    return row


def _dsl_nodes(conn: sqlite3.Connection, run: sqlite3.Row) -> dict[str, dict[str, Any]]:
    version = get_version(conn, run["version_id"])
    return {
        str(n.get("key")): n for n in (version["dsl"].get("nodes") or [])
        if n.get("key")
    }


def _has_valid_approval(conn: sqlite3.Connection, run_id: str, node_key: str,
                        digest: str) -> dict[str, Any] | None:
    """该节点当前是否有一个**仍然有效**的批准。

    有效性三条件缺一不可：decision=approved、未撤销、input_digest 与当前一致。
    过期（expires_at 早于现在）也算失效。
    """
    row = conn.execute(
        "SELECT * FROM workflow_approvals WHERE run_id = ? AND node_key = ?"
        " AND input_digest = ? AND decision = 'approved' AND revoked_at IS NULL"
        " ORDER BY created_at DESC LIMIT 1",
        (run_id, node_key, digest),
    ).fetchone()
    if row is None:
        return None
    expires = store.iso_utc(row["expires_at"]) if row["expires_at"] else None
    if expires and expires < store.now_iso():
        return None
    return dict(row)


def approve_node(conn: sqlite3.Connection, run_id: str, node_key: str, *,
                 approver: str, reason: str = "", decision: str = "approved",
                 expires_at: str | None = None) -> dict[str, Any]:
    """审批一个闸门节点。``approver`` 必须由上层从可信 Principal 传入。

    这里**不接受匿名或自报身份**：调用方要么拿到真实 principal，
    要么这个闸门就该一直挂着——审批人是审计链的一环，不能是字符串占位。
    """
    if decision not in ("approved", "rejected"):
        raise WorkflowError("invalid_payload", 422, "decision 只能是 approved / rejected")
    if not str(approver or "").strip():
        raise WorkflowError("invalid_payload", 422,
                            "审批人必填：审批必须绑定可信身份，不能匿名")

    run = _run_row(conn, run_id)
    node = conn.execute(
        "SELECT * FROM workflow_node_runs WHERE run_id = ? AND node_key = ?",
        (run_id, node_key),
    ).fetchone()
    if node is None:
        raise WorkflowError("not_found", 404, "该运行没有这个节点")
    if node["node_type"] not in BLOCKING_NODE_TYPES:
        raise WorkflowError("invalid_state", 422, f"节点 {node_key} 不是闸门，无需审批")
    if node["status"] == "completed":
        raise WorkflowError("invalid_state", 409, "节点已完成，审批无意义")

    now = store.now_iso()
    approval_id = _new_id("wfa")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO workflow_approvals (id, run_id, node_key, input_digest,"
            " decision, approver, reason, expires_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (approval_id, run_id, node_key, node["input_digest"], decision,
             approver, reason, expires_at, now),
        )
        conn.execute(
            "UPDATE workflow_node_runs SET status = ?, approver = ?, approved_at = ?,"
            " decided_at = ?, updated_at = ? WHERE run_id = ? AND node_key = ?",
            ("approved" if decision == "approved" else "rejected",
             approver, now if decision == "approved" else None, now, now,
             run_id, node_key),
        )
        events.emit(
            conn, f"workflow.gate.{decision}", project_id=run["project_id"],
            resource_type="workflow_run", resource_id=run_id,
            payload={"node_key": node_key, "approver": approver,
                     "digest": node["input_digest"]},
            correlation_id=run["correlation_id"],
        )
        # 闸门审批是高风险动作：谁批准了哪个节点、批准的是哪份输入，必须留痕。
        _audit(conn, run["project_id"], f"workflow.gate.{decision}", run_id,
               {"node_key": node_key, "approver": approver,
                "input_digest": node["input_digest"]},
               actor=approver)
    return advance_run(conn, run_id)


def revoke_approval(conn: sqlite3.Connection, approval_id: str, *, reason: str,
                    revoked_by: str) -> dict[str, Any]:
    """撤销审批。撤销后节点回到 waiting_approval，流程立刻停住。"""
    row = conn.execute(
        "SELECT * FROM workflow_approvals WHERE id = ?", (approval_id,)
    ).fetchone()
    if row is None:
        raise WorkflowError("not_found", 404, "审批记录不存在")
    if row["revoked_at"]:
        return {"approval_id": approval_id, "revoked_at": row["revoked_at"],
                "already_revoked": True}
    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "UPDATE workflow_approvals SET revoked_at = ?, reason = COALESCE(reason, ?)"
            " WHERE id = ?", (now, reason, approval_id),
        )
        conn.execute(
            "UPDATE workflow_node_runs SET status = 'waiting_approval', approver = NULL,"
            " approved_at = NULL, updated_at = ? WHERE run_id = ? AND node_key = ?",
            (now, row["run_id"], row["node_key"]),
        )
        run = _run_row(conn, row["run_id"])
        _audit(conn, run["project_id"], "workflow.gate.revoke", row["run_id"],
               {"node_key": row["node_key"], "approval_id": approval_id,
                "reason": reason}, actor=revoked_by)
    return advance_run(conn, row["run_id"])


def _sync_from_tasks(conn: sqlite3.Connection, run: sqlite3.Row,
                     dsl: dict[str, dict[str, Any]],
                     by_key: dict[str, dict[str, Any]]) -> None:
    """用第三期 task 的真实状态回填节点状态。

    Workflow 域**不执行**任何业务动作（见模块 docstring）：被物化出来的节点
    对应第三期 ``tasks``，它们的完成与否只能从 tasks 读回来。
    这里按 ``task_key = wf:<def_key>:<node_key>`` 精确匹配，
    不做任何「看起来像就关联」的推断。
    """
    definition = conn.execute(
        "SELECT * FROM workflow_definitions WHERE id = ("
        " SELECT definition_id FROM workflow_versions WHERE id = ?)",
        (run["version_id"],),
    ).fetchone()
    if definition is None:
        return
    now = store.now_iso()
    for key, node in by_key.items():
        if node["node_type"] in BLOCKING_NODE_TYPES:
            continue
        if node["status"] in ("completed", "skipped", "failed"):
            continue
        task_key = f"wf:{definition['def_key']}:{key}"
        task = conn.execute(
            "SELECT id, status FROM tasks WHERE project_id = ? AND task_key = ?",
            (run["project_id"], task_key),
        ).fetchone()
        if task is None:
            continue
        if node["task_id"] != task["id"]:
            conn.execute(
                "UPDATE workflow_node_runs SET task_id = ?, updated_at = ? WHERE id = ?",
                (task["id"], now, node["id"]),
            )
        if task["status"] == "done":
            conn.execute(
                "UPDATE workflow_node_runs SET status = 'completed', updated_at = ?"
                " WHERE id = ?", (now, node["id"]),
            )
        elif task["status"] in ("in_progress", "review"):
            conn.execute(
                "UPDATE workflow_node_runs SET status = 'running', updated_at = ?"
                " WHERE id = ?", (now, node["id"]),
            )


def advance_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    """推进状态机到第一个可执行点。Human Gate 没有有效审批就停在这里。

    规则（M3 验收）：

    1. 依赖全部 completed 的节点才可进入；
    2. human_gate / quality_gate 没有有效批准 → 状态 ``waiting_approval``，
       运行整体 ``waiting_approval``，**不往下走**；
    3. 有批准 → completed，继续找下一个；
    4. 全部 completed → 运行 completed。

    非闸门节点的 completed 由第三期 task 状态回推（见 ``_sync_from_tasks``）：
    本域不执行任务，凭空假设它们完成会让闸门形同虚设。
    """
    run = _run_row(conn, run_id)
    if run["status"] in ("completed", "cancelled", "failed"):
        return get_run(conn, run_id)

    dsl = _dsl_nodes(conn, run)
    by_key = {n["node_key"]: n for n in _node_rows(conn, run_id)}
    if not by_key:
        return get_run(conn, run_id)
    digest = run["input_digest"]

    # 先把 tasks 的真实状态同步进来，再决定能不能往下走。
    _sync_from_tasks(conn, run, dsl, by_key)
    nodes = _node_rows(conn, run_id)
    by_key = {n["node_key"]: n for n in nodes}

    waiting: list[str] = []
    with store.write_tx(conn):
        for node in nodes:
            if node["status"] in ("completed", "skipped", "rejected", "failed"):
                continue
            spec = dsl.get(node["node_key"], {})
            deps = spec.get("depends_on") or []
            if any(by_key.get(d) is not None
                   and by_key[d]["status"] != "completed" for d in deps):
                continue
            if node["node_type"] in BLOCKING_NODE_TYPES:
                approval = _has_valid_approval(conn, run_id, node["node_key"], digest)
                if approval is None:
                    if node["status"] != "waiting_approval":
                        conn.execute(
                            "UPDATE workflow_node_runs SET status = 'waiting_approval',"
                            " updated_at = ? WHERE id = ?",
                            (store.now_iso(), node["id"]),
                        )
                    waiting.append(node["node_key"])
                    continue
                conn.execute(
                    "UPDATE workflow_node_runs SET status = 'completed',"
                    " updated_at = ? WHERE id = ?", (store.now_iso(), node["id"]))
                continue
            # 非闸门节点：推进到 running，表示「可以交给 scheduler」。
            # 真正的完成来自 task 状态，不在这里臆断。
            if node["status"] == "pending":
                conn.execute(
                    "UPDATE workflow_node_runs SET status = 'running', updated_at = ?"
                    " WHERE id = ?", (store.now_iso(), node["id"]))

        if waiting:
            status, current = "waiting_approval", waiting[0]
        else:
            remaining = [n for n in _node_rows(conn, run_id)
                         if n["status"] not in ("completed", "skipped")]
            active = [n for n in remaining if n["status"] in ("running", "approved")]
            if not remaining:
                status, current = "completed", None
            elif active:
                status, current = "running", active[0]["node_key"]
            else:
                status = "running"
                current = remaining[0]["node_key"] if remaining else None
        finished = store.now_iso() if status == "completed" else None
        conn.execute(
            "UPDATE workflow_runs SET status = ?, current_node = ?,"
            " finished_at = COALESCE(?, finished_at), updated_at = ? WHERE id = ?",
            (status, current, finished, store.now_iso(), run_id),
        )
    return get_run(conn, run_id)


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = _run_row(conn, run_id)
    nodes = _node_rows(conn, run_id)
    approvals = [dict(r) for r in conn.execute(
        "SELECT * FROM workflow_approvals WHERE run_id = ? ORDER BY created_at DESC",
        (run_id,),
    )]
    return {
        **dict(run),
        "nodes": nodes,
        "approvals": approvals,
        "waiting_on": [n["node_key"] for n in nodes
                       if n["status"] == "waiting_approval"],
        "blocked": bool(run["status"] == "waiting_approval"),
    }


def list_runs(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM workflow_runs WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(r) for r in rows], "total": len(rows)}

