"""身份、角色与项目级授权（ADR-P4-003，风险 P4-R19）。

现状问题：第三期只有"本机/远端"二值判断，持令牌者可读写全部项目。
V6.0 §32.1 的「未授权跨项目读取/写入 = 0」在当前实现下**无法证明**。

本模块提供最小可用的主体 + 成员 + 授权判定：

* 主体（principal）：user / agent / service，有状态与有效期；
* 成员（project_members）：主体在某项目中的角色；
* 角色只有四类：owner / operator / viewer / auditor；
* 授权一律 **Deny by Default**：没有明确允许就是拒绝。

主体解析规则（不改变本机体验）：

* 回环来源 → 内置主体 ``local-owner``（平台所有者，v4 迁移时建好并对已有项目授权）；
* 远端且令牌正确 → 内置主体 ``remote-token``，**默认不是任何项目成员**；
  需要使用时由所有者显式加成员，这本身就是一次授权动作。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .. import store

#: 内置主体 ID。迁移时创建，不允许删除。
LOCAL_OWNER = "local-owner"
REMOTE_TOKEN = "remote-token"

#: 角色 → 资源类型 → 允许的动作集合。``*`` 表示该资源下的任意动作。
ROLE_PERMISSIONS: dict[str, dict[str, frozenset[str]]] = {
    "owner": {"*": frozenset({"*"})},
    "operator": {
        "*": frozenset({"read", "list", "create", "update"}),
        "task": frozenset({"read", "list", "create", "update", "transition"}),
        "dispatch_item": frozenset({"read", "list", "run", "cancel"}),
        "runtime_action": frozenset({"read", "list", "preflight", "run"}),
        "evaluation_run": frozenset({"read", "list", "create", "cancel"}),
        "workflow_test_run": frozenset({"read", "list", "create"}),
        "improvement_case": frozenset({"read", "list", "create", "update"}),
    },
    "viewer": {
        "*": frozenset({"read", "list"}),
    },
    "auditor": {
        "*": frozenset(),
        "audit_log": frozenset({"read", "list"}),
        "execution": frozenset({"read", "list"}),
        "outcome_record": frozenset({"read", "list"}),
        "policy_rule": frozenset({"read", "list"}),
        "approval": frozenset({"read", "list"}),
    },
}

VALID_ROLES = frozenset(ROLE_PERMISSIONS)


class AuthorizationError(Exception):
    """授权失败；HTTP 层映射为 403。"""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason


def get_principal(conn: sqlite3.Connection, principal_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM principals WHERE id = ?", (principal_id,)).fetchone()
    return dict(row) if row else None


def list_principals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM principals ORDER BY created_at").fetchall()
    return [dict(row) for row in rows]


def create_principal(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    principal_type: str,
    display_name: str,
    is_platform_owner: bool = False,
    agent_project_id: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    if principal_type not in {"user", "agent", "service"}:
        raise ValueError(f"非法 principal_type：{principal_type}")
    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO principals (id, principal_type, display_name, is_platform_owner,"
            " agent_project_id, status, expires_at, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (principal_id, principal_type, display_name, 1 if is_platform_owner else 0,
             agent_project_id, expires_at, now, now),
        )
    result = get_principal(conn, principal_id)
    assert result is not None
    return result


def set_member(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    principal_id: str,
    role: str,
) -> dict[str, Any]:
    if role not in VALID_ROLES:
        raise ValueError(f"非法角色：{role}")
    now = store.now_iso()
    member_id = store.stable_id(project_id, principal_id)
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO project_members (id, project_id, principal_id, role, created_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(project_id, principal_id) DO UPDATE SET role = excluded.role",
            (member_id, project_id, principal_id, role, now),
        )
    return {"project_id": project_id, "principal_id": principal_id, "role": role}


def remove_member(conn: sqlite3.Connection, *, project_id: str, principal_id: str) -> bool:
    with store.write_tx(conn):
        cur = conn.execute(
            "DELETE FROM project_members WHERE project_id = ? AND principal_id = ?",
            (project_id, principal_id),
        )
    return max(cur.rowcount, 0) > 0


def list_members(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT m.*, p.display_name, p.principal_type FROM project_members m"
        " JOIN principals p ON p.id = m.principal_id"
        " WHERE m.project_id = ? ORDER BY m.created_at",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def resolve_principal(conn: sqlite3.Connection, *, is_loopback: bool) -> dict[str, Any] | None:
    """按来源解析主体。远端目前映射到 ``remote-token``（无默认项目成员）。"""
    return get_principal(conn, LOCAL_OWNER if is_loopback else REMOTE_TOKEN)


def principal_active(principal: dict[str, Any] | None) -> tuple[bool, str]:
    if principal is None:
        return False, "unknown_principal"
    if principal["status"] != "active":
        return False, f"principal_{principal['status']}"
    expires = principal.get("expires_at")
    if expires and expires <= store.now_iso():
        return False, "principal_expired"
    return True, ""


def role_in_project(conn: sqlite3.Connection, principal_id: str, project_id: str) -> str | None:
    row = conn.execute(
        "SELECT role FROM project_members WHERE project_id = ? AND principal_id = ?",
        (project_id, principal_id),
    ).fetchone()
    return row["role"] if row else None


def _role_allows(role: str, resource_type: str, action: str) -> bool:
    table = ROLE_PERMISSIONS.get(role, {})
    for grant_type in (resource_type, "*"):
        actions = table.get(grant_type)
        if actions is None:
            continue
        if "*" in actions or action in actions:
            return True
    return False


def authorize(
    conn: sqlite3.Connection,
    *,
    principal: dict[str, Any] | None,
    project_id: str | None,
    resource_type: str,
    action: str,
) -> tuple[bool, str]:
    """RBAC 判定。返回 (允许, 拒绝原因)。

    平台所有者可以跨项目，但项目数据仍受数据分类与 Policy 约束
    （V6.0 §7.4：Platform Owner 不自动获得所有项目 Secret 或原始文件访问权）。
    """
    ok, reason = principal_active(principal)
    if not ok:
        return False, reason
    assert principal is not None

    if principal["is_platform_owner"]:
        return True, ""

    if not project_id:
        return False, "project_scope_required"

    # Agent 主体必须绑定同一项目，防止 Agent 跨项目读取。
    bound = principal.get("agent_project_id")
    if principal["principal_type"] == "agent" and bound and bound != project_id:
        return False, "agent_project_mismatch"

    role = role_in_project(conn, principal["id"], project_id)
    if role is None:
        return False, "not_a_project_member"
    if not _role_allows(role, resource_type, action):
        return False, f"role_{role}_cannot_{action}_{resource_type}"
    return True, ""


def require(
    conn: sqlite3.Connection,
    *,
    principal: dict[str, Any] | None,
    project_id: str | None,
    resource_type: str,
    action: str,
) -> None:
    allowed, reason = authorize(
        conn, principal=principal, project_id=project_id,
        resource_type=resource_type, action=action,
    )
    if not allowed:
        raise AuthorizationError(reason, f"无权执行该操作（{reason}）")


def new_principal_id(prefix: str = "pr") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"
