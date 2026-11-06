"""业务价值域 V1：指标字典 / 记录 / 成本归因（P4-M6）。

口径纪律（V6.0 §10.4、§17.5）：

* ``quality`` 只能是 verified / estimated / partial / stale / unknown；
* ``unknown`` 不许填数值——不知道就是不知道，不能写成 0；
* 汇总时 **estimate 与 verified 分开统计**，绝不混算平均；
* 成本只从第三期 ``token_usage_events`` 读，不重复记账（P4-OUT-002）。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .. import store

QUALITIES = ("verified", "estimated", "partial", "stale", "unknown")


class OutcomeError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def define_metric(conn: sqlite3.Connection, project_id: str,
                  payload: dict[str, Any]) -> dict[str, Any]:
    key = str(payload.get("metric_key") or "").strip()
    name = str(payload.get("name") or "").strip()
    formula = str(payload.get("formula") or "").strip()
    if not key or not name or not formula:
        raise OutcomeError("invalid_payload", 422, "metric_key / name / formula 必填")

    metric_id = _new_id("metric")
    now = store.now_iso()
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO outcome_metric_definitions (id, project_id, metric_key, name,"
                " formula, unit, direction, owner, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (metric_id, project_id, key, name, formula, payload.get("unit"),
                 str(payload.get("direction") or "higher_better"),
                 payload.get("owner"), now),
            )
            conn.execute(
                "INSERT INTO outcome_metric_versions (id, definition_id, version, formula,"
                " created_at) VALUES (?, ?, 1, ?, ?)",
                (_new_id("mv"), metric_id, formula, now),
            )
    except sqlite3.IntegrityError as exc:
        raise OutcomeError("conflict", 409, "该指标已定义") from exc
    return get_metric(conn, project_id, key)


def get_metric(conn: sqlite3.Connection, project_id: str, metric_key: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM outcome_metric_definitions WHERE project_id = ? AND metric_key = ?",
        (project_id, metric_key),
    ).fetchone()
    if row is None:
        raise OutcomeError("not_found", 404, "指标未定义")
    return dict(row)


def list_metrics(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM outcome_metric_definitions WHERE project_id = ? ORDER BY metric_key",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


def record_value(conn: sqlite3.Connection, project_id: str,
                 payload: dict[str, Any]) -> dict[str, Any]:
    metric_key = str(payload.get("metric_key") or "").strip()
    quality = str(payload.get("quality") or "unknown")
    if quality not in QUALITIES:
        raise OutcomeError("invalid_payload", 422, f"quality 必须是 {QUALITIES}")
    get_metric(conn, project_id, metric_key)

    value = payload.get("value")
    if quality == "unknown" and value is not None:
        raise OutcomeError("invalid_payload", 422, "quality=unknown 时不得填数值")
    if quality != "unknown" and value is None:
        raise OutcomeError("invalid_payload", 422, "非 unknown 记录必须带数值")

    record_id = _new_id("out")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO outcome_records (id, project_id, metric_key, metric_version,"
            " value, quality, source, window_start, window_end, note, created_at)"
            " VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (record_id, project_id, metric_key, value, quality, payload.get("source"),
             payload.get("window_start"), payload.get("window_end"),
             payload.get("note"), store.now_iso()),
        )
    return {"id": record_id, "metric_key": metric_key, "value": value, "quality": quality}


def summarize(conn: sqlite3.Connection, project_id: str, metric_key: str) -> dict[str, Any]:
    """按 quality 分组统计；verified 与 estimated 分开，不混算。"""
    rows = conn.execute(
        "SELECT quality, COUNT(*) AS n, AVG(value) AS avg_value"
        " FROM outcome_records WHERE project_id = ? AND metric_key = ?"
        " GROUP BY quality", (project_id, metric_key),
    ).fetchall()
    buckets = {row["quality"]: {"count": row["n"], "avg": row["avg_value"]} for row in rows}
    return {
        "metric_key": metric_key,
        "buckets": buckets,
        "verified_avg": buckets.get("verified", {}).get("avg"),
        "estimated_avg": buckets.get("estimated", {}).get("avg"),
        "unknown_count": buckets.get("unknown", {}).get("count", 0),
    }


# --------------------------------------------------------------------------
# 成本：只读第三期账本
# --------------------------------------------------------------------------
def cost_summary(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """读取 token_usage_events。没有 cost 列数据的记录标 unknown，不补 0。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n, SUM(total_tokens) AS tokens,"
        " SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS with_cost,"
        " SUM(COALESCE(cost_usd, 0)) AS cost"
        " FROM token_usage_events WHERE project_id = ?", (project_id,),
    ).fetchone()
    total = row["n"] or 0
    with_cost = row["with_cost"] or 0
    return {
        "project_id": project_id,
        "events": total,
        "tokens": row["tokens"] or 0,
        "events_with_cost": with_cost,
        "known_cost_usd": row["cost"] if with_cost else None,
        "quality": "verified" if total and with_cost == total else (
            "partial" if with_cost else "unknown"),
        "note": "成本只从第三期 Usage Ledger 读取，不重复记账",
    }
