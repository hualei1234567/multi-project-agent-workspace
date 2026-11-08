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
    ("POST", "/api/projects/{project_id}/eval/suites"),
    ("GET", "/api/projects/{project_id}/eval/suites"),
    ("POST", "/api/projects/{project_id}/eval/datasets"),
    ("GET", "/api/projects/{project_id}/eval/datasets"),
    ("POST", "/api/projects/{project_id}/eval/datasets/{dataset_id}/versions"),
    ("POST", "/api/projects/{project_id}/eval/evaluators"),
    ("GET", "/api/projects/{project_id}/eval/evaluators"),
    ("POST", "/api/projects/{project_id}/eval/runs"),
    ("GET", "/api/projects/{project_id}/eval/runs"),
    ("GET", "/api/projects/{project_id}/eval/runs/{run_id}"),
    ("POST", "/api/projects/{project_id}/knowledge/sources"),
    ("GET", "/api/projects/{project_id}/knowledge/sources"),
    ("POST", "/api/projects/{project_id}/knowledge/items"),
    ("GET", "/api/projects/{project_id}/knowledge/items"),
    ("POST", "/api/projects/{project_id}/knowledge/versions/{version_id}/publish"),
    ("POST", "/api/projects/{project_id}/knowledge/versions/{version_id}/revoke"),
    ("GET", "/api/projects/{project_id}/knowledge/search"),
    ("POST", "/api/projects/{project_id}/workflows"),
    ("GET", "/api/projects/{project_id}/workflows"),
    ("POST", "/api/projects/{project_id}/workflows/{definition_id}/versions"),
    ("POST", "/api/projects/{project_id}/workflow-versions/{version_id}/test"),
    ("POST", "/api/projects/{project_id}/workflow-versions/{version_id}/publish"),
    ("POST", "/api/projects/{project_id}/workflow-versions/{version_id}/bind"),
    ("GET", "/api/projects/{project_id}/workflow-bindings"),
    ("POST", "/api/projects/{project_id}/releases"),
    ("GET", "/api/projects/{project_id}/releases"),
    ("GET", "/api/projects/{project_id}/releases/{release_id}"),
    ("POST", "/api/projects/{project_id}/releases/{release_id}/validate"),
    ("POST", "/api/projects/{project_id}/releases/{release_id}/evaluation"),
    ("POST", "/api/projects/{project_id}/releases/{release_id}/request-approval"),
    ("POST", "/api/projects/{project_id}/releases/{release_id}/publish"),
    ("POST", "/api/projects/{project_id}/releases/{release_id}/canary"),
    ("POST", "/api/projects/{project_id}/releases/{release_id}/rollback"),
    ("POST", "/api/projects/{project_id}/approvals/{approval_id}/approve"),
    ("GET", "/api/projects/{project_id}/canaries"),
    ("POST", "/api/projects/{project_id}/canaries/{deployment_id}/observe"),
    ("POST", "/api/projects/{project_id}/canaries/{deployment_id}/promote"),
    ("GET", "/api/projects/{project_id}/rollbacks"),
    ("POST", "/api/projects/{project_id}/improvements/failures"),
    ("GET", "/api/projects/{project_id}/improvements/failures"),
    ("POST", "/api/projects/{project_id}/improvements/cases"),
    ("GET", "/api/projects/{project_id}/improvements/cases"),
    ("POST", "/api/projects/{project_id}/improvements/cases/{case_id}/root-cause"),
    ("POST", "/api/projects/{project_id}/improvements/proposals"),
    ("POST", "/api/projects/{project_id}/improvements/proposals/{proposal_id}/sandbox"),
    ("POST", "/api/projects/{project_id}/improvements/proposals/{proposal_id}/request-approval"),
    ("POST", "/api/projects/{project_id}/improvements/approvals/{approval_id}/approve"),
    ("POST", "/api/projects/{project_id}/outcome/metrics"),
    ("GET", "/api/projects/{project_id}/outcome/metrics"),
    ("POST", "/api/projects/{project_id}/outcome/records"),
    ("GET", "/api/projects/{project_id}/outcome/summary"),
    ("GET", "/api/projects/{project_id}/outcome/cost"),
    ("POST", "/api/projects/{project_id}/autonomy/eligibilities"),
    ("GET", "/api/projects/{project_id}/autonomy/eligibilities"),
    ("POST", "/api/projects/{project_id}/autonomy/eligibilities/{eligibility_id}/evidence"),
    ("POST", "/api/projects/{project_id}/autonomy/eligibilities/{eligibility_id}/approve"),
    ("POST", "/api/projects/{project_id}/autonomy/eligibilities/{eligibility_id}/suspend"),
    ("POST", "/api/projects/{project_id}/catalog/listings"),
    ("GET", "/api/projects/{project_id}/catalog/listings"),
    ("POST", "/api/projects/{project_id}/catalog/grants"),
    ("GET", "/api/projects/{project_id}/catalog/grants"),
    ("POST", "/api/projects/{project_id}/catalog/grants/{grant_id}/revoke"),
    ("GET", "/api/projects/{project_id}/tasks"),
    ("POST", "/api/projects/{project_id}/tasks"),
    ("GET", "/api/projects/{project_id}/tasks/{task_id}"),
    ("PATCH", "/api/projects/{project_id}/tasks/{task_id}"),
    ("POST", "/api/projects/{project_id}/tasks/{task_id}/transition"),
    ("POST", "/api/projects/{project_id}/tasks/{task_id}/dependencies"),
    ("DELETE", "/api/projects/{project_id}/tasks/{task_id}/dependencies/{dependency_id}"),
    ("GET", "/api/projects/{project_id}/deliveries"),
    ("POST", "/api/projects/{project_id}/deliveries"),
    ("GET", "/api/projects/{project_id}/deliveries/{delivery_id}"),
    ("POST", "/api/projects/{project_id}/deliveries/{delivery_id}/review"),
    ("POST", "/api/projects/{project_id}/deliveries/{delivery_id}/validate"),
    ("POST", "/api/projects/{project_id}/deliveries/{delivery_id}/resubmit"),
    ("GET", "/api/projects/{project_id}/executions"),
    ("GET", "/api/projects/{project_id}/executions/{execution_id}"),
    ("GET", "/api/projects/{project_id}/documents"),
    ("POST", "/api/projects/{project_id}/documents"),
    ("PATCH", "/api/projects/{project_id}/documents/{document_id}"),
    ("DELETE", "/api/projects/{project_id}/documents/{document_id}"),
    ("GET", "/api/projects/{project_id}/events"),
    ("GET", "/api/projects/{project_id}/timeline"),
    ("PUT", "/api/projects/{project_id}/favorite"),
    ("DELETE", "/api/projects/{project_id}/favorite"),
    ("POST", "/api/projects/{project_id}/visit"),
    ("GET", "/api/me/recent-projects"),
    ("GET", "/api/workspace/portfolio"),
    ("GET", "/api/workspace/my"),
    ("GET", "/api/workspace/notifications"),
    ("POST", "/api/workspace/notifications/read"),
    ("GET", "/api/search"),
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
    ("GET", "/api/projects/{project_id}/dispatch/executor"),
    ("POST", "/api/projects/{project_id}/dispatch/items/{item_id}/run"),
    ("POST", "/api/projects/{project_id}/dispatch/items/{item_id}/dry-run"),
    ("GET", "/api/runtime/services"),
    ("POST", "/api/runtime/services"),
    ("POST", "/api/runtime/collect"),
    ("GET", "/api/runtime/actions"),
    ("POST", "/api/runtime/actions/{action_key}/preflight"),
    ("POST", "/api/runtime/actions/{action_key}/run"),
    ("GET", "/api/agent-platform/cross-project"),
    ("GET", "/api/shared-resources"),
    ("POST", "/api/shared-resources"),
    ("GET", "/api/shared-resources/{resource_id}"),
    ("POST", "/api/shared-resources/{resource_id}/claims"),
    ("DELETE", "/api/shared-resources/{resource_id}/claims/{claim_id}"),
    ("GET", "/api/projects/{project_id}/tasks/{task_id}/cross-dependencies"),
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
             409: "冲突（版本/状态/只读/幂等）", 412: "缺少预检令牌或确认",
             422: "业务校验失败"}
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
        "/api/projects/{project_id}/eval/suites": {
            "post": project_op("post", "创建评估套件（P4-M1）", body={"type": "object"})[2],
            "get": project_op("get", "评估套件列表（P4-M1）")[2],
        },
        "/api/projects/{project_id}/eval/datasets": {
            "post": project_op("post", "创建数据集（P4-M1）", body={"type": "object"})[2],
            "get": project_op("get", "数据集列表（P4-M1）")[2],
        },
        "/api/projects/{project_id}/eval/datasets/{dataset_id}/versions": {
            "post": project_op(
                "post", "冻结数据集版本（不可变，P4-M1）",
                params=[_param("dataset_id", where="path", required=True)],
                body={"type": "object"},
            )[2],
        },
        "/api/projects/{project_id}/eval/evaluators": {
            "post": project_op("post", "创建评估器与规则版本（P4-M1）", body={"type": "object"})[2],
            "get": project_op("get", "评估器列表（P4-M1）")[2],
        },
        "/api/projects/{project_id}/eval/runs": {
            "post": project_op(
                "post", "执行评估（安全项失败即整轮 fail，P4-M1）",
                body={"type": "object"},
            )[2],
            "get": project_op("get", "评估运行列表（P4-M1）")[2],
        },
        "/api/projects/{project_id}/eval/runs/{run_id}": {
            "get": project_op(
                "get", "评估运行详情与逐样本结果（P4-M1）",
                params=[_param("run_id", where="path", required=True)],
                errors=(404,),
            )[2],
        },
        "/api/projects/{project_id}/knowledge/sources": {
            "post": project_op("post", "登记知识来源（P4-M2）", body={"type": "object"})[2],
            "get": project_op("get", "知识来源列表（P4-M2）")[2],
        },
        "/api/projects/{project_id}/knowledge/items": {
            "post": project_op("post", "创建知识条目与候选版本（Secret 阻断，P4-M2）",
                               body={"type": "object"})[2],
            "get": project_op("get", "知识条目列表（P4-M2）")[2],
        },
        "/api/projects/{project_id}/knowledge/versions/{version_id}/publish": {
            "post": project_op(
                "post", "审核发布并建索引（P4-M2）",
                params=[_param("version_id", where="path", required=True)],
                body={"type": "object"},
            )[2],
        },
        "/api/projects/{project_id}/knowledge/versions/{version_id}/revoke": {
            "post": project_op(
                "post", "撤销版本并同步删除索引（P4-M2）",
                params=[_param("version_id", where="path", required=True)],
                body={"type": "object"},
            )[2],
        },
        "/api/projects/{project_id}/knowledge/search": {
            "get": project_op(
                "get", "项目内知识检索（权限前置过滤，P4-M2）",
                params=[_param("q", required=True), _param("limit", typ="integer")],
            )[2],
        },
        "/api/projects/{project_id}/workflows": {
            "post": project_op("post", "创建 Workflow 定义（P4-M3）", body={"type": "object"})[2],
            "get": project_op("get", "Workflow 定义列表（P4-M3）")[2],
        },
        "/api/projects/{project_id}/workflows/{definition_id}/versions": {
            "post": project_op(
                "post", "创建 Workflow 版本（静态校验，P4-M3）",
                params=[_param("definition_id", where="path", required=True)],
                body={"type": "object"},
            )[2],
        },
        "/api/projects/{project_id}/workflow-versions/{version_id}/test": {
            "post": project_op(
                "post", "沙箱测试（只校验不建任务，P4-M3）",
                params=[_param("version_id", where="path", required=True)],
            )[2],
        },
        "/api/projects/{project_id}/workflow-versions/{version_id}/publish": {
            "post": project_op(
                "post", "发布 Workflow 版本（P4-M3）",
                params=[_param("version_id", where="path", required=True)],
                body={"type": "object"},
            )[2],
        },
        "/api/projects/{project_id}/workflow-versions/{version_id}/bind": {
            "post": project_op(
                "post", "绑定到项目并物化为第三期任务（P4-M3）",
                params=[_param("version_id", where="path", required=True)],
                body={"type": "object"},
            )[2],
        },
        "/api/projects/{project_id}/workflow-bindings": {
            "get": project_op("get", "项目 Workflow 绑定列表（P4-M3）")[2],
        },
        "/api/projects/{project_id}/releases": {
            "post": project_op("post", "创建 Release 与 Manifest（P4-M4）", body={"type": "object"})[2],
            "get": project_op("get", "Release 列表（P4-M4）")[2],
        },
        "/api/projects/{project_id}/releases/{release_id}": {
            "get": project_op("get", "Release 详情与 Manifest 完整性（P4-M4）",
                              params=[_param("release_id", where="path", required=True)], errors=(404,))[2],
        },
        "/api/projects/{project_id}/releases/{release_id}/validate": {
            "post": project_op("post", "校验 Manifest（P4-M4）", params=[_param("release_id", where="path", required=True)])[2],
        },
        "/api/projects/{project_id}/releases/{release_id}/evaluation": {
            "post": project_op("post", "挂接评估门禁（decision 必须 pass，P4-M4）",
                               params=[_param("release_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/releases/{release_id}/request-approval": {
            "post": project_op("post", "申请发布审批（绑定 manifest 摘要，P4-M4）",
                               params=[_param("release_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/releases/{release_id}/publish": {
            "post": project_op("post", "发布（需已批准且摘要未变，P4-M4）", params=[_param("release_id", where="path", required=True)])[2],
        },
        "/api/projects/{project_id}/releases/{release_id}/canary": {
            "post": project_op("post", "启动 Canary（带停止阈值，P4-M4）",
                               params=[_param("release_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/releases/{release_id}/rollback": {
            "post": project_op("post", "回滚并留痕（不改写历史版本，P4-M4）",
                               params=[_param("release_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/approvals/{approval_id}/approve": {
            "post": project_op("post", "批准发布（提案人不得自批，P4-M4）",
                               params=[_param("approval_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/canaries": {
            "get": project_op("get", "Canary 部署列表（P4-M4）")[2],
        },
        "/api/projects/{project_id}/canaries/{deployment_id}/observe": {
            "post": project_op("post", "上报 Canary 指标（越阈值即停止，P4-M4）",
                               params=[_param("deployment_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/canaries/{deployment_id}/promote": {
            "post": project_op("post", "Canary 晋级（P4-M4）", params=[_param("deployment_id", where="path", required=True)])[2],
        },
        "/api/projects/{project_id}/rollbacks": {
            "get": project_op("get", "回滚记录（P4-M4）")[2],
        },
        "/api/projects/{project_id}/improvements/failures": {
            "post": project_op("post", "登记失败信号（P4-M5）", params=[], body={"type": "object"})[2],
            "get": project_op("get", "失败模式列表（P4-M5）", params=[])[2],
        },
        "/api/projects/{project_id}/improvements/cases": {
            "post": project_op("post", "创建改进 Case（P4-M5）", params=[], body={"type": "object"})[2],
            "get": project_op("get", "改进 Case 列表（P4-M5）", params=[])[2],
        },
        "/api/projects/{project_id}/improvements/cases/{case_id}/root-cause": {
            "post": project_op("post", "评估根因（必须带证据，P4-M5）", params=[_param("case_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/improvements/proposals": {
            "post": project_op("post", "创建变更提案（P4-M5）", params=[], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/improvements/proposals/{proposal_id}/sandbox": {
            "post": project_op("post", "沙箱评估（不通过即否决，P4-M5）", params=[_param("proposal_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/improvements/proposals/{proposal_id}/request-approval": {
            "post": project_op("post", "申请改进审批（P4-M5）", params=[_param("proposal_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/improvements/approvals/{approval_id}/approve": {
            "post": project_op("post", "批准改进（提案人不得自批，P4-M5）", params=[_param("approval_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/outcome/metrics": {
            "post": project_op("post", "定义指标（口径版本化，P4-M6）", params=[], body={"type": "object"})[2],
            "get": project_op("get", "指标字典（P4-M6）", params=[])[2],
        },
        "/api/projects/{project_id}/outcome/records": {
            "post": project_op("post", "记录指标值（quality 必填，P4-M6）", params=[], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/outcome/summary": {
            "get": project_op("get", "指标汇总（verified/estimated 分开，P4-M6）", params=[])[2],
        },
        "/api/projects/{project_id}/outcome/cost": {
            "get": project_op("get", "成本汇总（只读第三期账本，P4-M6）", params=[])[2],
        },
        "/api/projects/{project_id}/autonomy/eligibilities": {
            "post": project_op("post", "获取/创建自动化资格（P4-M7）", params=[], body={"type": "object"})[2],
            "get": project_op("get", "资格列表（P4-M7）", params=[])[2],
        },
        "/api/projects/{project_id}/autonomy/eligibilities/{eligibility_id}/evidence": {
            "post": project_op("post", "提交资格证据（P4-M7）", params=[_param("eligibility_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/autonomy/eligibilities/{eligibility_id}/approve": {
            "post": project_op("post", "批准等级（生产上限 A2，P4-M7）", params=[_param("eligibility_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/autonomy/eligibilities/{eligibility_id}/suspend": {
            "post": project_op("post", "暂停资格（回落到 A0，P4-M7）", params=[_param("eligibility_id", where="path", required=True)], body={"type": "object"})[2],
        },
        "/api/projects/{project_id}/catalog/listings": {
            "post": project_op("post", "上架能力（只存引用，P4-M8 P1）", body={"type": "object"})[2],
            "get": project_op("get", "本项目可用能力（自有 + 已授权，P4-M8 P1）")[2],
        },
        "/api/projects/{project_id}/catalog/grants": {
            "post": project_op("post", "授权能力给目标项目（需用途，P4-M8 P1）",
                               body={"type": "object"})[2],
            "get": project_op("get", "本项目的授权记录（P4-M8 P1）")[2],
        },
        "/api/projects/{project_id}/catalog/grants/{grant_id}/revoke": {
            "post": project_op(
                "post", "撤销能力授权（立即不可见，P4-M8 P1）",
                params=[_param("grant_id", where="path", required=True)],
                body={"type": "object"},
            )[2],
        },
        "/api/runtime/services": {
            "get": _operation("运行时服务列表（含最近一次探测）", tags=["runtime"],
                              ok_schema={"type": "object"}),
            "post": _operation("登记运行时服务", tags=["runtime"], body={"type": "object"},
                               ok_schema={"type": "object"}),
        },
        "/api/runtime/collect": {
            "post": _operation("立即探测全部服务", tags=["runtime"],
                               ok_schema={"type": "object"}),
        },
        "/api/runtime/actions": {
            "get": _operation("操作执行记录", tags=["runtime"], params=[_param("limit", typ="integer")],
                              ok_schema={"type": "object"}),
        },
        "/api/runtime/actions/{action_key}/preflight": {
            "post": _operation("操作预检（返回命令并签发令牌）", tags=["runtime"],
                               params=[_param("action_key", where="path", required=True)],
                               ok_schema={"type": "object"}, errors=(404,)),
        },
        "/api/runtime/actions/{action_key}/run": {
            "post": _operation("执行操作（需令牌 + 确认）", tags=["runtime"],
                               params=[_param("action_key", where="path", required=True)],
                               body={"type": "object"}, ok_schema={"type": "object"},
                               errors=(404, 412)),
        },
        "/api/agent-platform/cross-project": {
            "get": _operation("跨项目协作图（项目节点 + 依赖边 + 阻塞统计）",
                              tags=["platform"], ok_schema={"type": "object"}),
        },
        "/api/shared-resources": {
            "get": _operation("共享资源列表", tags=["platform"], ok_schema=_ref("Page")),
            "post": _operation("登记共享资源", tags=["platform"], body={"type": "object"},
                               ok_schema={"type": "object"}),
        },
        "/api/shared-resources/{resource_id}": {
            "get": _operation("共享资源详情（含活跃占用）", tags=["platform"],
                              params=[_param("resource_id", where="path", required=True)],
                              ok_schema={"type": "object"}, errors=(404,)),
        },
        "/api/shared-resources/{resource_id}/claims": {
            "post": _operation("申请占用资源", tags=["platform"],
                               params=[_param("resource_id", where="path", required=True)],
                               body={"type": "object"}, ok_schema={"type": "object"},
                               errors=(404, 409, 422)),
        },
        "/api/shared-resources/{resource_id}/claims/{claim_id}": {
            "delete": _operation("释放资源占用", tags=["platform"],
                                 params=[_param("resource_id", where="path", required=True),
                                         _param("claim_id", where="path", required=True)],
                                 ok_schema={"type": "object"}, errors=(404,)),
        },
        "/api/projects/{project_id}/tasks/{task_id}/cross-dependencies": {
            "get": project_op("get", "任务的跨项目依赖（进/出双向）",
                              params=[_param("task_id", where="path", required=True)])[2],
        },
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
        "/api/projects/{project_id}/dispatch/executor": {
            "get": project_op("get", "执行器开关状态")[2],
        },
        "/api/projects/{project_id}/dispatch/items/{item_id}/run": {
            "post": project_op("post", "执行调度项（受双重开关约束）",
                               params=[_param("item_id", where="path", required=True)])[2],
        },
        "/api/projects/{project_id}/dispatch/items/{item_id}/dry-run": {
            "post": project_op("post", "预演调度项（只返回 argv，不起进程）",
                               params=[_param("item_id", where="path", required=True)])[2],
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
        "/api/projects/{project_id}/deliveries/{delivery_id}": {
            "get": project_op("get", "交付详情：校验项 + 流转历史", params=[_param("delivery_id", "path", required=True)], errors=(404,))[2]
        },
        "/api/projects/{project_id}/deliveries/{delivery_id}/review": {
            "post": project_op("post", "交付验收/退回（验收前先跑校验，硬失败拦截）", params=[_param("delivery_id", "path", required=True)], body={"type": "object"}, errors=(404, 409, 422))[2]
        },
        "/api/projects/{project_id}/deliveries/{delivery_id}/validate": {
            "post": project_op("post", "执行交付校验并落库证据快照", params=[_param("delivery_id", "path", required=True)], errors=(404, 409))[2]
        },
        "/api/projects/{project_id}/deliveries/{delivery_id}/resubmit": {
            "post": project_op("post", "返工后重新提交（仅 rejected → submitted）", params=[_param("delivery_id", "path", required=True)], body={"type": "object"}, errors=(404, 409, 422))[2]
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
        "/api/projects/{project_id}/timeline": {
            "get": project_op("get", "执行时间线：四态（运行中/失败/未验证/成功）由证据推导", params=[_param("state", "query"), _param("limit", "query")])[2]
        },
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
        "/api/workspace/portfolio": {
            "get": _operation(
                "项目组合视图：逐项目健康快照（任务完成、阻塞、待验收交付、待审批、数据新鲜度）",
                tags=["workspace"], ok_schema={"type": "object"}, errors=(),
            )
        },
        "/api/workspace/my": {
            "get": _operation(
                "我的工作台：可见范围内的任务、审批、待验收交付与最近执行",
                tags=["workspace"], params=[_param("limit", typ="integer")],
                ok_schema={"type": "object"}, errors=(),
            )
        },
        "/api/workspace/notifications": {
            "get": _operation(
                "统一消息中心：审批 / 交付 / 阻塞任务 / 执行异常聚合，附个人已读状态",
                tags=["workspace"],
                params=[_param("status"), _param("project"), _param("limit", typ="integer")],
                ok_schema={"type": "object"}, errors=(),
            )
        },
        "/api/workspace/notifications/read": {
            "post": _operation(
                "标记消息已读/未读（只写个人偏好，不改业务状态）",
                tags=["workspace"], body={"type": "object"},
                ok_schema={"type": "object"}, errors=(400, 422),
            )
        },
        "/api/search": {
            "get": _operation(
                "全局搜索：服务端按可见项目过滤后再匹配 project/task/agent/delivery/workflow/knowledge/release",
                tags=["workspace"],
                params=[_param("q", required=True), _param("types"), _param("project"),
                        _param("limit", typ="integer")],
                ok_schema={"type": "object"}, errors=(400,),
            )
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
            {"name": "agent"}, {"name": "audit"}, {"name": "preference"}, {"name": "platform"}, {"name": "workspace"},
        ],
        "paths": _paths(),
        "components": {"schemas": _schemas()},
    }
