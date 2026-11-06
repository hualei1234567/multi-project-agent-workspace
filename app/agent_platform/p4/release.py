"""发布治理域 V1：Manifest / 评估门禁 / 审批 / Canary / 回滚（P4-M4）。

四条不可绕过的规则（V6.0 §9.7、§15）：

1. **Manifest 不可变**：申请发布后任何条目变化都会改变 ``manifest_digest``，
   审批随之失效——不存在"批完之后偷偷改版本"；
2. **评估门禁**：没有 ``decision=pass`` 的评估运行，不能申请审批；
3. **职责分离**：审批人不得是申请提交人；
4. **Canary 可停**：超过停止阈值立即 stopped，回滚留痕且不改写历史。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .. import store
from . import events

ARTIFACT_TYPES = ("prompt", "skill", "model", "workflow")


class ReleaseError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _digest(items: list[dict[str, Any]]) -> str:
    parts = sorted(
        f"{item['artifact_type']}:{item['artifact_ref']}:{item['artifact_version']}:{item['digest']}"
        for item in items
    )
    return store.stable_id(*parts) if parts else ""


# --------------------------------------------------------------------------
# 创建与校验
# --------------------------------------------------------------------------
def create_release(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = str(payload.get("release_key") or "").strip()
    name = str(payload.get("name") or "").strip()
    items = payload.get("items") or []
    if not key or not name:
        raise ReleaseError("invalid_payload", 422, "release_key 与 name 必填")
    if not isinstance(items, list) or not items:
        raise ReleaseError("invalid_payload", 422, "items 不能为空")
    for item in items:
        if item.get("artifact_type") not in ARTIFACT_TYPES:
            raise ReleaseError("invalid_payload", 422, f"artifact_type 非法：{item.get('artifact_type')}")
        for field in ("artifact_ref", "artifact_version", "digest"):
            if not str(item.get(field) or "").strip():
                raise ReleaseError("invalid_payload", 422, f"缺少 {field}")

    release_id = _new_id("rel")
    now = store.now_iso()
    digest = _digest(items)
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO agent_releases (id, project_id, release_key, name, status,"
                " manifest_digest, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'assembled', ?, ?, ?, ?)",
                (release_id, project_id, key, name, digest,
                 str(payload.get("created_by") or "local"), now, now),
            )
            for item in items:
                conn.execute(
                    "INSERT INTO release_manifest_items (id, release_id, artifact_type,"
                    " artifact_ref, artifact_version, digest) VALUES (?, ?, ?, ?, ?, ?)",
                    (_new_id("rmi"), release_id, item["artifact_type"], item["artifact_ref"],
                     item["artifact_version"], item["digest"]),
                )
            events.emit(conn, "agent.release.created", project_id=project_id,
                        resource_type="agent_release", resource_id=release_id,
                        payload={"digest": digest, "items": len(items)})
    except sqlite3.IntegrityError as exc:
        raise ReleaseError("conflict", 409, "同项目下 release_key 已存在") from exc
    return get_release(conn, release_id)


def get_release(conn: sqlite3.Connection, release_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM agent_releases WHERE id = ?", (release_id,)
    ).fetchone()
    if row is None:
        raise ReleaseError("not_found", 404, "Release 不存在")
    release = dict(row)
    release["items"] = [
        dict(item) for item in conn.execute(
            "SELECT artifact_type, artifact_ref, artifact_version, digest"
            " FROM release_manifest_items WHERE release_id = ?"
            " ORDER BY artifact_type, artifact_ref", (release_id,)
        )
    ]
    release["manifest_digest_now"] = _digest(release["items"])
    release["digest_intact"] = release["manifest_digest_now"] == release["manifest_digest"]
    return release


def list_releases(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM agent_releases WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


def validate_release(conn: sqlite3.Connection, release_id: str) -> dict[str, Any]:
    release = get_release(conn, release_id)
    errors: list[str] = []
    if not release["items"]:
        errors.append("Manifest 为空")
    if not any(item["artifact_type"] == "prompt" for item in release["items"]):
        errors.append("Manifest 至少需要一个 prompt 条目")
    for item in release["items"]:
        if not item["artifact_version"] or not item["digest"]:
            errors.append(f"{item['artifact_ref']} 缺少版本或摘要")
    if not release["digest_intact"]:
        errors.append("Manifest 摘要与记录不一致（内容被改动）")

    if not errors:
        with store.write_tx(conn):
            conn.execute(
                "UPDATE agent_releases SET status = 'validated', updated_at = ?"
                " WHERE id = ? AND status IN ('assembled', 'validated')",
                (store.now_iso(), release_id),
            )
    return {"valid": not errors, "errors": errors,
            "digest": release["manifest_digest"]}


# --------------------------------------------------------------------------
# 评估门禁
# --------------------------------------------------------------------------
def attach_evaluation(conn: sqlite3.Connection, release_id: str, eval_run_id: str) -> dict[str, Any]:
    """挂接评估运行。decision 必须为 pass，安全失败不能被平均分掩盖。"""
    release = get_release(conn, release_id)
    run = conn.execute(
        "SELECT * FROM eval_runs WHERE id = ?", (eval_run_id,)
    ).fetchone()
    if run is None:
        raise ReleaseError("not_found", 404, "评估运行不存在")
    if run["project_id"] != release["project_id"]:
        raise ReleaseError("forbidden", 403, "评估运行不属于该项目")
    if run["decision"] != "pass":
        raise ReleaseError(
            "evaluation_gate_failed", 409,
            f"评估未通过（decision={run['decision']}, safety={run['safety_violations']}），不能进入发布",
        )
    if not release["digest_intact"]:
        raise ReleaseError("manifest_tampered", 409, "Manifest 已被改动，请重新创建 Release")

    with store.write_tx(conn):
        conn.execute(
            "UPDATE agent_releases SET eval_run_id = ?, status = 'evaluated', updated_at = ?"
            " WHERE id = ?", (eval_run_id, store.now_iso(), release_id),
        )
    return {"release_id": release_id, "eval_run_id": eval_run_id, "status": "evaluated"}


# --------------------------------------------------------------------------
# 审批
# --------------------------------------------------------------------------
def request_approval(conn: sqlite3.Connection, release_id: str, *,
                     requested_by: str, note: str | None = None) -> dict[str, Any]:
    release = get_release(conn, release_id)
    # 先问"评估过没有"，再问"状态对不对"——前者是更根本的原因。
    if not release["eval_run_id"]:
        raise ReleaseError("evaluation_required", 409, "必须先通过评估门禁")
    if release["status"] not in {"validated", "evaluated"}:
        raise ReleaseError("invalid_state", 409, f"当前状态 {release['status']} 不能申请审批")

    approval_id = _new_id("appr")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO approvals (id, project_id, resource_type, resource_id, action,"
            " requested_by, status, digest, request_note, created_at)"
            " VALUES (?, ?, 'agent_release', ?, 'publish', ?, 'pending', ?, ?, ?)",
            (approval_id, release["project_id"], release_id, requested_by,
             release["manifest_digest"], note, store.now_iso()),
        )
        conn.execute(
            "UPDATE agent_releases SET status = 'pending_approval', approval_id = ?,"
            " updated_at = ? WHERE id = ?", (approval_id, store.now_iso(), release_id),
        )
    return {"approval_id": approval_id, "release_id": release_id, "status": "pending_approval"}


def approve(conn: sqlite3.Connection, approval_id: str, *,
            approved_by: str, note: str | None = None) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise ReleaseError("not_found", 404, "审批单不存在")
    if row["status"] != "pending":
        raise ReleaseError("invalid_state", 409, f"审批单状态为 {row['status']}")
    if row["requested_by"] == approved_by:
        raise ReleaseError("separation_of_duties", 403, "提案人不能自己批准")

    release = get_release(conn, row["resource_id"])
    if not release["digest_intact"] or release["manifest_digest"] != row["digest"]:
        with store.write_tx(conn):
            conn.execute(
                "UPDATE approvals SET status = 'expired', decided_at = ?,"
                " decision_note = ? WHERE id = ?",
                (store.now_iso(), "Manifest 已变更，审批自动失效", approval_id),
            )
        raise ReleaseError("approval_invalidated", 409, "Manifest 已变更，审批失效")

    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "UPDATE approvals SET status = 'approved', approved_by = ?, decided_at = ?,"
            " decision_note = ? WHERE id = ?", (approved_by, now, note, approval_id),
        )
        conn.execute(
            "UPDATE agent_releases SET status = 'approved', updated_at = ? WHERE id = ?",
            (now, release["id"]),
        )
        events.emit(conn, "agent.release.approved", project_id=release["project_id"],
                    resource_type="agent_release", resource_id=release["id"],
                    payload={"approval_id": approval_id, "approved_by": approved_by})
    return {"approval_id": approval_id, "status": "approved", "release_id": release["id"]}


def publish(conn: sqlite3.Connection, release_id: str) -> dict[str, Any]:
    release = get_release(conn, release_id)
    if release["status"] != "approved":
        raise ReleaseError("invalid_state", 409, f"当前状态 {release['status']} 不能发布")
    if not release["digest_intact"]:
        raise ReleaseError("manifest_tampered", 409, "Manifest 已变更，需重新审批")
    with store.write_tx(conn):
        conn.execute(
            "UPDATE agent_releases SET status = 'published', updated_at = ? WHERE id = ?",
            (store.now_iso(), release_id),
        )
    return {"release_id": release_id, "status": "published"}


# --------------------------------------------------------------------------
# Canary 与回滚
# --------------------------------------------------------------------------
def start_canary(conn: sqlite3.Connection, release_id: str, *,
                 traffic_pct: int = 5, stop_threshold: dict[str, Any] | None = None,
                 created_by: str = "local") -> dict[str, Any]:
    release = get_release(conn, release_id)
    if release["status"] not in {"published", "approved"}:
        raise ReleaseError("invalid_state", 409, "只有已批准/已发布的 Release 能做 Canary")
    deployment_id = _new_id("canary")
    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO canary_deployments (id, release_id, project_id, status,"
            " traffic_pct, stop_threshold_json, started_at, created_at)"
            " VALUES (?, ?, ?, 'canary', ?, ?, ?, ?)",
            (deployment_id, release_id, release["project_id"],
             max(1, min(100, int(traffic_pct))),
             store.dumps_json(stop_threshold or {}), now, now),
        )
        events.emit(conn, "agent.release.canary.started", project_id=release["project_id"],
                    resource_type="canary_deployment", resource_id=deployment_id,
                    payload={"traffic_pct": traffic_pct})
    return get_canary(conn, deployment_id)


def get_canary(conn: sqlite3.Connection, deployment_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM canary_deployments WHERE id = ?", (deployment_id,)
    ).fetchone()
    if row is None:
        raise ReleaseError("not_found", 404, "Canary 部署不存在")
    return dict(row)


def observe_canary(conn: sqlite3.Connection, deployment_id: str,
                   metrics: dict[str, Any]) -> dict[str, Any]:
    """记录观测值；任一指标越过停止阈值立即 stopped。"""
    deployment = get_canary(conn, deployment_id)
    thresholds = store.loads_json(deployment["stop_threshold_json"], {}) or {}
    breaches = []
    for key, limit in thresholds.items():
        value = metrics.get(key)
        if value is None:
            continue
        if isinstance(limit, (int, float)) and isinstance(value, (int, float)) and value > limit:
            breaches.append(f"{key}={value} > {limit}")

    status = "stopped" if breaches else deployment["status"]
    with store.write_tx(conn):
        conn.execute(
            "UPDATE canary_deployments SET observed_json = ?, status = ?,"
            " ended_at = CASE WHEN ? = 'stopped' THEN ? ELSE ended_at END WHERE id = ?",
            (store.dumps_json(metrics), status, status, store.now_iso(), deployment_id),
        )
    return {"deployment_id": deployment_id, "status": status, "breaches": breaches}


def promote_canary(conn: sqlite3.Connection, deployment_id: str) -> dict[str, Any]:
    deployment = get_canary(conn, deployment_id)
    if deployment["status"] != "canary":
        raise ReleaseError("invalid_state", 409, f"当前状态 {deployment['status']} 不能晋级")
    with store.write_tx(conn):
        conn.execute(
            "UPDATE canary_deployments SET status = 'promoted', ended_at = ? WHERE id = ?",
            (store.now_iso(), deployment_id),
        )
        events.emit(conn, "agent.release.promoted",
                    project_id=deployment["project_id"],
                    resource_type="canary_deployment", resource_id=deployment_id)
    return {"deployment_id": deployment_id, "status": "promoted"}


def rollback(conn: sqlite3.Connection, release_id: str, *,
             reason: str, deployment_id: str | None = None,
             created_by: str = "local") -> dict[str, Any]:
    """回滚：不改写历史 Release，只记录"退回到哪套组合"并留痕。"""
    release = get_release(conn, release_id)
    rollback_id = _new_id("rb")
    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO rollback_records (id, release_id, deployment_id, reason,"
            " restored_digest, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rollback_id, release_id, deployment_id, reason,
             release["manifest_digest"], created_by, now),
        )
        if deployment_id:
            conn.execute(
                "UPDATE canary_deployments SET status = 'rolled_back', ended_at = ?"
                " WHERE id = ?", (now, deployment_id),
            )
        conn.execute(
            "UPDATE agent_releases SET status = 'deprecated', updated_at = ? WHERE id = ?",
            (now, release_id),
        )
        events.emit(conn, "agent.release.rolled_back", project_id=release["project_id"],
                    resource_type="agent_release", resource_id=release_id,
                    payload={"reason": reason, "deployment_id": deployment_id})
    return {"rollback_id": rollback_id, "release_id": release_id,
            "restored_digest": release["manifest_digest"], "status": "rolled_back"}


def list_canaries(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM canary_deployments WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


def list_rollbacks(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT r.* FROM rollback_records r"
        " JOIN agent_releases a ON a.id = r.release_id WHERE a.project_id = ?"
        " ORDER BY r.created_at DESC", (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}
