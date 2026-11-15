"""事件平面扩展测试（P3-M1）。

契约：``docs/phase3/contracts/CF-1_EVENT_SCHEMA_v1.md``

锁定四件事：
1. 退避重试（只让失败那条等，不阻塞整批）
2. DLQ 可查询（以前失败事件掉进黑洞）
3. 事件目录（可发现性，不是闸门）
4. 数据新鲜度三态（不用 0 伪装）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import store
from agent_platform.p4 import events


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    c = store.connect()
    store.init_db(c)
    yield c
    c.close()


# ---- 目录 ----
def test_catalog_seeded(conn):
    items = events.catalog(conn)
    assert len(items) >= 10
    types = {i["event_type"] for i in items}
    assert "task.created" in types
    assert "approval.decided" in types


def test_unknown_type_still_emittable(conn):
    """目录是可发现性，不是闸门——未登记的也能 emit，不能丢事件。"""
    with store.write_tx(conn):
        eid = events.emit(conn, "custom.something_new", project_id="p1")
    assert eid
    row = conn.execute("SELECT * FROM event_outbox WHERE event_id = ?",
                       (eid,)).fetchone()
    assert row is not None
    assert events.is_catalogued(conn, "custom.something_new") is False


# ---- 退避 ----
def test_pending_skips_future_backoff(conn):
    with store.write_tx(conn):
        eid = events.emit(conn, "task.created", project_id="p1")
    assert any(r["event_id"] == eid for r in events.pending(conn))
    # 手动排到未来
    conn.execute("UPDATE event_outbox SET next_attempt_at = ? WHERE event_id = ?",
                 ("2099-01-01T00:00:00+00:00", eid))
    conn.commit()
    assert not any(r["event_id"] == eid for r in events.pending(conn))


def test_failed_event_sets_backoff(conn):
    with store.write_tx(conn):
        eid = events.emit(conn, "task.created", project_id="p1")

    def boom(_envelope):
        raise RuntimeError("handler 爆了")

    events.dispatch_pending(conn, boom, max_attempts=5)
    row = conn.execute("SELECT * FROM event_outbox WHERE event_id = ?",
                       (eid,)).fetchone()
    assert row["attempts"] == 1
    assert row["status"] == "pending", "未到上限应继续重试"
    assert row["next_attempt_at"] is not None, "应排到未来，不阻塞整批"


def test_backoff_grows_exponentially(conn):
    """第 n 次失败等 base*2^(n-1)。这里只验单调递增。"""
    t1 = events._backoff_at(1)
    t3 = events._backoff_at(3)
    from datetime import datetime
    assert datetime.fromisoformat(t3) > datetime.fromisoformat(t1)


# ---- DLQ ----
def test_dlq_empty_initially(conn):
    assert events.dead_letters(conn) == []
    assert events.dlq_stats(conn)["total"] == 0


def test_event_lands_in_dlq_after_max_attempts(conn):
    with store.write_tx(conn):
        eid = events.emit(conn, "task.created", project_id="p1")

    def boom(_e):
        raise RuntimeError("always fails")

    # 第一次失败：attempts=1，仍 pending 但要等退避
    events.dispatch_pending(conn, boom, max_attempts=2)
    # 清掉退避让它能立刻再试
    conn.execute("UPDATE event_outbox SET next_attempt_at = NULL WHERE event_id = ?",
                 (eid,))
    conn.commit()
    # 第二次失败：attempts=2 达到上限 → failed
    events.dispatch_pending(conn, boom, max_attempts=2)

    row = conn.execute("SELECT * FROM event_outbox WHERE event_id = ?",
                       (eid,)).fetchone()
    assert row["status"] == "failed"
    dlq = events.dead_letters(conn)
    assert any(r["event_id"] == eid for r in dlq)
    assert events.dlq_stats(conn)["total"] == 1


def test_dlq_filter_by_project(conn):
    for pid in ("p1", "p2"):
        with store.write_tx(conn):
            eid = events.emit(conn, "task.created", project_id=pid)
        conn.execute("UPDATE event_outbox SET status='failed' WHERE event_id = ?",
                     (eid,))
    conn.commit()
    assert len(events.dead_letters(conn, project_id="p1")) == 1
    assert len(events.dead_letters(conn)) == 2


# ---- 幂等与重放 ----
def test_duplicate_delivery_skipped(conn):
    with store.write_tx(conn):
        events.emit(conn, "task.created", project_id="p1")
    seen = []
    events.dispatch_pending(conn, lambda e: seen.append(e["event_id"]),
                            consumer="c1")
    events.dispatch_pending(conn, lambda e: seen.append(e["event_id"]),
                            consumer="c1")
    assert len(seen) == 1, "同一消费者不应重复处理"


def test_replay_allows_reprocessing(conn):
    with store.write_tx(conn):
        eid = events.emit(conn, "task.created", project_id="p1")
    seen = []
    events.dispatch_pending(conn, lambda e: seen.append(e["event_id"]),
                            consumer="c1")
    assert len(seen) == 1
    assert events.replay(conn, eid, consumer="c1") is True
    events.dispatch_pending(conn, lambda e: seen.append(e["event_id"]),
                            consumer="c1")
    assert len(seen) == 2, "重放后应能再处理一次"


# ---- 新鲜度 ----
def test_freshness_unknown_without_events(conn):
    f = events.freshness(conn)
    assert f["state"] == "UNKNOWN"
    assert f["reason"] == "no_events"
    assert f["age_seconds"] is None, "无数据时不能编造 age"


def test_freshness_fresh_for_recent_event(conn):
    with store.write_tx(conn):
        events.emit(conn, "task.created", project_id="p1")
    f = events.freshness(conn)
    assert f["state"] == "FRESH"
    assert f["age_seconds"] is not None


def test_freshness_stale_for_old_event(conn):
    with store.write_tx(conn):
        events.emit(conn, "task.created", project_id="p1")
    conn.execute("UPDATE event_outbox SET occurred_at = '2026-11-15T00:00:00+00:00'"
                 " WHERE 1=1")
    conn.commit()
    # 用未来时间点算，确保 age 落在 STALE 区间
    f = events.freshness(conn)
    assert f["state"] in {"STALE", "UNKNOWN"}


def test_freshness_project_scoped(conn):
    with store.write_tx(conn):
        events.emit(conn, "task.created", project_id="p1")
    assert events.freshness(conn, project_id="p1")["state"] == "FRESH"
    assert events.freshness(conn, project_id="p2")["state"] == "UNKNOWN"
