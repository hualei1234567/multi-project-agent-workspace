"""内置团队模板（角色 key 稳定；前端从 API 读取，不写死）。

模板字段依据 docs/110 §8.1：
role_key / display_name / responsibility / layer_order / sort_order /
parent_role_key / agent_type / execution_mode / skills / permission_profile / is_required
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import store

STANDARD_1_6 = "standard-1-6"
LEAN_1_3 = "lean-1-3"


def _role(
    role_key: str,
    display_name: str,
    responsibility: str,
    layer_order: int,
    sort_order: int,
    parent_role_key: str | None,
    agent_type: str,
    execution_mode: str,
    skills: list[str],
    is_required: bool = True,
) -> dict[str, Any]:
    return {
        "role_key": role_key,
        "display_name": display_name,
        "responsibility": responsibility,
        "layer_order": layer_order,
        "sort_order": sort_order,
        "parent_role_key": parent_role_key,
        "agent_type": agent_type,
        "execution_mode": execution_mode,
        "skills": skills,
        "permission_profile": {},
        "is_required": is_required,
    }


STANDARD_ROLES: list[dict[str, Any]] = [
    _role(
        "general-owner", "General Owner",
        "需求理解、任务拆解、组织执行、冲突裁决与最终交付",
        1, 0, None, "main", "main_session",
        ["team-dispatch", "api-contract", "qa-acceptance", "gitlab-delivery"],
    ),
    _role(
        "product-manager", "Product Manager",
        "需求分析、PRD、优先级与验收标准（只读）",
        2, 10, "general-owner", "subagent", "subagent",
        ["team-dispatch", "ddp-memory"],
    ),
    _role(
        "system-architect", "System Architect",
        "架构分析、技术方案、接口与数据设计（只读）",
        2, 20, "general-owner", "subagent", "subagent",
        ["ddp-architecture-planning", "api-contract", "ddp-traceability"],
    ),
    _role(
        "ux-designer", "UX Designer",
        "页面设计、交互与组件规范（只读）",
        2, 30, "general-owner", "subagent", "subagent",
        ["ddp-ui-design", "frontend-design", "ui-ux-pro-max"],
    ),
    _role(
        "frontend-engineer", "Frontend Engineer",
        "页面、组件、接口接入与前端测试（独立 worktree）",
        3, 10, "general-owner", "subagent", "worktree",
        ["tdd", "frontend-design", "vercel-react-best-practices"],
    ),
    _role(
        "backend-engineer", "Backend Engineer",
        "API、服务、模型、迁移与后端测试（独立 worktree）",
        3, 20, "general-owner", "subagent", "worktree",
        ["tdd", "ddp-codebase-analysis", "api-contract", "diagnosing-bugs"],
    ),
    _role(
        "qa-devops", "QA & DevOps",
        "测试策略、验收结论、CI 与发布流水线（独立 worktree / 只读审查）",
        3, 30, "general-owner", "subagent", "readonly",
        ["qa-acceptance", "gitlab-delivery", "security-review"],
    ),
]

LEAN_ROLES: list[dict[str, Any]] = [
    _role(
        "general-owner", "General Owner",
        "需求理解、任务拆解与交付汇总",
        1, 0, None, "main", "main_session",
        ["team-dispatch", "api-contract"],
    ),
    _role(
        "fullstack-engineer", "Fullstack Engineer",
        "前后端实现、接口接入与自测（独立 worktree）",
        2, 10, "general-owner", "subagent", "worktree",
        ["tdd", "codebase-design", "diagnosing-bugs"],
    ),
    _role(
        "qa-engineer", "QA Engineer",
        "测试策略、接口/集成/E2E 与验收结论",
        2, 20, "general-owner", "subagent", "worktree",
        ["qa-acceptance", "gitlab-delivery"],
    ),
    _role(
        "product-architecture", "Product / Architecture",
        "产品与架构方案（可配置、可移除）",
        2, 30, "general-owner", "subagent", "readonly",
        ["team-dispatch", "ddp-architecture-planning", "api-contract"],
        is_required=False,
    ),
]

TEMPLATES: dict[str, dict[str, Any]] = {
    STANDARD_1_6: {
        "template_key": STANDARD_1_6,
        "version": 1,
        "name": "标准 1+6",
        "description": "General Owner + 产品/架构/UX + 前端/后端/QA，DDP 当前使用",
        "roles": STANDARD_ROLES,
    },
    LEAN_1_3: {
        "template_key": LEAN_1_3,
        "version": 1,
        "name": "精简 1+3",
        "description": "Owner + Fullstack + QA + Product/Architecture（可配置），适合小型项目",
        "roles": LEAN_ROLES,
    },
}


def list_templates() -> list[dict[str, Any]]:
    return [dict(item) for item in TEMPLATES.values()]


def get_template(template_key: str) -> dict[str, Any] | None:
    item = TEMPLATES.get(template_key)
    return dict(item) if item else None


def template_roles(template_key: str) -> list[dict[str, Any]]:
    item = TEMPLATES.get(template_key)
    return [dict(role) for role in item["roles"]] if item else []


def ensure_templates(conn: sqlite3.Connection) -> None:
    """内置模板幂等写入；不覆盖同 key/version 的已有记录。"""
    now = store.now_iso()
    for item in TEMPLATES.values():
        conn.execute(
            """
            INSERT INTO agent_team_templates
                (id, template_key, version, name, description, roles_json, is_system, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(template_key, version) DO NOTHING
            """,
            (
                f"tpl:{item['template_key']}:v{item['version']}",
                item["template_key"],
                item["version"],
                item["name"],
                item["description"],
                store.dumps_json(item["roles"]),
                now,
            ),
        )


def load_template(conn: sqlite3.Connection, template_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM agent_team_templates
        WHERE template_key = ?
        ORDER BY version DESC LIMIT 1
        """,
        (template_key,),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["roles"] = store.loads_json(data.pop("roles_json"), [])
    return data
