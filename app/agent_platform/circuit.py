"""熔断器（P3-M9 前置，蓝图 §3 P3-M1 硬要求）。

**为什么 Full Auto 必须有它**：无人值守时，连续失败意味着要么模型跑偏、
要么环境坏了。继续派发只会烧钱并制造垃圾改动。熔断比"多试几次"更重要。

规则（蓝图原文）：**同项目连续 3 次失败自动 paused，需人工恢复**。

设计取舍：

* 熔断状态存在 ``kill_switches`` 表（复用既有表，不新建）——
  它本来就是"阻止执行"的机制，语义一致。
* **按项目熔断**，不是全局熔断：一个项目烂掉不该拖垮另一个。
* 熔断后 ``scheduler.schedulable()`` 把该项目所有任务挡在候选之外。
* 恢复必须显式（人工调 API），不自动恢复——自动恢复等于没有熔断。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from . import store

#: 连续失败多少次触发熔断。蓝图定的 3。
FAILURE_THRESHOLD = 3

#: 熔断作用域标记。
SCOPE_PROJECT = "project"

#: 熔断原因前缀，用于区分"人工熔断"与"自动熔断"。
AUTO_REASON_PREFIX = "[auto]"


class CircuitError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id() -> str:
    return f"ks_{uuid.uuid4().hex[:16]}"


def consecutive_failures(conn: sqlite3.Connection, project_id: str) -> int:
    """最近连续失败次数。遇到一次成功即清零。

    只看**终态**（succeeded / failed），running/queued 不算。
    """
    rows = conn.execute(
        "SELECT status FROM dispatch_items WHERE project_id = ?"
        "  AND status IN ('succeeded', 'failed')"
        " ORDER BY COALESCE(finished_at, updated_at) DESC LIMIT 50",
        (project_id,),
    ).fetchall()
    n = 0
    for r in rows:
        if r["status"] == "failed":
            n += 1
        else:
            break
    return n


def is_tripped(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    """该项目是否已熔断。返回熔断记录或 None。"""
    try:
        row = conn.execute(
            "SELECT * FROM kill_switches WHERE scope = ? AND project_id = ?"
            "  AND released_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (SCOPE_PROJECT, project_id),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return dict(row) if row else None


def trip(conn: sqlite3.Connection, project_id: str, *, reason: str,
         triggered_by: str = "system") -> dict[str, Any]:
    """触发熔断。幂等：已熔断则不重复建。"""
    existing = is_tripped(conn, project_id)
    if existing is not None:
        return existing
    kid = _new_id()
    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO kill_switches (id, scope, project_id, domain, reason,"
            " triggered_by, created_at) VALUES (?, ?, ?, NULL, ?, ?, ?)",
            (kid, SCOPE_PROJECT, project_id, reason, triggered_by, now),
        )
    from .p4 import killswitch
    killswitch.reset_cache()
    return dict(conn.execute("SELECT * FROM kill_switches WHERE id = ?",
                             (kid,)).fetchone())


def reset(conn: sqlite3.Connection, project_id: str, *,
          released_by: str = "local", note: str = "") -> dict[str, Any]:
    """人工恢复。**必须显式调用**——自动恢复等于没有熔断。"""
    row = is_tripped(conn, project_id)
    if row is None:
        raise CircuitError("not_tripped", 404, "该项目未处于熔断状态")
    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "UPDATE kill_switches SET released_at = ?, reason = ? WHERE id = ?",
            (now, f"{row['reason']} | 恢复：{note}（{released_by}）", row["id"]),
        )
    from .p4 import killswitch
    killswitch.reset_cache()
    return {"project_id": project_id, "released_at": now, "by": released_by}


def check_after_failure(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """执行失败后调用。达到阈值就熔断。

    返回 ``{tripped, failures, threshold, record}``。
    """
    n = consecutive_failures(conn, project_id)
    if n < FAILURE_THRESHOLD:
        return {"tripped": False, "failures": n,
                "threshold": FAILURE_THRESHOLD, "record": None}
    rec = trip(
        conn, project_id,
        reason=f"{AUTO_REASON_PREFIX} 连续 {n} 次执行失败，自动暂停派发",
        triggered_by="executor",
    )
    return {"tripped": True, "failures": n,
            "threshold": FAILURE_THRESHOLD, "record": rec}


def status(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """熔断状态总览。"""
    rec = is_tripped(conn, project_id)
    return {
        "project_id": project_id,
        "tripped": rec is not None,
        "failures": consecutive_failures(conn, project_id),
        "threshold": FAILURE_THRESHOLD,
        "record": rec,
    }
