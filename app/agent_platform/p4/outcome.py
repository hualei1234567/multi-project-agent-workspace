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
    """读取 usage ledger，按 R2 / D-2 的三档口径汇总。

    * ``actual_cost_usd``：provider 账单（CLI 的 total_cost_usd）；
    * ``estimated_cost_usd``：按带生效区间的价格版本算出来的估算；
    * 两者都空 = UNKNOWN，**不补 0**，并给出覆盖率让读者判断可信度。

    历史 ``cost_usd`` 列不再作为口径：它来源不明，混进任何一边都是错的。
    """
    from .. import cost as cost_module

    tokens = conn.execute(
        "SELECT COALESCE(SUM(total_tokens), 0) FROM token_usage_events WHERE project_id = ?",
        (project_id,),
    ).fetchone()[0]
    cov = cost_module.cost_coverage(conn, project_id)
    return {
        "project_id": project_id,
        "events": cov["events"],
        "tokens": int(tokens or 0),
        "events_with_cost": cov["actual_events"] + cov["estimated_events"],
        "actual_cost_usd": cov["actual_cost_usd"],
        "estimated_cost_usd": cov["estimated_cost_usd"],
        "known_cost_usd": (
            (cov["actual_cost_usd"] or 0) + (cov["estimated_cost_usd"] or 0)
            if (cov["actual_cost_usd"] is not None
                or cov["estimated_cost_usd"] is not None) else None
        ),
        "coverage": cov["coverage"],
        "unknown_events": cov["unknown_events"],
        "zero_token_events": cov["zero_token_events"],
        "quality": (
            "verified" if cov["events"] and cov["coverage"] >= 1.0
            else "partial" if cov["actual_events"] or cov["estimated_events"]
            else "unknown"
        ),
        "note": "actual 为 provider 账单，estimated 为价格版本估算；未知不补 0",
    }
