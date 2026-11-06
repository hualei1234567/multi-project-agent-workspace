"""自主化域 V1：A0–A4 资格与等级变更（P4-M7）。

关键区分（V6.0 §18.1）：

* **资格（Eligibility）** 只说明"有证据支持更高自动化"；
* **授权（Policy）** 才决定这次能不能跑；
* 资格过期自动失效；P0 阶段生产环境上限 **A2**。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .. import store
from . import events

LEVELS = ("A0", "A1", "A2", "A3", "A4")
LEVEL_ORDER = {name: index for index, name in enumerate(LEVELS)}

#: P0 阶段允许的最高自动化等级。
P0_MAX_LEVEL = "A2"


class AutonomyError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def get_or_create(conn: sqlite3.Connection, project_id: str, *,
                  task_type: str, tool_scope: str = "read_write_code",
                  environment: str = "dev") -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM autonomy_eligibilities WHERE project_id = ? AND task_type = ?"
        " AND tool_scope = ? AND environment = ?",
        (project_id, task_type, tool_scope, environment),
    ).fetchone()
    if row is not None:
        return dict(row)
    eligibility_id = _new_id("aut")
    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO autonomy_eligibilities (id, project_id, task_type, tool_scope,"
            " environment, level, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'A0', 'not_assessed', ?, ?)",
            (eligibility_id, project_id, task_type, tool_scope, environment, now, now),
        )
    return get(conn, eligibility_id)


def get(conn: sqlite3.Connection, eligibility_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM autonomy_eligibilities WHERE id = ?", (eligibility_id,)
    ).fetchone()
    if row is None:
        raise AutonomyError("not_found", 404, "资格记录不存在")
    return dict(row)


def list_for_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM autonomy_eligibilities WHERE project_id = ? ORDER BY updated_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


def submit_evidence(conn: sqlite3.Connection, eligibility_id: str,
                    evidence: dict[str, Any]) -> dict[str, Any]:
    """提交多维证据（质量/安全/成本/稳定性），进入待评审。"""
    if not evidence:
        raise AutonomyError("evidence_required", 422, "证据不能为空")
    record = get(conn, eligibility_id)
    if record["status"] == "revoked":
        raise AutonomyError("invalid_state", 409, "资格已撤销")
    with store.write_tx(conn):
        conn.execute(
            "UPDATE autonomy_eligibilities SET evidence_json = ?,"
            " status = 'eligible_for_review', updated_at = ? WHERE id = ?",
            (store.dumps_json(evidence), store.now_iso(), eligibility_id),
        )
    return get(conn, eligibility_id)


def approve_level(conn: sqlite3.Connection, eligibility_id: str, *,
                  level: str, approved_by: str, reason: str) -> dict[str, Any]:
    if level not in LEVELS:
        raise AutonomyError("invalid_payload", 422, f"等级必须是 {LEVELS}")
    record = get(conn, eligibility_id)
    if record["status"] != "eligible_for_review":
        raise AutonomyError("invalid_state", 409, "必须先提交证据并进入待评审")
    if record["environment"] == "production" and LEVEL_ORDER[level] > LEVEL_ORDER[P0_MAX_LEVEL]:
        raise AutonomyError(
            "p0_level_cap", 409, f"P0 阶段生产环境上限为 {P0_MAX_LEVEL}",
        )

    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "UPDATE autonomy_eligibilities SET level = ?, status = 'active',"
            " approved_by = ?, updated_at = ? WHERE id = ?",
            (level, approved_by, now, eligibility_id),
        )
        conn.execute(
            "INSERT INTO autonomy_changes (id, eligibility_id, from_level, to_level,"
            " reason, requested_by, approved_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_new_id("autc"), eligibility_id, record["level"], level, reason,
             record["approved_by"] or "local", approved_by, now),
        )
        events.emit(conn, "autonomy.eligibility.updated", project_id=record["project_id"],
                    resource_type="autonomy_eligibility", resource_id=eligibility_id,
                    payload={"from": record["level"], "to": level})
    return get(conn, eligibility_id)


def suspend(conn: sqlite3.Connection, eligibility_id: str, *, reason: str) -> dict[str, Any]:
    record = get(conn, eligibility_id)
    with store.write_tx(conn):
        conn.execute(
            "UPDATE autonomy_eligibilities SET status = 'suspended', level = 'A0',"
            " updated_at = ? WHERE id = ?", (store.now_iso(), eligibility_id),
        )
        events.emit(conn, "autonomy.eligibility.suspended",
                    project_id=record["project_id"],
                    resource_type="autonomy_eligibility", resource_id=eligibility_id,
                    payload={"reason": reason})
    return get(conn, eligibility_id)


def current_level(conn: sqlite3.Connection, project_id: str, task_type: str,
                  environment: str = "dev") -> str:
    """当前有效等级。过期或非 active 一律回落到 A0。"""
    row = conn.execute(
        "SELECT * FROM autonomy_eligibilities WHERE project_id = ? AND task_type = ?"
        " AND environment = ? ORDER BY updated_at DESC LIMIT 1",
        (project_id, task_type, environment),
    ).fetchone()
    if row is None or row["status"] != "active":
        return "A0"
    if row["expires_at"] and row["expires_at"] <= store.now_iso():
        return "A0"
    return row["level"]


def level_satisfies(current: str, required: str) -> bool:
    return LEVEL_ORDER.get(current, 0) >= LEVEL_ORDER.get(required, 0)
