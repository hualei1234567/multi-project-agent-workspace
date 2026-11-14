"""成本口径层（R2 / D-2）：真实账单、估算、未知三者必须分开。

审计发现 6,780 条 ``token_usage_events`` 的 ``cost_usd`` 与 ``price_version``
全为空，报表却把它当「0 成本」或干脆不显示——这两种都不对。本模块确立三条规则：

1. **真实优先**：``actual_cost_usd`` 只来自可核对的账单（如 Claude CLI 输出里的
   ``total_cost_usd``，属于 provider 侧计算）。任何本地推算都不得写进这一列。
2. **估算必须带版本**：``estimated_cost_usd`` 由 ``model_price_versions`` 里
   **带生效区间**的价格算出，且必须同时写 ``price_version``。没有匹配到价格
   就保持 NULL，不允许用「当前单价」倒填历史。
3. **未知保留未知**：三列都为空就是 UNKNOWN，报表显示 ``None`` 与覆盖率，
   绝不补 0——「不知道花了多少」和「花了 0 元」是完全不同的结论。

价格来源：``ccusage`` 对同一份 transcript 的计费是四条 token 维度的线性函数，
用多会话做最小二乘可**精确**反解出单价（本机实测残差 0.0000%）。因此建价格版本时
以 ccusage 的数据为证据，而不是抄一张会过期的价目表。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import store

#: 成本质量（写进 ``token_usage_events.cost_quality``）。
COST_ACTUAL = "actual"           # 来自 provider 账单
COST_ESTIMATED = "estimated"     # 由带版本价格算出
COST_LEGACY = "legacy_ledger"    # 历史 cost_usd 非空但来源不明
COST_ZERO = "not_billable"       # token 为 0：确定不计费，不是「不知道」
COST_UNKNOWN = "unknown"         # 无从判断，保持 NULL


def _match_pattern(model: str, pattern: str) -> bool:
    """模型名匹配：支持 ``%`` 通配，逐字相等优先。

    不用 SQL 的 LIKE，是因为价格表很小（个位数行），内存里匹配更可控，
    也避免模型名里出现 ``_`` 被 LIKE 的 ``_`` 通配符误伤。
    """
    model = (model or "").strip().lower()
    pattern = (pattern or "").strip().lower()
    if not model or not pattern:
        return False
    if "%" not in pattern:
        return model == pattern
    parts = [p for p in pattern.split("%")]
    if not parts[0] and not parts[-1]:
        return all(p in model for p in parts if p)
    if not parts[0]:
        return model.endswith(parts[-1]) and all(p in model for p in parts[1:] if p)
    if not parts[-1]:
        return model.startswith(parts[0]) and all(p in model for p in parts[:-1] if p)
    return model.startswith(parts[0]) and model.endswith(parts[-1]) and all(
        p in model for p in parts[1:-1] if p
    )


def resolve_price(
    conn: sqlite3.Connection, model: str, occurred_at: str | None
) -> dict[str, Any] | None:
    """按模型名 + 发生时间找当时生效的价格版本；找不到返回 None。

    ``occurred_at`` 为空时不猜：宁可返回 None（→ UNKNOWN），也不拿最新价格顶上。
    """
    if not model or not occurred_at:
        return None
    rows = conn.execute(
        "SELECT * FROM model_price_versions ORDER BY effective_from DESC"
    ).fetchall()
    stamp = store.iso_utc(occurred_at) or occurred_at
    best: sqlite3.Row | None = None
    for row in rows:
        if not _match_pattern(model, row["model_pattern"]):
            continue
        start = store.iso_utc(row["effective_from"]) or row["effective_from"]
        end = store.iso_utc(row["effective_to"]) if row["effective_to"] else None
        if start and stamp < start:
            continue
        if end and stamp >= end:
            continue
        # 同区间内取最具体（无通配优先）的那条
        if best is None or ("%" not in row["model_pattern"]) > (
            "%" not in best["model_pattern"]
        ):
            best = row
    return dict(best) if best is not None else None


def estimate_usd(price: dict[str, Any], usage: dict[str, Any]) -> float:
    """按价格版本算估算成本（USD）。四个维度都按 per-million-token 计价。"""
    per_mtok = (
        ("input_tokens", float(price["input_usd_per_mtok"] or 0)),
        ("output_tokens", float(price["output_usd_per_mtok"] or 0)),
        ("cache_read_tokens", float(price["cache_read_usd_per_mtok"] or 0)),
        ("cache_write_tokens", float(price["cache_write_usd_per_mtok"] or 0)),
    )
    total = 0.0
    for key, rate in per_mtok:
        total += int(usage.get(key) or 0) * rate / 1_000_000
    return total


def price_version_id(price: dict[str, Any] | None) -> str | None:
    return str(price["id"]) if price else None


def add_price_version(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """登记一个价格版本。生效区间必填 from；to 为空表示至今有效。"""
    pattern = str(payload.get("model_pattern") or "").strip()
    start = str(payload.get("effective_from") or "").strip()
    if not pattern:
        raise ValueError("model_pattern 必填")
    if not start:
        raise ValueError("effective_from 必填：没有生效时间就无法解释历史成本")
    vid = store.stable_id(
        "price", pattern, start, payload.get("provider") or "anthropic"
    )
    conn.execute(
        "INSERT OR REPLACE INTO model_price_versions"
        " (id, provider, model_pattern, effective_from, effective_to,"
        "  input_usd_per_mtok, output_usd_per_mtok,"
        "  cache_read_usd_per_mtok, cache_write_usd_per_mtok,"
        "  source, source_detail, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            vid,
            str(payload.get("provider") or "anthropic"),
            pattern,
            start,
            payload.get("effective_to") or None,
            float(payload.get("input_usd_per_mtok") or 0),
            float(payload.get("output_usd_per_mtok") or 0),
            float(payload.get("cache_read_usd_per_mtok") or 0),
            float(payload.get("cache_write_usd_per_mtok") or 0),
            str(payload.get("source") or "manual"),
            payload.get("source_detail"),
            store.now_iso(),
        ),
    )
    row = conn.execute("SELECT * FROM model_price_versions WHERE id = ?", (vid,)).fetchone()
    return dict(row)


def backfill_estimates(
    conn: sqlite3.Connection, *, apply: bool = False, limit: int | None = None
) -> dict[str, Any]:
    """给历史 token 事件补估算成本。默认 dry-run（只统计，不写）。

    只处理 ``actual_cost_usd IS NULL AND estimated_cost_usd IS NULL`` 的记录，
    且必须命中带生效区间的价格版本；命中不了的保持 UNKNOWN 并在结果里计数，
    让「覆盖率」这一指标本身可被质疑、可被核对。
    """
    sql = (
        "SELECT id, model, occurred_at, total_tokens, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens FROM token_usage_events"
        " WHERE actual_cost_usd IS NULL AND estimated_cost_usd IS NULL"
        " ORDER BY occurred_at"
    )
    params: tuple[Any, ...] = ()
    if limit:
        sql += " LIMIT ?"
        params = (int(limit),)
    rows = conn.execute(sql, params).fetchall()
    priced: list[tuple[str, float, str]] = []
    zero: list[str] = []
    unmatched: dict[str, int] = {}
    for row in rows:
        if int(row["total_tokens"] or 0) <= 0:
            # 零 token 的消息一定不产生费用：这是确定结论，不是未知。
            # 单独记一档，免得它把「成本覆盖率」稀释成看起来很低。
            zero.append(row["id"])
            continue
        price = resolve_price(conn, str(row["model"] or ""), row["occurred_at"])
        if price is None:
            key = str(row["model"] or "unknown")
            unmatched[key] = unmatched.get(key, 0) + 1
            continue
        usage = {
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cache_read_tokens": row["cache_read_tokens"],
            "cache_write_tokens": row["cache_write_tokens"],
        }
        priced.append((row["id"], estimate_usd(price, usage), str(price["id"])))
    result = {
        "scanned": len(rows),
        "priced": len(priced),
        "zero_token": len(zero),
        "unmatched": sum(unmatched.values()),
        "unmatched_by_model": unmatched,
        "total_estimated_usd": round(sum(p[1] for p in priced), 6),
        "applied": False,
    }
    if apply and (priced or zero):
        with store.write_tx(conn):
            for event_id, amount, price_id in priced:
                conn.execute(
                    "UPDATE token_usage_events SET estimated_cost_usd = ?,"
                    " price_version = ?, cost_quality = ? WHERE id = ?",
                    (amount, price_id, COST_ESTIMATED, event_id),
                )
            for event_id in zero:
                conn.execute(
                    "UPDATE token_usage_events SET estimated_cost_usd = 0,"
                    " cost_quality = ? WHERE id = ?",
                    (COST_ZERO, event_id),
                )
        result["applied"] = True
    return result


def cost_coverage(conn: sqlite3.Connection, project_id: str | None = None) -> dict[str, Any]:
    """成本覆盖率：actual / estimated / unknown 各多少，金额分开汇总。

    报表必须能回答「我知道多少、还有多少不知道」，所以覆盖率是核心指标，
    未知金额用 ``None`` 表示而不是 0。
    """
    where, params = "", ()
    if project_id:
        where, params = " WHERE project_id = ?", (project_id,)
    row = conn.execute(
        "SELECT COUNT(*) AS n,"
        " SUM(CASE WHEN cost_quality = 'actual' THEN 1 ELSE 0 END) AS actual_n,"
        " SUM(CASE WHEN cost_quality = 'estimated' THEN 1 ELSE 0 END) AS estimated_n,"
        " SUM(CASE WHEN cost_quality = 'legacy_ledger' THEN 1 ELSE 0 END) AS legacy_n,"
        " SUM(CASE WHEN cost_quality = 'not_billable' THEN 1 ELSE 0 END) AS zero_n,"
        " SUM(CASE WHEN cost_quality IS NULL OR cost_quality = 'unknown'"
        "          THEN 1 ELSE 0 END) AS unknown_n,"
        " SUM(actual_cost_usd) AS actual_usd,"
        " SUM(estimated_cost_usd) AS estimated_usd"
        f" FROM token_usage_events{where}",
        params,
    ).fetchone()
    total = int(row["n"] or 0)
    actual_n = int(row["actual_n"] or 0)
    estimated_n = int(row["estimated_n"] or 0)
    legacy_n = int(row["legacy_n"] or 0)
    zero_n = int(row["zero_n"] or 0)
    unknown_n = int(row["unknown_n"] or 0)
    # 覆盖率的分母去掉「确定不计费」的记录：它们不是待解决的问题。
    billable = total - zero_n
    return {
        "project_id": project_id,
        "events": total,
        "billable_events": billable,
        "actual_events": actual_n,
        "estimated_events": estimated_n,
        "legacy_events": legacy_n,
        "zero_token_events": zero_n,
        "unknown_events": unknown_n,
        "actual_cost_usd": round(float(row["actual_usd"]), 6) if actual_n else None,
        "estimated_cost_usd": (
            round(float(row["estimated_usd"]), 6) if estimated_n else None
        ),
        "coverage": round((actual_n + estimated_n) / billable, 4) if billable else 0.0,
        "coverage_note": "覆盖率 = (真实+估算) / 应计费事件数；零 token 事件不计入分母",
        "unknown_disclosed": True,
    }
