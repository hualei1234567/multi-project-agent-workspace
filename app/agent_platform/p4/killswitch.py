"""Kill Switch：DB 驱动的即时止损（ADR-P4-012，风险 P4-R20）。

为什么不能只靠环境变量：改环境变量要重启进程，重启会打断进行中的任务，
而且重启期间的请求无人拦截。Kill Switch 必须能在**不重启**的前提下，
让新请求在秒级被拒绝。

范围：
* ``global``          —— 全平台停；
* ``project``         —— 单项目停；
* ``project_domain``  —— 单项目的某个域停（如 material 的 evaluation）。

命中即拒绝，不做"软降级"。解除只能显式 release 或等 TTL 到期。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import UTC
from typing import Any

from .. import store

#: 进程内缓存，避免每个请求都查一次库；2 秒足够快。
_CACHE_TTL_S = 2.0
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def trigger(
    conn: sqlite3.Connection,
    *,
    scope: str,
    reason: str,
    triggered_by: str,
    project_id: str | None = None,
    domain: str | None = None,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """触发一个 Kill Switch。返回记录。"""
    if scope not in {"global", "project", "project_domain"}:
        raise ValueError(f"非法 scope：{scope}")
    if scope in {"project", "project_domain"} and not project_id:
        raise ValueError("项目级 Kill Switch 必须给 project_id")
    if scope == "project_domain" and not domain:
        raise ValueError("域级 Kill Switch 必须给 domain")

    now = store.now_iso()
    expires = None
    if ttl_seconds:
        from datetime import datetime, timedelta

        expires = (
            datetime.now(UTC) + timedelta(seconds=max(60, ttl_seconds))
        ).isoformat(timespec="seconds")

    switch_id = uuid.uuid4().hex
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO kill_switches (id, scope, project_id, domain, reason,"
            " triggered_by, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (switch_id, scope, project_id, domain, reason, triggered_by, expires, now),
        )
    _cache.clear()
    return {
        "id": switch_id, "scope": scope, "project_id": project_id,
        "domain": domain, "reason": reason, "expires_at": expires,
        "created_at": now,
    }


def release(conn: sqlite3.Connection, switch_id: str, *, released_by: str) -> dict[str, Any]:
    now = store.now_iso()
    with store.write_tx(conn):
        cur = conn.execute(
            "UPDATE kill_switches SET released_at = ? WHERE id = ? AND released_at IS NULL",
            (now, switch_id),
        )
    _cache.clear()
    return {"id": switch_id, "released": max(cur.rowcount, 0) > 0, "released_by": released_by}


def active(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """当前生效的开关（未解除且未过期）。"""
    key = "all"
    hit = _cache.get(key)
    now_ts = time.monotonic()
    if hit and now_ts - hit[0] < _CACHE_TTL_S:
        return hit[1]
    now = store.now_iso()
    rows = conn.execute(
        "SELECT * FROM kill_switches WHERE released_at IS NULL"
        " AND (expires_at IS NULL OR expires_at > ?) ORDER BY created_at DESC",
        (now,),
    ).fetchall()
    items = [dict(row) for row in rows]
    _cache[key] = (now_ts, items)
    return items


def is_blocked(
    conn: sqlite3.Connection,
    *,
    project_id: str | None = None,
    domain: str | None = None,
) -> tuple[bool, str]:
    """是否被拦截。返回 (拦截, 原因)。"""
    for item in active(conn):
        scope = item["scope"]
        if scope == "global":
            return True, f"全局 Kill Switch：{item['reason']}"
        if scope == "project" and project_id and item["project_id"] == project_id:
            return True, f"项目 Kill Switch：{item['reason']}"
        if (
            scope == "project_domain"
            and project_id
            and item["project_id"] == project_id
            and domain
            and item["domain"] == domain
        ):
            return True, f"域 Kill Switch（{domain}）：{item['reason']}"
    return False, ""


def reset_cache() -> None:
    """测试用：清掉进程内缓存。"""
    _cache.clear()
