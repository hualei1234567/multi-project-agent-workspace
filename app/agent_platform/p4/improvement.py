"""持续改进域 V1：失败模式 → Case → 根因 → 提案 → 沙箱评估 → 人工批准（P4-M5）。

硬约束（V6.0 §16.6）：

* **Agent 不得直接改生产配置**：本模块只产出"已批准的提案"，
  真正生效必须走 Release（P4-M4）的 Manifest + 审批 + Canary；
* **根因必须有证据**：``root_cause`` 不是模型猜测，必须带 evidence；
* **沙箱评估必须在隔离版本上做**，且评估不通过不得进入审批。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .. import store
from . import events


class ImprovementError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def record_failure(conn: sqlite3.Connection, project_id: str,
                   payload: dict[str, Any]) -> dict[str, Any]:
    """登记一次失败信号，按 pattern_key 聚合成失败模式。"""
    key = str(payload.get("pattern_key") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    if not key or not summary:
        raise ImprovementError("invalid_payload", 422, "pattern_key 与 summary 必填")
    now = store.now_iso()
    row = conn.execute(
        "SELECT * FROM failure_patterns WHERE project_id = ? AND pattern_key = ?",
        (project_id, key),
    ).fetchone()
    if row is None:
        pattern_id = _new_id("fp")
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO failure_patterns (id, project_id, pattern_key, summary,"
                " occurrences, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                (pattern_id, project_id, key, summary, now, now),
            )
    else:
        pattern_id = row["id"]
        with store.write_tx(conn):
            conn.execute(
                "UPDATE failure_patterns SET occurrences = occurrences + 1,"
                " updated_at = ? WHERE id = ?", (now, pattern_id),
            )
    return get_pattern(conn, pattern_id)


def get_pattern(conn: sqlite3.Connection, pattern_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM failure_patterns WHERE id = ?", (pattern_id,)
    ).fetchone()
    if row is None:
        raise ImprovementError("not_found", 404, "失败模式不存在")
    return dict(row)


def list_patterns(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM failure_patterns WHERE project_id = ?"
        " ORDER BY occurrences DESC, updated_at DESC", (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


def open_case(conn: sqlite3.Connection, project_id: str,
              payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ImprovementError("invalid_payload", 422, "title 必填")
    case_id = _new_id("ic")
    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO improvement_cases (id, project_id, title, status, source_type,"
            " source_ref, pattern_id, owner, created_at, updated_at)"
            " VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
            (case_id, project_id, title, str(payload.get("source_type") or "evaluation"),
             payload.get("source_ref"), payload.get("pattern_id"),
             payload.get("owner"), now, now),
        )
        events.emit(conn, "improvement.case.created", project_id=project_id,
                    resource_type="improvement_case", resource_id=case_id)
    return get_case(conn, case_id)


def get_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM improvement_cases WHERE id = ?", (case_id,)
    ).fetchone()
    if row is None:
        raise ImprovementError("not_found", 404, "改进 Case 不存在")
    case = dict(row)
    case["proposals"] = [
        dict(p) for p in conn.execute(
            "SELECT id, title, status, created_at FROM change_proposals WHERE case_id = ?"
            " ORDER BY created_at DESC", (case_id,),
        )
    ]
    return case


def list_cases(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM improvement_cases WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


def assess_root_cause(conn: sqlite3.Connection, case_id: str,
                      payload: dict[str, Any]) -> dict[str, Any]:
    """根因必须有证据，空证据直接拒绝（模型推测不等于确认根因）。"""
    root_cause = str(payload.get("root_cause") or "").strip()
    evidence = payload.get("evidence") or {}
    if not root_cause:
        raise ImprovementError("invalid_payload", 422, "root_cause 必填")
    if not evidence:
        raise ImprovementError("evidence_required", 422, "根因必须附带证据")

    case = get_case(conn, case_id)
    if case["status"] not in {"open", "investigating"}:
        raise ImprovementError("invalid_state", 409, f"当前状态 {case['status']}")
    with store.write_tx(conn):
        conn.execute(
            "UPDATE improvement_cases SET root_cause = ?, root_cause_evidence_json = ?,"
            " status = 'proposal_ready', updated_at = ? WHERE id = ?",
            (root_cause, store.dumps_json(evidence), store.now_iso(), case_id),
        )
    return get_case(conn, case_id)


def create_proposal(conn: sqlite3.Connection, project_id: str,
                    payload: dict[str, Any]) -> dict[str, Any]:
    case_id = str(payload.get("case_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not case_id or not title:
        raise ImprovementError("invalid_payload", 422, "case_id 与 title 必填")
    case = get_case(conn, case_id)
    if case["project_id"] != project_id:
        raise ImprovementError("forbidden", 403, "Case 不属于该项目")
    if not case["root_cause"]:
        raise ImprovementError("root_cause_required", 409, "先完成根因评估再提提案")

    for field in ("base_version", "candidate_version", "rollback_plan"):
        if not str(payload.get(field) or "").strip():
            raise ImprovementError("invalid_payload", 422, f"{field} 必填")

    proposal_id = _new_id("cp")
    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO change_proposals (id, project_id, case_id, title, status,"
            " base_version, candidate_version, change_json, expected_benefit,"
            " rollback_plan, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?)",
            (proposal_id, project_id, case_id, title,
             payload["base_version"], payload["candidate_version"],
             store.dumps_json(payload.get("change") or {}),
             payload.get("expected_benefit"), payload["rollback_plan"], now, now),
        )
        conn.execute(
            "UPDATE improvement_cases SET status = 'evaluating', updated_at = ? WHERE id = ?",
            (now, case_id),
        )
        events.emit(conn, "improvement.proposal.created", project_id=project_id,
                    resource_type="change_proposal", resource_id=proposal_id)
    return get_proposal(conn, proposal_id)


def get_proposal(conn: sqlite3.Connection, proposal_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM change_proposals WHERE id = ?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise ImprovementError("not_found", 404, "变更提案不存在")
    proposal = dict(row)
    proposal["sandbox"] = [
        dict(s) for s in conn.execute(
            "SELECT * FROM sandbox_evaluations WHERE proposal_id = ? ORDER BY created_at DESC",
            (proposal_id,),
        )
    ]
    return proposal


def run_sandbox(conn: sqlite3.Connection, proposal_id: str,
                eval_run_id: str) -> dict[str, Any]:
    """在隔离版本上跑评估。评估不通过 → 提案 rejected，不能进入审批。"""
    proposal = get_proposal(conn, proposal_id)
    run = conn.execute("SELECT * FROM eval_runs WHERE id = ?", (eval_run_id,)).fetchone()
    if run is None:
        raise ImprovementError("not_found", 404, "评估运行不存在")
    if run["project_id"] != proposal["project_id"]:
        raise ImprovementError("forbidden", 403, "评估运行不属于该项目")

    passed = run["decision"] == "pass"
    sandbox_id = _new_id("sbx")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO sandbox_evaluations (id, proposal_id, eval_run_id, status,"
            " result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sandbox_id, proposal_id, eval_run_id,
             "passed" if passed else "failed",
             store.dumps_json({"decision": run["decision"],
                               "score": run["score"],
                               "safety_violations": run["safety_violations"]}),
             store.now_iso()),
        )
        conn.execute(
            "UPDATE change_proposals SET status = ?, updated_at = ? WHERE id = ?",
            ("evaluated" if passed else "rejected", store.now_iso(), proposal_id),
        )
    return {"sandbox_id": sandbox_id, "proposal_id": proposal_id,
            "status": "passed" if passed else "failed"}


def request_approval(conn: sqlite3.Connection, proposal_id: str, *,
                     requested_by: str) -> dict[str, Any]:
    proposal = get_proposal(conn, proposal_id)
    if proposal["status"] != "evaluated":
        raise ImprovementError("sandbox_required", 409, "必须先通过沙箱评估")
    approval_id = _new_id("appr")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO approvals (id, project_id, resource_type, resource_id, action,"
            " requested_by, status, digest, created_at)"
            " VALUES (?, ?, 'change_proposal', ?, 'apply', ?, 'pending', ?, ?)",
            (approval_id, proposal["project_id"], proposal_id, requested_by,
             store.stable_id(proposal_id, proposal["candidate_version"]), store.now_iso()),
        )
        conn.execute(
            "UPDATE change_proposals SET status = 'pending_approval', approval_id = ?,"
            " updated_at = ? WHERE id = ?", (approval_id, store.now_iso(), proposal_id),
        )
    return {"approval_id": approval_id, "proposal_id": proposal_id,
            "status": "pending_approval"}


def approve(conn: sqlite3.Connection, approval_id: str, *,
            approved_by: str, note: str | None = None) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None or row["resource_type"] != "change_proposal":
        raise ImprovementError("not_found", 404, "审批单不存在")
    if row["status"] != "pending":
        raise ImprovementError("invalid_state", 409, f"审批单状态 {row['status']}")
    if row["requested_by"] == approved_by:
        raise ImprovementError("separation_of_duties", 403, "提案人不能自己批准")

    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "UPDATE approvals SET status = 'approved', approved_by = ?, decided_at = ?,"
            " decision_note = ? WHERE id = ?", (approved_by, now, note, approval_id),
        )
        conn.execute(
            "UPDATE change_proposals SET status = 'approved', updated_at = ? WHERE id = ?",
            (now, row["resource_id"]),
        )
        conn.execute(
            "UPDATE improvement_cases SET status = 'verified', updated_at = ?"
            " WHERE id = (SELECT case_id FROM change_proposals WHERE id = ?)",
            (now, row["resource_id"]),
        )
        events.emit(conn, "improvement.proposal.approved", project_id=row["project_id"],
                    resource_type="change_proposal", resource_id=row["resource_id"],
                    payload={"approved_by": approved_by})
    return {"approval_id": approval_id, "status": "approved"}
