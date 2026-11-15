"""统一调度门面（P3-M3，ADR-P3-002）。

**为什么需要它**：平台原先有两条独立构造 `claude` 命令的路径：

* ``executor.py`` —— 走 Policy + 审批，受控；
* ``team.py`` —— 直接 `_run()`，只套了工具白名单，**不过 Policy、不过审批**。

蓝图 §2 点名这是「旧入口绕过 Facade」。本模块提供唯一的入口，
两条路径都从这里过；Policy 判定与审计在门面内统一执行。

设计原则：

1. **Facade 不改上层签名**。`team.py` 的调用方看到的接口不变。
2. **Policy 判定在这里做，不在调用方做**——调用方忘了就是漏洞，
   门面忘了是 bug（有测试盯着）。
3. **拒绝要有明确原因**，不能只是 False。
"""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from typing import Any

from . import store
from .p4 import killswitch, policy


class FacadeError(Exception):
    def __init__(self, code: str, status: int = 403, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


#: 门面认可的入口。每个入口对应一个 Policy 的 resource_type/action。
ENTRY_POLICY = {
    # 后台会话启动：风险 R3（会起真实 claude 进程）
    "team.launch": ("exec_runtime", "run", "R3"),
    # 调度项执行：风险 R3
    "dispatch.run": ("dispatch_item", "run", "R3"),
}

#: 不需要 Policy 的只读入口（预检、列表）。
READONLY_ENTRIES = frozenset({"team.preflight"})


def guard(
    conn: sqlite3.Connection,
    *,
    entry: str,
    project_id: str | None,
    principal: dict[str, Any] | None,
) -> dict[str, Any]:
    """门面统一闸门。不通过就抛 FacadeError。

    调用方**必须**调这个函数再往下走——这是唯一入口。
    """
    if entry in READONLY_ENTRIES:
        return {"entry": entry, "policy": "skipped_readonly"}

    spec = ENTRY_POLICY.get(entry)
    if spec is None:
        raise FacadeError("unknown_entry", 400, f"未知入口：{entry}")
    resource_type, action, risk = spec

    # Kill Switch 优先——秒级停止必须比任何判定都早。
    blocked, why = killswitch.is_blocked(conn, project_id=project_id)
    if blocked:
        raise FacadeError("kill_switch_active", 403, why)

    decision = policy.decide(
        conn,
        principal=principal,
        project_id=project_id,
        resource_type=resource_type,
        action=action,
        risk=risk,
    )
    if not decision.allowed:
        raise FacadeError("policy_denied", 403, decision.reason)

    return {
        "entry": entry,
        "policy": "allowed",
        "requires_approval": decision.requires_approval,
        "matched_rules": decision.matched_rules,
    }


def team_launch_argv(policy_args: list[str], *, agent: str | None,
                     prompt: str) -> list[str]:
    """后台会话的 argv 构造。收敛到这里，`team.py` 不再自己拼。

    ``policy_args`` 来自 ``executor_policy.bg_policy_args()``——
    工具白名单仍由那里定义，门面只负责统一出口。
    """
    binary = shutil.which("claude") or ""
    args = [binary]
    if agent:
        args += ["--agent", agent]
    args += ["--bg", *policy_args, prompt]
    return args


def audit_denial(conn: sqlite3.Connection, *, entry: str, project_id: str | None,
                 actor: str, reason: str) -> None:
    """记录被拒绝的启动尝试。

    拒绝路径也必须留痕——否则"谁在什么时候试图绕过"查不出来。
    """
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO audit_logs (id, project_id, actor, action,"
                " resource_type, resource_id, result, detail_json, created_at)"
                " VALUES (?, ?, ?, ?, 'facade_entry', ?, 'denied', ?, ?)",
                (uuid.uuid4().hex, project_id, actor, f"facade.denied:{entry}",
                 entry, store.dumps_json({"reason": reason}), store.now_iso()),
            )
    except sqlite3.DatabaseError:
        # 审计写入失败不能反过来改变判定结果——拒绝就是拒绝。
        # 但也不吞掉：调用方已经抛了 FacadeError，这里静默即可。
        return
