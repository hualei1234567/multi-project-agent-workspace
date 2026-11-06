"""Policy Engine：唯一执行权限边界（ADR-P4-012，P4-AUT-005）。

V6.0 的硬规则：**自动化资格不等于执行授权**，Policy 才是最终判定点。
因此这里不接受"我已经被评估为可自动执行"这类输入，只接受
(主体, 项目, 资源, 动作, 环境, 风险) 六元组。

判定顺序（任一拒绝即终止）：

1. Kill Switch —— 全局 / 项目 / 项目域，秒级生效；
2. 主体有效性 —— 状态与有效期；
3. 显式 deny 规则 —— 优先级最高，用于兜底拦截；
4. RBAC —— 项目成员关系与角色；
5. 显式 allow 规则 —— 收紧到资源/动作/环境/风险；
6. 审批要求 —— 命中 ``requires_approval`` 则返回待审批。

无任何 allow 规则命中时按 RBAC 结论走；RBAC 也拒绝就拒绝。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import autonomy, governance, killswitch

RISK_ORDER = {"R0": 0, "R1": 1, "R2": 2, "R3": 3, "R4": 4}


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    requires_approval: bool = False
    matched_rules: list[str] = field(default_factory=list)
    blocked_by: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "requires_approval": self.requires_approval,
            "matched_rules": self.matched_rules,
            "blocked_by": self.blocked_by,
        }


class PolicyDenied(Exception):
    def __init__(self, reason: str, *, requires_approval: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.requires_approval = requires_approval


def _rule_matches(rule: dict[str, Any], *, principal: dict[str, Any] | None,
                  project_id: str | None, resource_type: str, action: str,
                  environment: str, risk: str) -> bool:
    if not rule["enabled"]:
        return False
    if rule["scope"] == "project" and rule["project_id"] != project_id:
        return False
    if rule["principal_id"] and (principal is None or rule["principal_id"] != principal["id"]):
        return False
    if rule["principal_type"] and (principal is None or rule["principal_type"] != principal["principal_type"]):
        return False
    if rule["resource_type"] not in {"*", resource_type}:
        return False
    if rule["action"] not in {"*", action}:
        return False
    if rule["environment"] not in {"*", environment}:
        return False
    allowed_risk = RISK_ORDER.get(rule["max_risk"], 4)
    return RISK_ORDER.get(risk, 4) <= allowed_risk


def _rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM policy_rules WHERE enabled = 1 ORDER BY effect, created_at").fetchall()
    return [dict(row) for row in rows]


def decide(
    conn: sqlite3.Connection,
    *,
    principal: dict[str, Any] | None,
    project_id: str | None,
    resource_type: str,
    action: str,
    environment: str = "dev",
    risk: str = "R2",
    domain: str | None = None,
    task_type: str | None = None,
    required_automation: str | None = None,
) -> Decision:
    """唯一判定入口。任何写操作、执行、导出、检索都必须先过这里。

    ``required_automation`` 表示该操作要求的自动化等级（A1–A4）。
    资格不够直接拒绝——资格不等于授权，但也不允许绕过资格。
    """
    blocked, why = killswitch.is_blocked(conn, project_id=project_id, domain=domain)
    if blocked:
        return Decision(allowed=False, reason=why, blocked_by="kill_switch")

    ok, reason = governance.principal_active(principal)
    if not ok:
        return Decision(allowed=False, reason=reason, blocked_by="principal")

    if required_automation and project_id:
        current = autonomy.current_level(conn, project_id, task_type or "default", environment)
        if not autonomy.level_satisfies(current, required_automation):
            return Decision(
                allowed=False,
                reason=f"自动化等级不足：当前 {current}，要求 {required_automation}",
                blocked_by="automation",
            )

    matched: list[str] = []
    deny_hit = None
    allow_needs_approval = False
    for rule in _rules(conn):
        if not _rule_matches(
            rule, principal=principal, project_id=project_id,
            resource_type=resource_type, action=action,
            environment=environment, risk=risk,
        ):
            continue
        matched.append(rule["id"])
        if rule["effect"] == "deny":
            deny_hit = rule
            break
        if rule["requires_approval"]:
            allow_needs_approval = True

    if deny_hit is not None:
        return Decision(
            allowed=False,
            reason=f"命中禁止规则：{deny_hit['note'] or deny_hit['id']}",
            matched_rules=matched, blocked_by="policy_rule",
        )

    allowed, rbac_reason = governance.authorize(
        conn, principal=principal, project_id=project_id,
        resource_type=resource_type, action=action,
    )
    if not allowed:
        return Decision(allowed=False, reason=rbac_reason,
                        matched_rules=matched, blocked_by="rbac")

    return Decision(allowed=True, requires_approval=allow_needs_approval,
                    matched_rules=matched)


def require(
    conn: sqlite3.Connection,
    *,
    principal: dict[str, Any] | None,
    project_id: str | None,
    resource_type: str,
    action: str,
    environment: str = "dev",
    risk: str = "R2",
    domain: str | None = None,
    approval_satisfied: bool = False,
) -> Decision:
    """判定并要求通过。需要审批而未满足时抛 PolicyDenied(requires_approval=True)。"""
    decision = decide(
        conn, principal=principal, project_id=project_id,
        resource_type=resource_type, action=action,
        environment=environment, risk=risk, domain=domain,
    )
    if not decision.allowed:
        raise PolicyDenied(decision.reason)
    if decision.requires_approval and not approval_satisfied:
        raise PolicyDenied("需要审批", requires_approval=True)
    return decision
