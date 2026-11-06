"""共享资源登记与占用（M3-2 T3-203）。

用途：4090 站点运行时、A800 训练集群这类**同一时刻只能一个项目占用**的资源。
跨项目协作的难点不是任务依赖，而是「抢同一台机器」——这里把这件事显式化。

规则：
* ``exclusive=1`` 的资源同一时刻只能有一个 active claim。
* claim 有 ``expires_at``，过期即失效（避免某个项目忘了释放把资源锁死）。
* 释放是幂等的。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import store

DEFAULT_CLAIM_TTL_S = 3600


class ResourceError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _now() -> str:
    return store.now_iso()


def _expires(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)  # noqa: UP017
    ).isoformat(timespec="seconds")


def list_resources() -> dict[str, Any]:
    with store.session() as conn:
        rows = conn.execute(
            """
            SELECT r.*, p.name AS owner_project_name,
                   (SELECT COUNT(*) FROM resource_claims c
                     WHERE c.resource_id = r.id AND c.status = 'active'
                       AND (c.expires_at IS NULL OR c.expires_at > ?)) AS active_claims
              FROM shared_resources r
              LEFT JOIN agent_projects p ON p.id = r.owner_project_id
             ORDER BY r.name
            """,
            (_now(),),
        ).fetchall()
        return {"items": [dict(row) for row in rows], "total": len(rows)}


def create_resource(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ResourceError("invalid_json", 400)
    name = str(payload.get("name") or "").strip()
    kind = str(payload.get("kind") or "").strip()
    if not name:
        raise ResourceError("invalid_payload", 422, "name 必填")
    if kind not in {"gpu", "device", "port", "service", "host"}:
        raise ResourceError("invalid_payload", 422, "kind 非法")
    owner = payload.get("owner_project_id") or None
    capacity = int(payload.get("capacity") or 1)
    if capacity < 1:
        raise ResourceError("invalid_payload", 422, "capacity 必须 ≥ 1")

    conn = store.connect()
    try:
        store.init_db(conn)
        if owner and conn.execute(
            "SELECT 1 FROM agent_projects WHERE id = ?", (owner,)
        ).fetchone() is None:
            raise ResourceError("invalid_payload", 422, "owner_project_id 不存在")
        resource_id = uuid.uuid4().hex
        now = _now()
        try:
            with store.write_tx(conn):
                conn.execute(
                    "INSERT INTO shared_resources"
                    " (id, name, kind, owner_project_id, capacity, exclusive,"
                    "  metadata_json, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (resource_id, name, kind, owner, capacity,
                     1 if payload.get("exclusive", True) else 0,
                     store.dumps_json(payload.get("metadata") or {}), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ResourceError("conflict", 409, "资源名已存在") from exc
        return get_resource(resource_id, conn=conn)
    finally:
        conn.close()


def get_resource(resource_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    own = conn is None
    conn = conn or store.connect()
    try:
        if own:
            store.init_db(conn)
        row = conn.execute(
            "SELECT * FROM shared_resources WHERE id = ?", (resource_id,)
        ).fetchone()
        if row is None:
            raise ResourceError("not_found", 404, "资源不存在")
        resource = dict(row)
        now = _now()
        resource["claims"] = [
            dict(claim)
            for claim in conn.execute(
                "SELECT * FROM resource_claims WHERE resource_id = ?"
                " AND status = 'active' AND (expires_at IS NULL OR expires_at > ?)"
                " ORDER BY created_at",
                (resource_id, now),
            )
        ]
        return resource
    finally:
        if own:
            conn.close()


def claim_resource(
    resource_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """申请占用。独占资源已被占时拒绝，并告知持有者。"""
    if not isinstance(payload, dict):
        raise ResourceError("invalid_json", 400)
    project_id = str(payload.get("project_id") or "").strip()
    if not project_id:
        raise ResourceError("invalid_payload", 422, "project_id 必填")
    try:
        amount = int(payload.get("amount") or 1)
    except (TypeError, ValueError) as exc:
        raise ResourceError("invalid_payload", 422, "amount 必须是整数") from exc
    if amount < 1:
        raise ResourceError("invalid_payload", 422, "amount 必须 ≥ 1")
    try:
        ttl = int(payload.get("ttl_seconds") or DEFAULT_CLAIM_TTL_S)
    except (TypeError, ValueError) as exc:
        raise ResourceError("invalid_payload", 422, "ttl_seconds 必须是整数") from exc
    ttl = max(60, min(86400, ttl))

    conn = store.connect()
    try:
        store.init_db(conn)
        resource = get_resource(resource_id, conn=conn)
        if conn.execute(
            "SELECT 1 FROM agent_projects WHERE id = ?", (project_id,)
        ).fetchone() is None:
            raise ResourceError("invalid_payload", 422, "project_id 不存在")

        now = _now()
        active = [
            claim for claim in resource["claims"]
            if claim["status"] == "active" and (claim["expires_at"] is None or claim["expires_at"] > now)
        ]
        if resource["exclusive"] and active:
            holder = active[0]
            raise ResourceError(
                "resource_busy", 409,
                f"资源已被项目 {holder['project_id']} 占用至 {holder['expires_at']}",
            )
        used = sum(int(claim["amount"] or 0) for claim in active)
        if used + amount > int(resource["capacity"]):
            raise ResourceError(
                "capacity_exceeded", 409,
                f"剩余容量不足：已用 {used} / 总量 {resource['capacity']}",
            )

        claim_id = uuid.uuid4().hex
        _safe_audit(conn, project_id, "resource.claim", "shared_resource", resource_id, {
            "claim_id": claim_id, "amount": amount, "task_id": payload.get("task_id"),
            "ttl_seconds": ttl,
        })
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO resource_claims"
                " (id, resource_id, project_id, task_id, amount, status, expires_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                (claim_id, resource_id, project_id, payload.get("task_id"),
                 amount, _expires(ttl), now),
            )
        return {"claim_id": claim_id, "resource": get_resource(resource_id, conn=conn)}
    finally:
        conn.close()


def release_claim(resource_id: str, claim_id: str) -> dict[str, Any]:
    conn = store.connect()
    try:
        store.init_db(conn)
        row = conn.execute(
            "SELECT * FROM resource_claims WHERE id = ? AND resource_id = ?",
            (claim_id, resource_id),
        ).fetchone()
        if row is None:
            raise ResourceError("not_found", 404, "占用记录不存在")
        if row["status"] != "active":
            return {"claim_id": claim_id, "status": row["status"], "changed": False}
        with store.write_tx(conn):
            conn.execute(
                "UPDATE resource_claims SET status='released', released_at=? WHERE id=?",
                (_now(), claim_id),
            )
        _safe_audit(conn, row["project_id"], "resource.release", "shared_resource",
                    resource_id, {"claim_id": claim_id})
        return {"claim_id": claim_id, "status": "released", "changed": True}
    finally:
        conn.close()


def _safe_audit(
    conn, project_id: str | None, action: str, resource_type: str,
    resource_id: str | None, detail: dict[str, Any], *, result: str = "ok",
) -> None:
    """写统一审计；失败不阻断资源操作。"""
    try:
        from . import service

        service.audit(conn, project_id, action, resource_type, resource_id,
                      detail=detail, result=result)
    except Exception:  # noqa: BLE001,S110 - 审计失败不影响资源操作
        pass


def expire_stale_claims(conn: sqlite3.Connection | None = None) -> int:
    """把过期未释放的占用标记为 expired。返回处理条数。"""
    own = conn is None
    conn = conn or store.connect()
    try:
        if own:
            store.init_db(conn)
        now = _now()
        with store.write_tx(conn):
            cur = conn.execute(
                "UPDATE resource_claims SET status='expired', released_at=?"
                " WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ?",
                (now, now),
            )
        return max(cur.rowcount, 0)
    finally:
        if own:
            conn.close()
