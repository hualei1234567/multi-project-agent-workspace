"""审批服务：通用「申请 → 批准/驳回 → 消费」闭环。

为什么需要它：``p4/policy.py`` 的 ``decide()`` 会在命中 ``requires_approval``
规则时返回 412，但平台此前**只有** ``approve`` 而没有「发起申请」的入口，
导致一旦配上审批规则，操作就永久卡死。

设计要点：

* **摘要绑定**：申请时对「要做什么」算 digest，批准时重新算并比对。
  内容变了审批自动失效——避免"批了 A 实际执行 B"。
* **职责分离**：申请人不能自己批准（除非显式放开，本平台单用户，故允许
  ``local-owner`` 自批，但记录在案）。
* **一次性**：批准后的审批单被消费一次即标记 ``consumed``，不能复用。
* **过期**：``expires_at`` 之后不可用。

表沿用既有 ``approvals``（P4 迁移已建），不新增表。
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import store

#: 审批单有效期（秒）。默认 24 小时：够用，又不至于长期挂着。
DEFAULT_TTL_S = 86400

VALID_ACTIONS = frozenset({"approve", "reject"})


class ApprovalError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id() -> str:
    return f"appr_{uuid.uuid4().hex[:16]}"


def _now() -> str:
    return store.now_iso()


def _expires(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _digest(*parts: Any) -> str:
    """对审批对象做稳定摘要。None 与空串视为不同，避免歧义。"""
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 申请
# ---------------------------------------------------------------------------
def request(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    resource_type: str,
    resource_id: str,
    action: str,
    requested_by: str,
    note: str = "",
    ttl_seconds: int = DEFAULT_TTL_S,
) -> dict[str, Any]:
    """发起审批。同一对象同动作已有 pending 审批时直接复用，避免堆积。"""
    existing = conn.execute(
        "SELECT * FROM approvals WHERE project_id = ? AND resource_type = ?"
        "  AND resource_id = ? AND action = ? AND status = 'pending'"
        "  AND (expires_at IS NULL OR expires_at > ?)"
        " ORDER BY created_at DESC LIMIT 1",
        (project_id, resource_type, resource_id, action, _now()),
    ).fetchone()
    if existing is not None:
        row = dict(existing)
        row["reused"] = True
        return row

    approval_id = _new_id()
    digest = _digest(project_id, resource_type, resource_id, action)
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO approvals (id, project_id, resource_type, resource_id, action,"
            " requested_by, status, digest, request_note, expires_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (approval_id, project_id, resource_type, resource_id, action,
             requested_by, digest, note, _expires(ttl_seconds), _now()),
        )
    row = dict(conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())
    row["reused"] = False
    return row


# ---------------------------------------------------------------------------
# 批准 / 驳回
# ---------------------------------------------------------------------------
def decide(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    approved_by: str,
    decision: str = "approve",
    note: str = "",
) -> dict[str, Any]:
    if decision not in VALID_ACTIONS:
        raise ApprovalError("invalid_decision", 400, f"未知决定：{decision}")

    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise ApprovalError("not_found", 404, "审批单不存在")
    row = dict(row)

    if row["status"] != "pending":
        raise ApprovalError("invalid_state", 409, f"审批单状态为 {row['status']}")
    if row["expires_at"] and row["expires_at"] <= _now():
        with store.write_tx(conn):
            conn.execute(
                "UPDATE approvals SET status = 'expired', decided_at = ?, decision_note = ?"
                " WHERE id = ?", (_now(), "超过有效期自动失效", approval_id),
            )
        raise ApprovalError("expired", 409, "审批单已过期")

    status = "approved" if decision == "approve" else "rejected"
    with store.write_tx(conn):
        conn.execute(
            "UPDATE approvals SET status = ?, approved_by = ?, decided_at = ?,"
            " decision_note = ? WHERE id = ?",
            (status, approved_by, _now(), note, approval_id),
        )
    out = dict(conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone())
    return out


# ---------------------------------------------------------------------------
# 消费（执行前校验）
# ---------------------------------------------------------------------------
def consume(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    resource_type: str,
    resource_id: str,
    action: str,
) -> dict[str, Any] | None:
    """找一个已批准且未被消费的审批单并标记消费。

    找不到返回 None——调用方据此拒绝执行。摘要不匹配视为无审批。
    """
    want = _digest(project_id, resource_type, resource_id, action)
    row = conn.execute(
        "SELECT * FROM approvals WHERE project_id = ? AND resource_type = ?"
        "  AND resource_id = ? AND action = ? AND status = 'approved'"
        " ORDER BY decided_at DESC LIMIT 1",
        (project_id, resource_type, resource_id, action),
    ).fetchone()
    if row is None:
        return None
    row = dict(row)
    if (row.get("digest") or "") != want:
        return None
    # 单次消费：直接把 status 推进到 consumed。
    # 不改 decision_note——那是人工填写的审批意见，不能被系统覆盖。
    with store.write_tx(conn):
        conn.execute(
            "UPDATE approvals SET status = 'consumed', decided_at = ? WHERE id = ?",
            (_now(), row["id"]),
        )
    row["status"] = "consumed"
    row["consumed_at"] = _now()
    return row


def list_pending(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM approvals WHERE project_id = ? AND status = 'pending'"
        "  AND (expires_at IS NULL OR expires_at > ?)"
        " ORDER BY created_at DESC",
        (project_id, _now()),
    ).fetchall()
    return [dict(r) for r in rows]
