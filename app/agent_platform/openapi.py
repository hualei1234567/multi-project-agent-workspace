"""Agent 平台 OpenAPI 3.1 文档（由路由清单生成，保持与 docs/113 一致）。

维护约定：新增/删除接口时同步 ``API_PATHS``；``scripts/tests/test_openapi.py``
会断言文档与实际清单一致，并对若干端点做真实 HTTP 探测。
"""
from __future__ import annotations

from typing import Any

OPENAPI_VERSION = "3.1.0"
API_VERSION = "2.0"
TITLE = "多项目 Agent 管理平台 API"
DESCRIPTION = (
    "本机只读/可写管理接口。契约细则见 docs/113-多项目Agent平台-API契约二期.md。"
    "所有项目级接口校验 project_id；归档项目仅放行读与用户偏好。"
)

#: 已实现的路由清单（METHOD, 路径模板）。测试以此与文档互相校验。
API_PATHS: list[tuple[str, str]] = [
    ("GET", "/api/openapi.json"),
    ("GET", "/api/state"),
    ("GET", "/api/projects"),
    ("POST", "/api/projects"),
    ("GET", "/api/projects/{project_id}"),
    ("PATCH", "/api/projects/{project_id}"),
    ("POST", "/api/projects/{project_id}/archive"),
    ("POST", "/api/projects/{project_id}/restore"),
    ("POST", "/api/projects/{project_id}/backfill"),
    ("POST", "/api/projects/{project_id}/team/preview"),
    ("POST", "/api/projects/{project_id}/team/apply"),
    ("GET", "/api/projects/{project_id}/tasks"),
    ("POST", "/api/projects/{project_id}/tasks"),
    ("GET", "/api/projects/{project_id}/tasks/{task_id}"),
    ("PATCH", "/api/projects/{project_id}/tasks/{task_id}"),
    ("POST", "/api/projects/{project_id}/tasks/{task_id}/transition"),
    ("POST", "/api/projects/{project_id}/tasks/{task_id}/dependencies"),
    ("DELETE", "/api/projects/{project_id}/tasks/{task_id}/dependencies/{dependency_id}"),
    ("GET", "/api/projects/{project_id}/deliveries"),
    ("POST", "/api/projects/{project_id}/deliveries"),
    ("POST", "/api/projects/{project_id}/deliveries/{delivery_id}/review"),
    ("GET", "/api/projects/{project_id}/executions"),
    ("GET", "/api/projects/{project_id}/executions/{execution_id}"),
    ("GET", "/api/projects/{project_id}/documents"),
    ("POST", "/api/projects/{project_id}/documents"),
    ("PATCH", "/api/projects/{project_id}/documents/{document_id}"),
    ("DELETE", "/api/projects/{project_id}/documents/{document_id}"),
    ("GET", "/api/projects/{project_id}/events"),
    ("PUT", "/api/projects/{project_id}/favorite"),
    ("DELETE", "/api/projects/{project_id}/favorite"),
    ("POST", "/api/projects/{project_id}/visit"),
    ("GET", "/api/me/recent-projects"),
    ("GET", "/api/audit-logs"),
    ("GET", "/api/team-templates"),
    ("POST", "/api/team-templates"),
    ("GET", "/api/team-templates/{template_key}"),
    ("POST", "/api/team-templates/{template_key}/clone"),
    ("GET", "/api/agent-platform/overview"),
    ("POST", "/api/projects/{project_id}/dispatch/plan"),
    ("GET", "/api/projects/{project_id}/dispatch/plans"),
    ("GET", "/api/projects/{project_id}/dispatch/plans/{plan_id}"),
    ("GET", "/api/projects/{project_id}/dispatch/items/{item_id}"),
    ("GET", "/api/projects/{project_id}/dispatch/schedulable"),
    ("GET", "/api/agent/{agent_id}"),
]


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _json(schema: dict[str, Any]) -> dict[str, Any]:
    return {"application/json": {"schema": schema}}


def _ok(schema: dict[str, Any] | None = None, description: str = "成功") -> dict[str, Any]:
    body: dict[str, Any] = {"description": description}
    if schema is not None:
        body["content"] = _json(schema)
    return body


def _err(code: int, what: str) -> dict[str, Any]:
    return {str(code): _ok(_ref("Error"), what)}


