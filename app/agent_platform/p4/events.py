"""事件平面：Outbox / Inbox（ADR-P4-008，风险 P4-R14）。

设计取舍：**不新建第二条事件总线**。第三期已有 agent_events / dispatch_events /
audit_logs / runtime_action_runs 四张表，但它们只写不投递。这里补上投递能力：

* ``emit()`` 在**调用方的事务内**写 outbox，保证"业务状态改变"与"事件待发"
  原子一致（不会出现状态改了但事件丢了）；
* ``dispatch_pending()`` 单线程拉取 pending 事件交给 handler；
* ``event_inbox`` 按 (event_id, consumer) 去重，保证至少一次投递不产生重复副作用。

Envelope 字段按 V6.0 §11.6，兼容第三期既有表结构（扩展列而非新建总线）。
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable
from typing import Any

from .. import store

SCHEMA_VERSION = 1


def new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:20]}"


def emit(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    project_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    payload: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    event_id: str | None = None,
) -> str:
    """写一条待投递事件。**必须在调用方的事务内调用**，与业务写入同生共死。"""
    eid = event_id or new_event_id()
    conn.execute(
        "INSERT INTO event_outbox (id, event_id, event_type, schema_version, project_id,"
        " resource_type, resource_id, correlation_id, causation_id, payload_json,"
        " occurred_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
        (
            uuid.uuid4().hex, eid, event_type, SCHEMA_VERSION, project_id,
            resource_type, resource_id, correlation_id, causation_id,
            store.dumps_json(payload or {}), store.now_iso(),
        ),
    )
    return eid


def pending(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM event_outbox WHERE status = 'pending' ORDER BY occurred_at, id LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def envelope(row: dict[str, Any]) -> dict[str, Any]:
    """把 outbox 行转成 V6.0 §11.6 的 Envelope。"""
    payload = store.loads_json(row["payload_json"], {}) or {}
    return {
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "schema_version": row["schema_version"],
        "project_id": row["project_id"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "occurred_at": row["occurred_at"],
        "ingested_at": store.now_iso(),
        "source_type": "p4_eventbus",
        "source_ref": row["id"],
        "correlation_id": row["correlation_id"],
        "causation_id": row["causation_id"],
        "payload": payload,
    }


def dispatch_pending(
    conn: sqlite3.Connection,
    handler: Callable[[dict[str, Any]], None] | None = None,
    *,
    consumer: str = "default",
    limit: int = 100,
    max_attempts: int = 5,
) -> dict[str, int]:
    """投递待发事件。返回计数。

    handler 为空时只做"标记已投递"，用于没有消费者的阶段。
    handler 抛错则记 failed 并累加 attempts，达到上限后不再重试（进 DLQ）。
    """
    stats = {"dispatched": 0, "skipped": 0, "failed": 0}
    for row in pending(conn, limit):
        seen = conn.execute(
            "SELECT 1 FROM event_inbox WHERE event_id = ? AND consumer = ?",
            (row["event_id"], consumer),
        ).fetchone()
        if seen is not None:
            _mark(conn, row["id"], "dispatched", None, attempts=row["attempts"] + 1)
            stats["skipped"] += 1
            continue
        try:
            if handler is not None:
                handler(envelope(row))
            with store.write_tx(conn):
                conn.execute(
                    "INSERT OR IGNORE INTO event_inbox (event_id, consumer, processed_at)"
                    " VALUES (?, ?, ?)",
                    (row["event_id"], consumer, store.now_iso()),
                )
            _mark(conn, row["id"], "dispatched", None, attempts=row["attempts"] + 1)
            stats["dispatched"] += 1
        except Exception as exc:  # noqa: BLE001 - 单条事件失败不应中断整批
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= max_attempts else "pending"
            _mark(conn, row["id"], status, str(exc)[:500], attempts=attempts)
            stats["failed"] += 1
    return stats


def _mark(conn: sqlite3.Connection, outbox_id: str, status: str,
          error: str | None, *, attempts: int) -> None:
    with store.write_tx(conn):
        conn.execute(
            "UPDATE event_outbox SET status = ?, attempts = ?, last_error = ?,"
            " dispatched_at = CASE WHEN ? = 'dispatched' THEN ? ELSE dispatched_at END"
            " WHERE id = ?",
            (status, attempts, error, status, store.now_iso(), outbox_id),
        )


def replay(conn: sqlite3.Connection, event_id: str, *, consumer: str) -> bool:
    """重放：清掉该消费者对该事件的去重记录，允许再次投递。"""
    with store.write_tx(conn):
        cur = conn.execute(
            "DELETE FROM event_inbox WHERE event_id = ? AND consumer = ?",
            (event_id, consumer),
        )
        conn.execute(
            "UPDATE event_outbox SET status = 'pending', last_error = NULL, attempts = 0"
            " WHERE event_id = ?",
            (event_id,),
        )
    return max(cur.rowcount, 0) > 0
