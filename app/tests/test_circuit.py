"""熔断器测试（P3-M9 前置）。

蓝图硬要求：「同项目连续 3 次失败自动 paused，需人工恢复」。

为什么不自动恢复：自动恢复等于没有熔断——刚恢复就继续跑，还是那些失败。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import circuit, scheduler, store
from agent_platform.p4 import killswitch


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    c = store.connect()
    store.init_db(c)
    c.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('p1','p1','项目一','/tmp/r','2026-11-15T00:00:00+00:00',"
        " '2026-11-15T00:00:00+00:00')")
    c.execute(
        "INSERT INTO dispatch_plans (id, project_id, status, strategy, created_by,"
        " created_at, version) VALUES ('pl1','p1','running','priority','local',"
        " '2026-11-15T00:00:00+00:00',1)")
    c.commit()
    killswitch.reset_cache()
    yield c
    c.close()
    killswitch.reset_cache()


def _item(conn, item_id, status, ts="2026-11-15T01:00:00+00:00"):
    conn.execute(
        "INSERT INTO dispatch_items (id, plan_id, project_id, status, priority,"
        " attempt, max_attempts, finished_at, created_at, updated_at)"
        " VALUES (?, 'pl1','p1',?, 'P1', 1, 3, ?, "
        " '2026-11-15T00:00:00+00:00', ?)",
        (item_id, status, ts, ts))
    conn.commit()


# ---- 计数 ----
def test_no_failures(conn):
    assert circuit.consecutive_failures(conn, "p1") == 0


def test_counts_consecutive(conn):
    _item(conn, "a", "failed", "2026-11-15T01:00:00+00:00")
    _item(conn, "b", "failed", "2026-11-15T02:00:00+00:00")
    assert circuit.consecutive_failures(conn, "p1") == 2


def test_success_resets_counter(conn):
    """中间成功一次就清零——这是"连续"的含义。"""
    _item(conn, "a", "failed", "2026-11-15T01:00:00+00:00")
    _item(conn, "b", "succeeded", "2026-11-15T02:00:00+00:00")
    _item(conn, "c", "failed", "2026-11-15T03:00:00+00:00")
    assert circuit.consecutive_failures(conn, "p1") == 1


# ---- 触发 ----
def test_trips_at_threshold(conn):
    for i in range(2):
        _item(conn, f"f{i}", "failed", f"2026-11-15T0{i + 1}:00:00+00:00")
    r = circuit.check_after_failure(conn, "p1")
    assert r["tripped"] is False, "2 次还不够"

    _item(conn, "f2", "failed", "2026-11-15T03:00:00+00:00")
    r = circuit.check_after_failure(conn, "p1")
    assert r["tripped"] is True
    assert r["failures"] == 3
    assert circuit.is_tripped(conn, "p1") is not None


def test_trip_is_idempotent(conn):
    for i in range(3):
        _item(conn, f"f{i}", "failed", f"2026-11-15T0{i + 1}:00:00+00:00")
    circuit.check_after_failure(conn, "p1")
    first = circuit.is_tripped(conn, "p1")
    circuit.check_after_failure(conn, "p1")
    second = circuit.is_tripped(conn, "p1")
    assert first["id"] == second["id"], "重复触发不应建多条记录"


def test_circuit_is_per_project(conn):
    """一个项目熔断不该拖垮另一个。"""
    conn.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('p2','p2','项目二','/tmp/r2','2026-11-15T00:00:00+00:00',"
        " '2026-11-15T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO dispatch_plans (id, project_id, status, strategy, created_by,"
        " created_at, version) VALUES ('pl2','p2','running','priority','local',"
        " '2026-11-15T00:00:00+00:00',1)")
    conn.commit()
    for i in range(3):
        _item(conn, f"f{i}", "failed", f"2026-11-15T0{i + 1}:00:00+00:00")
    circuit.check_after_failure(conn, "p1")
    assert circuit.is_tripped(conn, "p1") is not None
    assert circuit.is_tripped(conn, "p2") is None


# ---- 调度接线 ----
def test_tripped_blocks_scheduling(conn):
    conn.execute(
        "INSERT INTO project_agents (id, project_id, role_key, display_name,"
        " instance_key, agent_type, execution_mode)"
        " VALUES ('a1','p1','be','BE','a1','claude','worktree')")
    conn.execute(
        "INSERT INTO tasks (id, project_id, task_key, title, status, priority,"
        " assignee_agent_id, source, updated_at, version)"
        " VALUES ('t1','p1','t1','任务','todo','P1','a1','platform',"
        " '2026-11-15T00:00:00+00:00',1)")
    conn.commit()

    ready, _ = scheduler.schedulable(conn, "p1")
    assert [t["task_key"] for t in ready] == ["t1"]

    circuit.trip(conn, "p1", reason="测试熔断")
    ready, skipped = scheduler.schedulable(conn, "p1")
    assert ready == [], "熔断后不能调度"
    assert any("熔断" in s["reason"] for s in skipped)


# ---- 恢复 ----
def test_reset_requires_explicit_call(conn):
    """没有自动恢复——熔断必须人工解除。"""
    for i in range(3):
        _item(conn, f"f{i}", "failed", f"2026-11-15T0{i + 1}:00:00+00:00")
    circuit.check_after_failure(conn, "p1")
    assert circuit.is_tripped(conn, "p1") is not None
    # 再检查一次仍然是熔断状态（不会自己恢复）
    circuit.check_after_failure(conn, "p1")
    assert circuit.is_tripped(conn, "p1") is not None


def test_reset_clears_circuit(conn):
    circuit.trip(conn, "p1", reason="测试")
    r = circuit.reset(conn, "p1", released_by="owner", note="修好了")
    assert r["by"] == "owner"
    assert circuit.is_tripped(conn, "p1") is None


def test_reset_when_not_tripped_errors(conn):
    with pytest.raises(circuit.CircuitError) as exc:
        circuit.reset(conn, "p1")
    assert exc.value.code == "not_tripped"


def test_status_report(conn):
    st = circuit.status(conn, "p1")
    assert st["tripped"] is False
    assert st["threshold"] == 3
    assert st["failures"] == 0