def _operation(
    summary: str,
    *,
    tags: list[str],
    params: list[dict[str, Any]] | None = None,
    body: dict[str, Any] | None = None,
    ok_schema: dict[str, Any] | None = None,
    errors: tuple[int, ...] = (400, 403, 404, 409, 422),
) -> dict[str, Any]:
    op: dict[str, Any] = {
        "summary": summary,
        "tags": tags,
        "responses": {"200": _ok(ok_schema)},
    }
    if params:
        op["parameters"] = params
    if body is not None:
        op["requestBody"] = {"required": True, "content": _json(body)}
    names = {400: "参数错误", 403: "无权限或项目已归档", 404: "资源不存在",
             409: "冲突（版本/状态/只读/幂等）", 422: "业务校验失败"}
    for code in errors:
        op["responses"][str(code)] = _ok(_ref("Error"), names[code])
    return op


def _param(name: str, where: str = "query", typ: str = "string", required: bool = False) -> dict[str, Any]:
    return {"name": name, "in": where, "required": required, "schema": {"type": typ}}


PAGE_PARAMS = [_param("page", typ="integer"), _param("page_size", typ="integer")]


def _schemas() -> dict[str, Any]:
    str_or_null = {"type": ["string", "null"]}
    return {
        "Error": {
            "type": "object",
            "required": ["detail"],
            "properties": {
                "detail": {
                    "type": "object",
                    "required": ["code", "message"],
                    "properties": {
                        "code": {"type": "string", "examples": ["not_found", "project_archived"]},
                        "message": {"type": "string"},
                    },
                    "additionalProperties": True,
                }
            },
        },
        "Project": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "code": {"type": "string"},
                "name": {"type": "string"},
                "description": str_or_null,
                "status": {"type": "string", "enum": ["active", "paused", "risk", "archived"]},
                "owner": str_or_null,
                "repo_root": {"type": "string"},
                "team_template_key": {"type": "string"},
                "team_template_version": {"type": "integer"},
                "archived_at": str_or_null,
                "version": {"type": "integer"},
                "favorite": {"type": "boolean"},
                "created_at": {"type": "string"},
                "updated_at": {"type": "string"},
            },
        },
        "Agent": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "project_id": {"type": "string"},
                "role_key": {"type": "string"},
                "display_name": {"type": "string"},
                "instance_key": {"type": "string"},
                "agent_type": {"type": "string", "enum": ["main", "subagent"]},
                "execution_mode": {"type": "string"},
                "status": {"type": "string"},
                "current_task_id": str_or_null,
                "current_branch": str_or_null,
                "worktree_path": str_or_null,
                "model_id": str_or_null,
                "last_heartbeat_at": str_or_null,
                "role_snapshot_json": {"type": "object"},
            },
        },
        "Task": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "project_id": {"type": "string"},
                "task_key": {"type": "string"},
                "title": {"type": "string"},
                "description": str_or_null,
                "status": {"type": "string", "enum": ["todo", "in_progress", "review", "done", "blocked"]},
                "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                "assignee_agent_id": str_or_null,
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "blocked_reason": str_or_null,
                "branch_name": str_or_null,
                "started_at": str_or_null,
                "completed_at": str_or_null,
                "version": {"type": "integer"},
                "readonly": {"type": "boolean"},
                "source": {"type": "string"},
            },
        },
        "Delivery": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "project_id": {"type": "string"},
                "task_id": str_or_null,
                "title": {"type": "string"},
                "kind": {"type": "string"},
                "summary": str_or_null,
                "status": {"type": "string", "enum": ["submitted", "in_review", "accepted", "rejected"]},
                "path": str_or_null,
                "sha": str_or_null,
                "reviewer": str_or_null,
                "reviewed_at": str_or_null,
                "rejection_reason": str_or_null,
                "readonly": {"type": "boolean"},
            },
        },
        "Execution": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "project_id": {"type": "string"},
                "agent_id": str_or_null,
                "task_id": str_or_null,
                "session_id": {"type": "string"},
                "status": {"type": "string"},
                "started_at": str_or_null,
                "finished_at": str_or_null,
                "duration_ms": {"type": ["integer", "null"]},
                "summary": str_or_null,
                "error_summary": str_or_null,
                "model_id": str_or_null,
                "total_tokens": {"type": "integer"},
            },
        },
        "Document": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "project_id": {"type": "string"},
                "title": {"type": "string"},
                "document_type": {"type": "string"},
                "uri": {"type": "string"},
                "owner_agent_id": str_or_null,
                "updated_at": str_or_null,
                "created_at": {"type": "string"},
            },
        },
        "AuditLog": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "project_id": str_or_null,
                "actor": {"type": "string"},
                "action": {"type": "string"},
                "resource_type": {"type": "string"},
                "resource_id": str_or_null,
                "result": {"type": "string"},
                "detail_json": {"type": "object"},
                "created_at": {"type": "string"},
            },
        },
        "Page": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {}},
                "total": {"type": "integer"},
                "page": {"type": "integer"},
                "page_size": {"type": "integer"},
            },
        },
        "State": {"type": "object", "additionalProperties": True},
    }


