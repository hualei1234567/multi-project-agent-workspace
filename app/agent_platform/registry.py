"""资源注册中心（P3-M2）。

契约：``docs/phase3/contracts/CF-2_REGISTRY_SCHEMA_v1.md``（已冻结）。

**解决什么问题**：在此之前，系统里只有 ``project_agents``（实例），
没有任何地方能回答「系统里有哪些角色 / 技能 / 模型 / 运行时，谁能用哪个版本」。
于是调度只能按优先级排序，做不到按能力匹配；Gateway 也没法上报能力。

**核心约束**（都写进代码，不是文档）：

1. 撤销用 ``status='revoked'`` / ``revoked_at``，**不删行**——审计要能回溯。
2. 能力必须带 ``evidence``；没有依据的能力不禁止，但评分权重 ×0.5。
3. ``scan_status != 'passed'`` 的 Skill 不允许进入 ASSISTED 派发。
4. 兼容矩阵**没有记录 = 兼容**（显式宽松决策）。
5. 所有变更写 ``registry_changes``。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from . import store

#: 角色执行模式。与 project_agents.execution_mode 的真实取值一致
#: （生产库实测四种：main_session / subagent / worktree / readonly）。
#: readonly 角色不自动执行——它负责评审，自动执行会绕过人工判断。
EXECUTION_MODES = frozenset({"readonly", "worktree", "subagent", "main_session"})

#: 资源状态。deprecated 表示不推荐新用但已用的可跑完；revoked 立即失效。
ENTITY_STATUSES = frozenset({"active", "deprecated", "revoked"})

#: Skill 扫描状态。只有 passed 才允许 ASSISTED 派发。
SCAN_STATUSES = frozenset({"unscanned", "passed", "failed"})

#: 运行时状态。offline 由心跳超时计算得出，不是手动设置。
RUNTIME_STATUSES = frozenset({"unknown", "healthy", "degraded", "offline", "revoked"})

#: 没有 evidence 的能力，评分权重乘这个系数。
NO_EVIDENCE_WEIGHT = 0.5


class RegistryError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _now() -> str:
    return store.now_iso()


def _change(conn: sqlite3.Connection, entity_type: str, entity_id: str,
            action: str, *, changed_by: str, from_version: int | None = None,
            to_version: int | None = None, diff: dict | None = None) -> None:
    """写变更审计。**在调用方事务内**调用，与业务写入同生共死。"""
    conn.execute(
        "INSERT INTO registry_changes (id, entity_type, entity_id, action,"
        " from_version, to_version, diff_json, changed_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_new_id("chg"), entity_type, entity_id, action, from_version, to_version,
         store.dumps_json(diff or {}), changed_by, _now()),
    )


# ---------------------------------------------------------------------------
# Agent Role Definition
# ---------------------------------------------------------------------------
def create_role(conn: sqlite3.Connection, *, role_key: str, display_name: str,
                execution_mode: str = "readonly", description: str | None = None,
                default_model: str | None = None, source: str = "user",
                created_by: str = "local") -> dict[str, Any]:
    role_key = (role_key or "").strip()
    if not role_key:
        raise RegistryError("invalid_role_key", 400, "role_key 不能为空")
    if execution_mode not in EXECUTION_MODES:
        raise RegistryError("invalid_execution_mode", 400,
                            f"execution_mode 必须是 {sorted(EXECUTION_MODES)} 之一")
    dup = conn.execute("SELECT id FROM registry_agent_roles WHERE role_key = ?",
                       (role_key,)).fetchone()
    if dup is not None:
        raise RegistryError("role_exists", 409, f"角色 {role_key} 已存在")

    rid = f"role_{role_key}"
    now = _now()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO registry_agent_roles (id, role_key, display_name, description,"
            " execution_mode, default_model, source, version, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)",
            (rid, role_key, display_name or role_key, description, execution_mode,
             default_model, source, now, now),
        )
        _change(conn, "agent_role", rid, "create", changed_by=created_by,
                to_version=1, diff={"role_key": role_key})
    return get_role(conn, rid)


def get_role(conn: sqlite3.Connection, role_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM registry_agent_roles WHERE id = ?",
                       (role_id,)).fetchone()
    if row is None:
        raise RegistryError("not_found", 404, "角色不存在")
    out = dict(row)
    out["capabilities"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM registry_agent_capabilities WHERE role_id = ?"
            " ORDER BY proficiency DESC", (role_id,),
        )
    ]
    out["skills"] = [
        dict(r) for r in conn.execute(
            "SELECT s.* FROM registry_skills s"
            "  JOIN registry_skill_grants g ON g.skill_id = s.id"
            " WHERE g.role_id = ? AND g.revoked_at IS NULL AND s.status = 'active'",
            (role_id,),
        )
    ]
    return out


def list_roles(conn: sqlite3.Connection, *, status: str | None = None,
               execution_mode: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM registry_agent_roles WHERE 1=1"
    args: list[Any] = []
    if status:
        sql += " AND status = ?"
        args.append(status)
    if execution_mode:
        sql += " AND execution_mode = ?"
        args.append(execution_mode)
    sql += " ORDER BY role_key"
    return [dict(r) for r in conn.execute(sql, args)]


def add_capability(conn: sqlite3.Connection, *, role_id: str, capability: str,
                   proficiency: int = 1, evidence: str | None = None,
                   changed_by: str = "local") -> dict[str, Any]:
    """登记一项能力。

    ``evidence`` 为空的含义是「自称具备但拿不出依据」——不禁止登记，
    但调度评分时权重打折。这是诚实优于完美的取舍。
    """
    get_role(conn, role_id)  # 校验存在
    if not (1 <= int(proficiency) <= 5):
        raise RegistryError("invalid_proficiency", 400, "proficiency 必须在 1..5")
    cap_id = _new_id("cap")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO registry_agent_capabilities (id, role_id, capability,"
            " proficiency, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (cap_id, role_id, capability, int(proficiency), evidence, _now()),
        )
        _change(conn, "agent_capability", cap_id, "create", changed_by=changed_by,
                diff={"role_id": role_id, "capability": capability,
                      "has_evidence": bool(evidence)})
    return dict(conn.execute("SELECT * FROM registry_agent_capabilities WHERE id = ?",
                             (cap_id,)).fetchone())


def capability_score(conn: sqlite3.Connection, role_id: str,
                     capability: str) -> float:
    """某角色在某能力上的评分（0..5，无依据打折）。

    没有该项能力返回 0。这是调度「智能匹配」的输入。
    """
    row = conn.execute(
        "SELECT proficiency, evidence FROM registry_agent_capabilities"
        " WHERE role_id = ? AND capability = ?", (role_id, capability),
    ).fetchone()
    if row is None:
        return 0.0
    score = float(row["proficiency"])
    if not (row["evidence"] or "").strip():
        score *= NO_EVIDENCE_WEIGHT
    return score


# ---------------------------------------------------------------------------
# Skill Definition
# ---------------------------------------------------------------------------
def create_skill(conn: sqlite3.Connection, *, skill_key: str, display_name: str,
                 version: str = "1.0.0", risk_level: str = "R1",
                 source: str = "user", source_ref: str | None = None,
                 description: str | None = None,
                 requires_approval: bool = False,
                 changed_by: str = "local") -> dict[str, Any]:
    skill_key = (skill_key or "").strip()
    if not skill_key:
        raise RegistryError("invalid_skill_key", 400, "skill_key 不能为空")
    dup = conn.execute("SELECT id FROM registry_skills WHERE skill_key = ?",
                       (skill_key,)).fetchone()
    if dup is not None:
        raise RegistryError("skill_exists", 409, f"技能 {skill_key} 已存在")

    sid = f"skill_{skill_key}"
    now = _now()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO registry_skills (id, skill_key, display_name, description,"
            " source, source_ref, version, risk_level, requires_approval,"
            " scan_status, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unscanned', 'active', ?, ?)",
            (sid, skill_key, display_name or skill_key, description, source,
             source_ref, version, risk_level, 1 if requires_approval else 0, now, now),
        )
        _change(conn, "skill", sid, "create", changed_by=changed_by,
                diff={"skill_key": skill_key, "version": version})
    return get_skill(conn, sid)


def get_skill(conn: sqlite3.Connection, skill_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM registry_skills WHERE id = ?",
                       (skill_id,)).fetchone()
    if row is None:
        raise RegistryError("not_found", 404, "技能不存在")
    return dict(row)


def record_scan(conn: sqlite3.Connection, skill_id: str, *, passed: bool,
                detail: str = "", changed_by: str = "local") -> dict[str, Any]:
    """记录扫描结果。**未通过扫描的技能不能进入 ASSISTED 派发。**"""
    get_skill(conn, skill_id)
    status = "passed" if passed else "failed"
    with store.write_tx(conn):
        conn.execute(
            "UPDATE registry_skills SET scan_status = ?, scan_detail = ?,"
            " updated_at = ? WHERE id = ?", (status, detail[:1000], _now(), skill_id),
        )
        _change(conn, "skill", skill_id, "scan", changed_by=changed_by,
                diff={"passed": passed})
    return get_skill(conn, skill_id)


def grant_skill(conn: sqlite3.Connection, *, skill_id: str, role_id: str,
                granted_by: str = "local", note: str | None = None) -> dict[str, Any]:
    get_skill(conn, skill_id)
    get_role(conn, role_id)
    existing = conn.execute(
        "SELECT * FROM registry_skill_grants WHERE skill_id = ? AND role_id = ?",
        (skill_id, role_id),
    ).fetchone()
    if existing is not None:
        if existing["revoked_at"] is not None:
            # 重新授权：清掉撤销标记，保留原行（审计可追溯）
            with store.write_tx(conn):
                conn.execute(
                    "UPDATE registry_skill_grants SET revoked_at = NULL,"
                    " granted_by = ?, note = ?, created_at = ? WHERE id = ?",
                    (granted_by, note, _now(), existing["id"]),
                )
            return dict(conn.execute("SELECT * FROM registry_skill_grants WHERE id = ?",
                                     (existing["id"],)).fetchone())
        return dict(existing)

    gid = _new_id("grant")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO registry_skill_grants (id, skill_id, role_id, granted_by,"
            " note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (gid, skill_id, role_id, granted_by, note, _now()),
        )
        _change(conn, "skill_grant", gid, "create", changed_by=granted_by,
                diff={"skill_id": skill_id, "role_id": role_id})
    return dict(conn.execute("SELECT * FROM registry_skill_grants WHERE id = ?",
                             (gid,)).fetchone())


def revoke_skill_grant(conn: sqlite3.Connection, grant_id: str, *,
                       revoked_by: str = "local", reason: str = "") -> dict[str, Any]:
    row = conn.execute("SELECT * FROM registry_skill_grants WHERE id = ?",
                       (grant_id,)).fetchone()
    if row is None:
        raise RegistryError("not_found", 404, "授权不存在")
    if row["revoked_at"]:
        raise RegistryError("already_revoked", 409, "该授权已撤销")
    with store.write_tx(conn):
        conn.execute("UPDATE registry_skill_grants SET revoked_at = ?, note = ?"
                     " WHERE id = ?", (_now(), reason, grant_id))
        _change(conn, "skill_grant", grant_id, "revoke", changed_by=revoked_by,
                diff={"reason": reason})
    return dict(conn.execute("SELECT * FROM registry_skill_grants WHERE id = ?",
                             (grant_id,)).fetchone())


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
def create_model(conn: sqlite3.Connection, *, model_key: str, provider: str,
                 protocol: str = "openai", context_window: int | None = None,
                 max_output: int | None = None, supports_tools: bool = True,
                 supports_vision: bool = False,
                 changed_by: str = "local") -> dict[str, Any]:
    model_key = (model_key or "").strip()
    if not model_key:
        raise RegistryError("invalid_model_key", 400, "model_key 不能为空")
    dup = conn.execute("SELECT id FROM registry_models WHERE model_key = ?",
                       (model_key,)).fetchone()
    if dup is not None:
        raise RegistryError("model_exists", 409, f"模型 {model_key} 已存在")

    mid = f"model_{model_key}"
    now = _now()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO registry_models (id, model_key, provider, protocol,"
            " context_window, max_output, supports_tools, supports_vision,"
            " status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (mid, model_key, provider, protocol, context_window, max_output,
             1 if supports_tools else 0, 1 if supports_vision else 0, now, now),
        )
        _change(conn, "model", mid, "create", changed_by=changed_by,
                diff={"model_key": model_key, "provider": provider})
    return dict(conn.execute("SELECT * FROM registry_models WHERE id = ?",
                             (mid,)).fetchone())


def list_models(conn: sqlite3.Connection, *, status: str | None = None
                ) -> list[dict[str, Any]]:
    sql = "SELECT * FROM registry_models WHERE 1=1"
    args: list[Any] = []
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY provider, model_key"
    return [dict(r) for r in conn.execute(sql, args)]


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
def create_runtime(conn: sqlite3.Connection, *, runtime_key: str, kind: str = "local",
                   device_name: str | None = None, capabilities: list[str] | None = None,
                   max_concurrency: int = 1, heartbeat_ttl_s: int = 120,
                   service_id: str | None = None,
                   changed_by: str = "local") -> dict[str, Any]:
    runtime_key = (runtime_key or "").strip()
    if not runtime_key:
        raise RegistryError("invalid_runtime_key", 400, "runtime_key 不能为空")
    dup = conn.execute("SELECT id FROM registry_runtimes WHERE runtime_key = ?",
                       (runtime_key,)).fetchone()
    if dup is not None:
        raise RegistryError("runtime_exists", 409, f"运行时 {runtime_key} 已存在")

    rid = f"rt_{runtime_key}"
    now = _now()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO registry_runtimes (id, runtime_key, service_id, kind,"
            " device_name, capabilities, max_concurrency, heartbeat_ttl_s,"
            " allocatable, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'unknown', ?, ?)",
            (rid, runtime_key, service_id, kind, device_name,
             store.dumps_json(capabilities or []), int(max_concurrency),
             int(heartbeat_ttl_s), now, now),
        )
        _change(conn, "runtime", rid, "create", changed_by=changed_by,
                diff={"runtime_key": runtime_key, "kind": kind})
    return get_runtime(conn, rid)


def heartbeat(conn: sqlite3.Connection, runtime_id: str) -> dict[str, Any]:
    """设备心跳。状态 healthy 由心跳推断，不靠人工设置。"""
    get_runtime(conn, runtime_id)
    with store.write_tx(conn):
        conn.execute(
            "UPDATE registry_runtimes SET heartbeat_at = ?, status = 'healthy',"
            " updated_at = ? WHERE id = ?", (_now(), _now(), runtime_id),
        )
    return get_runtime(conn, runtime_id)


def get_runtime(conn: sqlite3.Connection, runtime_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM registry_runtimes WHERE id = ?",
                       (runtime_id,)).fetchone()
    if row is None:
        raise RegistryError("not_found", 404, "运行时不存")
    out = dict(row)
    out["effective_status"] = _effective_status(out)
    out["active_slots"] = _active_slots(conn, runtime_id)
    return out


def _effective_status(rt: dict[str, Any]) -> str:
    """计算有效状态。心跳超时 → offline。

    为什么不直接读 status：采集线程如果挂了，status 会永远停在 healthy。
    读取时计算才不会骗人。
    """
    if rt["status"] == "revoked":
        return "revoked"
    hb = rt.get("heartbeat_at")
    if not hb:
        return "unknown"
    from datetime import datetime, timezone
    try:
        last = datetime.fromisoformat(hb)
    except ValueError:
        return "unknown"
    ttl = int(rt.get("heartbeat_ttl_s") or 120)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age > ttl:
        return "offline"
    return rt["status"]


def _active_slots(conn: sqlite3.Connection, runtime_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(slots), 0) AS s FROM registry_runtime_leases"
        " WHERE runtime_id = ? AND released_at IS NULL AND expires_at > ?",
        (runtime_id, _now()),
    ).fetchone()
    return int(row["s"] or 0)


def revoke_runtime(conn: sqlite3.Connection, runtime_id: str, *,
                   revoked_by: str = "local", reason: str = "") -> dict[str, Any]:
    """撤销设备。撤销后不能执行任何命令（P3-M8 安全清单要求）。"""
    get_runtime(conn, runtime_id)
    with store.write_tx(conn):
        conn.execute(
            "UPDATE registry_runtimes SET status = 'revoked', allocatable = 0,"
            " updated_at = ? WHERE id = ?", (_now(), runtime_id),
        )
        _change(conn, "runtime", runtime_id, "revoke", changed_by=revoked_by,
                diff={"reason": reason})
    return get_runtime(conn, runtime_id)


def acquire_lease(conn: sqlite3.Connection, *, runtime_id: str, project_id: str,
                  holder: str, slots: int = 1,
                  ttl_seconds: int = 3600) -> dict[str, Any]:
    """申请运行时租约。超过并发上限或运行时不可用即拒绝。"""
    rt = get_runtime(conn, runtime_id)
    if rt["effective_status"] == "revoked":
        raise RegistryError("runtime_revoked", 409, "运行时已撤销")
    if not rt["allocatable"]:
        raise RegistryError("runtime_not_allocatable", 409, "运行时不接受分配")
    used = rt["active_slots"]
    if used + slots > int(rt["max_concurrency"]):
        raise RegistryError(
            "concurrency_exceeded", 409,
            f"运行时并发已满：{used}/{rt['max_concurrency']}，本次申请 {slots}",
        )

    from datetime import datetime, timedelta, timezone
    lid = _new_id("lease")
    now = _now()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
               ).isoformat(timespec="seconds")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO registry_runtime_leases (id, runtime_id, project_id,"
            " holder, slots, acquired_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lid, runtime_id, project_id, holder, int(slots), now, expires),
        )
    return dict(conn.execute("SELECT * FROM registry_runtime_leases WHERE id = ?",
                             (lid,)).fetchone())


def release_lease(conn: sqlite3.Connection, lease_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM registry_runtime_leases WHERE id = ?",
                       (lease_id,)).fetchone()
    if row is None:
        raise RegistryError("not_found", 404, "租约不存在")
    if row["released_at"]:
        return dict(row)  # 幂等
    with store.write_tx(conn):
        conn.execute("UPDATE registry_runtime_leases SET released_at = ? WHERE id = ?",
                     (_now(), lease_id))
    return dict(conn.execute("SELECT * FROM registry_runtime_leases WHERE id = ?",
                             (lease_id,)).fetchone())


# ---------------------------------------------------------------------------
# 兼容矩阵
# ---------------------------------------------------------------------------
def set_compatibility(conn: sqlite3.Connection, *, subject_type: str, subject_id: str,
                      target_type: str, target_id: str, compatible: bool,
                      reason: str | None = None,
                      changed_by: str = "local") -> dict[str, Any]:
    """显式登记兼容/不兼容。没有记录的组合视为兼容。"""
    cid = _new_id("compat")
    now = _now()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO registry_compat_matrix (id, subject_type, subject_id,"
            " target_type, target_id, compatible, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(subject_type, subject_id, target_type, target_id)"
            " DO UPDATE SET compatible = excluded.compatible,"
            "  reason = excluded.reason",
            (cid, subject_type, subject_id, target_type, target_id,
             1 if compatible else 0, reason, now),
        )
        _change(conn, "compat", cid, "set", changed_by=changed_by,
                diff={"subject": f"{subject_type}:{subject_id}",
                      "target": f"{target_type}:{target_id}",
                      "compatible": compatible})
    return dict(conn.execute(
        "SELECT * FROM registry_compat_matrix WHERE subject_type = ? AND subject_id = ?"
        "  AND target_type = ? AND target_id = ?",
        (subject_type, subject_id, target_type, target_id)).fetchone())


def is_compatible(conn: sqlite3.Connection, *, subject_type: str, subject_id: str,
                  target_type: str, target_id: str) -> tuple[bool, str]:
    """兼容判定。**没有记录 = 兼容**（显式宽松决策，见契约 §2.8）。"""
    row = conn.execute(
        "SELECT compatible, reason FROM registry_compat_matrix"
        " WHERE subject_type = ? AND subject_id = ? AND target_type = ? AND target_id = ?",
        (subject_type, subject_id, target_type, target_id),
    ).fetchone()
    if row is None:
        return True, "no_record_defaults_compatible"
    return bool(row["compatible"]), (row["reason"] or "")


# ---------------------------------------------------------------------------
# 调度前置硬过滤
# ---------------------------------------------------------------------------
def dispatch_precheck(conn: sqlite3.Connection, *, role_id: str,
                      skill_ids: list[str] | None = None,
                      runtime_id: str | None = None) -> dict[str, Any]:
    """派发前的硬过滤。返回 {allowed, blockers, warnings}。

    **这是硬约束，不是建议**：任一 blocker 存在都不允许派发。
    """
    blockers: list[str] = []
    warnings: list[str] = []

    role = get_role(conn, role_id)
    if role["status"] == "revoked":
        blockers.append("role_revoked")
    elif role["status"] == "deprecated":
        warnings.append("role_deprecated")
    if role["execution_mode"] == "readonly":
        blockers.append("role_readonly_cannot_execute")

    for sid in (skill_ids or []):
        skill = conn.execute("SELECT * FROM registry_skills WHERE id = ?",
                             (sid,)).fetchone()
        if skill is None:
            blockers.append(f"skill_missing:{sid}")
            continue
        skill = dict(skill)
        if skill["status"] == "revoked":
            blockers.append(f"skill_revoked:{sid}")
        if skill["scan_status"] != "passed":
            blockers.append(f"skill_not_scanned:{sid}")
        granted = conn.execute(
            "SELECT 1 FROM registry_skill_grants WHERE skill_id = ? AND role_id = ?"
            "  AND revoked_at IS NULL", (sid, role_id),
        ).fetchone()
        if granted is None:
            blockers.append(f"skill_not_granted:{sid}")
        ok, why = is_compatible(conn, subject_type="agent_role", subject_id=role_id,
                                target_type="skill", target_id=sid)
        if not ok:
            blockers.append(f"incompatible_skill:{sid}:{why}")

    if runtime_id:
        rt = get_runtime(conn, runtime_id)
        if rt["effective_status"] == "revoked":
            blockers.append("runtime_revoked")
        elif rt["effective_status"] == "offline":
            blockers.append("runtime_offline")
        elif rt["effective_status"] == "unknown":
            warnings.append("runtime_unknown_status")
        if rt["active_slots"] >= int(rt["max_concurrency"]):
            blockers.append("runtime_concurrency_full")
        ok, why = is_compatible(conn, subject_type="agent_role", subject_id=role_id,
                                target_type="runtime", target_id=runtime_id)
        if not ok:
            blockers.append(f"incompatible_runtime:{why}")

    return {"allowed": not blockers, "blockers": blockers, "warnings": warnings}


# ---------------------------------------------------------------------------
# 汇总视图
# ---------------------------------------------------------------------------
def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Registry 总览：各类资源计数 + 需要关注的问题。"""
    def count(sql: str, *args: Any) -> int:
        row = conn.execute(sql, args).fetchone()
        return int(row[0] or 0)

    return {
        "roles": {
            "total": count("SELECT COUNT(*) FROM registry_agent_roles"),
            "active": count("SELECT COUNT(*) FROM registry_agent_roles WHERE status='active'"),
            "executable": count(
                "SELECT COUNT(*) FROM registry_agent_roles"
                " WHERE status='active'"
                "   AND execution_mode IN ('worktree','subagent','main_session')"),
        },
        "skills": {
            "total": count("SELECT COUNT(*) FROM registry_skills"),
            "scanned_passed": count(
                "SELECT COUNT(*) FROM registry_skills WHERE scan_status='passed'"),
            "unscanned": count(
                "SELECT COUNT(*) FROM registry_skills WHERE scan_status='unscanned'"),
        },
        "models": {
            "total": count("SELECT COUNT(*) FROM registry_models"),
            "active": count("SELECT COUNT(*) FROM registry_models WHERE status='active'"),
        },
        "runtimes": {
            "total": count("SELECT COUNT(*) FROM registry_runtimes"),
            "revoked": count("SELECT COUNT(*) FROM registry_runtimes WHERE status='revoked'"),
        },
        "attention": {
            # 这些是"需要人看一眼"的信号，不是错误
            "roles_without_capabilities": count(
                "SELECT COUNT(*) FROM registry_agent_roles r"
                " WHERE r.status='active' AND NOT EXISTS ("
                "   SELECT 1 FROM registry_agent_capabilities c WHERE c.role_id = r.id)"),
            "unscanned_skills": count(
                "SELECT COUNT(*) FROM registry_skills WHERE scan_status='unscanned'"
                "  AND status='active'"),
        },
    }