def _paths() -> dict[str, Any]:
    pid = _param("project_id", "path", required=True)
    tid = _param("task_id", "path", required=True)

    def project_op(method: str, summary: str, **kw: Any) -> tuple[str, str, dict[str, Any]]:
        return method, summary, _operation(summary, tags=["project"], params=[pid] + kw.pop("params", []), **kw)

    paths: dict[str, Any] = {
        "/api/openapi.json": {"get": _operation("OpenAPI 3.1 文档", tags=["meta"], ok_schema={"type": "object"}, errors=())},
        "/api/state": {
            "get": _operation(
                "平台/项目状态快照（含 Agent/任务/看板/用量）",
                tags=["state"],
                params=[_param("project"), _param("refresh", typ="integer")],
                ok_schema=_ref("State"),
                errors=(404,),
            )
        },
        "/api/projects": {
            "get": _operation("项目列表", tags=["project"], params=[_param("q"), _param("status"), _param("favorite")], ok_schema=_ref("Page"), errors=()),
            "post": _operation("创建项目（幂等）", tags=["project"], body={"type": "object"}, ok_schema=_ref("Project")),
        },
        "/api/projects/{project_id}": {
            "get": _operation("项目详情", tags=["project"], params=[pid], ok_schema=_ref("Project"), errors=(404,)),
            "patch": _operation("更新项目（version 乐观锁）", tags=["project"], params=[pid], body={"type": "object"}, ok_schema=_ref("Project")),
        },
        "/api/projects/{project_id}/archive": {"post": project_op("post", "归档项目")[2]},
        "/api/projects/{project_id}/restore": {"post": project_op("post", "恢复项目")[2]},
        "/api/projects/{project_id}/backfill": {"post": project_op("post", "回填采集数据")[2]},
        "/api/projects/{project_id}/team/preview": {"post": project_op("post", "预览应用团队模板")[2]},
        "/api/projects/{project_id}/team/apply": {"post": project_op("post", "应用团队模板（保留旧实例）")[2]},
        "/api/projects/{project_id}/dispatch/plan": {
            "post": project_op("post", "生成调度计划（纯计算，不执行）")[2],
        },
        "/api/projects/{project_id}/dispatch/plans": {
            "get": project_op("get", "调度计划列表（分页）", params=PAGE_PARAMS,
                              ok_schema=_ref("Page"))[2],
        },
        "/api/projects/{project_id}/dispatch/plans/{plan_id}": {
            "get": project_op("get", "调度计划详情（含调度项）", params=[_param("plan_id", where="path", required=True)],
                              ok_schema={"type": "object"}, errors=(404,))[2],
        },
        "/api/projects/{project_id}/dispatch/items/{item_id}": {
            "get": project_op("get", "调度项详情（含日志与事件）", params=[_param("item_id", where="path", required=True)],
                              ok_schema={"type": "object"}, errors=(404,))[2],
        },
        "/api/projects/{project_id}/dispatch/schedulable": {
            "get": project_op("get", "当前可调度任务与被跳过原因（只读）")[2],
        },
        "/api/projects/{project_id}/tasks": {
            "get": project_op("get", "任务列表（分页）", params=PAGE_PARAMS, ok_schema=_ref("Page"))[2],
            "post": project_op("post", "创建任务")[2],
        },
        "/api/projects/{project_id}/tasks/{task_id}": {
            "get": project_op("get", "任务详情", params=[tid], ok_schema=_ref("Task"), errors=(404,))[2],
            "patch": project_op("patch", "更新任务", params=[tid], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/tasks/{task_id}/transition": {
            "post": project_op("post", "任务状态流转", params=[tid], body={"type": "object"})[2]
        },
        "/api/projects/{project_id}/tasks/{task_id}/dependencies": {
            "post": project_op("post", "新增任务依赖", params=[tid], body={"type": "object"})[2]
        },
        "/api/projects/{project_id}/tasks/{task_id}/dependencies/{dependency_id}": {
            "delete": project_op("delete", "删除任务依赖", params=[tid, _param("dependency_id", "path", required=True)], errors=(404, 409))[2]
        },
        "/api/projects/{project_id}/deliveries": {
            "get": project_op("get", "交付列表（分页，含镜像）", params=PAGE_PARAMS, ok_schema=_ref("Page"))[2],
            "post": project_op("post", "创建交付")[2],
        },
        "/api/projects/{project_id}/deliveries/{delivery_id}/review": {
            "post": project_op("post", "交付验收/退回", params=[_param("delivery_id", "path", required=True)], body={"type": "object"})[2]
        },
        "/api/projects/{project_id}/executions": {"get": project_op("get", "执行记录（分页）", params=PAGE_PARAMS, ok_schema=_ref("Page"))[2]},
        "/api/projects/{project_id}/executions/{execution_id}": {
            "get": project_op("get", "执行详情", params=[_param("execution_id", "path", required=True)], ok_schema=_ref("Execution"), errors=(404,))[2]
        },
        "/api/projects/{project_id}/documents": {
            "get": project_op("get", "文档索引", params=PAGE_PARAMS, ok_schema=_ref("Page"))[2],
            "post": project_op("post", "新增文档索引")[2],
        },
        "/api/projects/{project_id}/documents/{document_id}": {
            "patch": project_op("patch", "更新文档索引", params=[_param("document_id", "path", required=True)], body={"type": "object"})[2],
            "delete": project_op("delete", "删除文档索引", params=[_param("document_id", "path", required=True)], errors=(404,))[2],
        },
        "/api/projects/{project_id}/events": {"get": project_op("get", "项目事件（分页）", params=PAGE_PARAMS, ok_schema=_ref("Page"))[2]},
        "/api/projects/{project_id}/favorite": {
            "put": project_op("put", "收藏项目", errors=(404,))[2],
            "delete": project_op("delete", "取消收藏", errors=(404,))[2],
        },
        "/api/projects/{project_id}/visit": {"post": project_op("post", "记录访问", errors=(404,))[2]},
        "/api/me/recent-projects": {
            "get": _operation("最近访问项目", tags=["preference"], params=[_param("limit", typ="integer")], ok_schema=_ref("Page"), errors=())
        },
        "/api/audit-logs": {
            "get": _operation("审计日志", tags=["audit"], params=[_param("project"), _param("action"), _param("from"), _param("to")] + PAGE_PARAMS, ok_schema=_ref("Page"), errors=())
        },
        "/api/team-templates": {
            "get": _operation("模板列表", tags=["template"], ok_schema=_ref("Page"), errors=()),
            "post": _operation("创建自定义模板", tags=["template"], body={"type": "object"}, errors=(400, 409, 422)),
        },
        "/api/team-templates/{template_key}": {
            "get": _operation("模板详情（可带 version）", tags=["template"], params=[_param("template_key", "path", required=True), _param("version", typ="integer")], ok_schema={"type": "object"}, errors=(404,))
        },
        "/api/team-templates/{template_key}/clone": {
            "post": _operation("克隆模板", tags=["template"], params=[_param("template_key", "path", required=True)], body={"type": "object"}, errors=(400, 404, 409, 422))
        },
        "/api/agent-platform/overview": {
            "get": _operation("平台总览", tags=["platform"], ok_schema={"type": "object"}, errors=())
        },
        "/api/agent/{agent_id}": {
            "get": _operation("Agent 执行轨迹", tags=["agent"], params=[_param("agent_id", "path", required=True)], ok_schema={"type": "object"}, errors=(404,))
        },
    }
    return paths


def build_openapi() -> dict[str, Any]:
    return {
        "openapi": OPENAPI_VERSION,
        "info": {"title": TITLE, "version": API_VERSION, "description": DESCRIPTION},
        "servers": [{"url": "http://127.0.0.1:8099"}],
        "tags": [
            {"name": "meta"}, {"name": "state"}, {"name": "project"}, {"name": "template"},
            {"name": "agent"}, {"name": "audit"}, {"name": "preference"}, {"name": "platform"},
        ],
        "paths": _paths(),
        "components": {"schemas": _schemas()},
    }
