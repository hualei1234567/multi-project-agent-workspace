"""Full Auto 测试（P3-M9）。

契约：``docs/phase3/contracts/CF-6_FULL_AUTO_v1.md``

这是平台风险最高的模块，测试重点是**拒绝**而不是**执行**：
- 前置不全时必须 fail-closed
- 全局开关默认 off
- dry_run 是默认
- Full Auto 不绕过预算/熔断/Kill Switch
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import full_auto, store
from agent_platform.p4 import killswitch


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    monkeypatch.delenv(full_auto.ENV_SWITCH, raising=False)
    c = store.connect()
    store.init_db(c)
    c.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('p1','p1','项目一','/tmp/r','2026-11-15T00:00:00+00:00',"
        " '2026-11-15T00:00:00+00:00')")
    c.commit()
    killswitch.reset_cache()
    yield c
    c.close()
    killswitch.reset_cache()


def _add_task(conn):
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


# ---- 开关默认关闭 ----
def test_disabled_by_default(conn):
    assert full_auto.enabled() is False


def test_run_refused_when_disabled(conn):
    _add_task(conn)
    r = full_auto.run_round(conn, "p1", dry_run=False)
    assert r["ok"] is False
    assert r["reason"] == "full_auto_disabled"


# ---- fail-closed 前置 ----
def test_preflight_blocks_without_budget(conn):
    pre = full_auto.preflight(conn, "p1")
    assert pre["ready"] is False
    assert any(c["id"] == "budget" and not c["ok"] for c in pre["checks"])


def test_preflight_blocks_when_executor_off(conn, monkeypatch):
    monkeypatch.delenv("AGWS_PLATFORM_EXECUTOR", raising=False)
    pre = full_auto.preflight(conn, "p1")
    assert pre["ready"] is False
    assert any(c["id"] == "executor" and not c["ok"] for c in pre["checks"])


def test_preflight_blocks_when_tripped(conn):
    from agent_platform import circuit
    circuit.trip(conn, "p1", reason="测试熔断")
    pre = full_auto.preflight(conn, "p1")
    assert pre["ready"] is False
    assert any(c["id"] == "not_tripped" and not c["ok"] for c in pre["checks"])


def test_preflight_passes_with_all_prereqs(conn, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_EXECUTOR", "on")
    from agent_platform import budget
    budget.set_budget(conn, scope="project", scope_id="p1",
                      window="monthly", limit_usd=100.0)
    pre = full_auto.preflight(conn, "p1")
    assert pre["ready"] is True, [c for c in pre["checks"] if not c["ok"]]


# ---- 试算不执行 ----
def test_plan_round_does_not_execute(conn, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_EXECUTOR", "on")
    from agent_platform import budget
    budget.set_budget(conn, scope="project", scope_id="p1",
                      window="monthly", limit_usd=100.0)
    _add_task(conn)
    before = conn.execute("SELECT COUNT(*) AS c FROM dispatch_items").fetchone()["c"]
    r = full_auto.plan_round(conn, "p1")
    after = conn.execute("SELECT COUNT(*) AS c FROM dispatch_items").fetchone()["c"]
    assert after == before, "试算不能创建 dispatch_item"
    assert r["ok"] is True
    assert len(r["candidates"]) == 1


def test_plan_respects_max_per_round(conn, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_EXECUTOR", "on")
    from agent_platform import budget
    budget.set_budget(conn, scope="project", scope_id="p1",
                      window="monthly", limit_usd=100.0)
    _add_task(conn)
    conn.execute(
        "INSERT INTO tasks (id, project_id, task_key, title, status, priority,"
        " assignee_agent_id, source, updated_at, version)"
        " VALUES ('t2','p1','t2','任务二','todo','P2','a1','platform',"
        " '2026-11-15T00:00:00+00:00',1)")
    conn.commit()
    r = full_auto.plan_round(conn, "p1")
    assert len(r["candidates"]) <= full_auto.MAX_PER_ROUND


def test_dry_run_is_default(conn, monkeypatch):
    """run_round 默认 dry_run=True——想真跑必须显式传 False。"""
    monkeypatch.setenv(full_auto.ENV_SWITCH, "on")
    monkeypatch.setenv("AGWS_PLATFORM_EXECUTOR", "on")
    from agent_platform import budget
    budget.set_budget(conn, scope="project", scope_id="p1",
                      window="monthly", limit_usd=100.0)
    _add_task(conn)
    r = full_auto.run_round(conn, "p1")  # 不传 dry_run
    assert r.get("dry_run") is True
    assert "created" not in r, "dry_run 不应创建任何计划"


# ---- 不绕过既有闸门 ----
def test_blocked_by_budget(conn, monkeypatch):
    """Full Auto 不能绕过预算——这是它存在的意义。"""
    monkeypatch.setenv(full_auto.ENV_SWITCH, "on")
    monkeypatch.setenv("AGWS_PLATFORM_EXECUTOR", "on")
    from agent_platform import budget
    budget.set_budget(conn, scope="project", scope_id="p1",
                      window="daily", limit_usd=1.0)
    _add_task(conn)
    conn.execute(
        "INSERT INTO executions (id, project_id, session_id, status, started_at,"
        " actual_cost_usd) VALUES ('e1','p1','s1','succeeded',?,5.0)",
        (store.now_iso(),))
    conn.commit()
    r = full_auto.plan_round(conn, "p1")
    assert r["candidates"] == [], "预算超支时不能派发"


def test_blocked_by_circuit(conn, monkeypatch):
    monkeypatch.setenv(full_auto.ENV_SWITCH, "on")
    monkeypatch.setenv("AGWS_PLATFORM_EXECUTOR", "on")
    from agent_platform import budget, circuit
    budget.set_budget(conn, scope="project", scope_id="p1",
                      window="monthly", limit_usd=100.0)
    _add_task(conn)
    circuit.trip(conn, "p1", reason="测试")
    r = full_auto.plan_round(conn, "p1")
    assert r["ok"] is False  # preflight 拦下


def test_max_auto_risk_is_r2(conn):
    """Full Auto 只放开 R0–R2；R3+ 仍需审批。"""
    assert full_auto.MAX_AUTO_RISK == "R2"


def test_max_per_round_is_one(conn):
    """每轮最多 1 项：无人值守并发改同一仓库会互相踩。"""
    assert full_auto.MAX_PER_ROUND == 1


def test_status_reports_config(conn):
    st = full_auto.status(conn, "p1")
    assert st["enabled"] is False
    assert st["env_switch"] == full_auto.ENV_SWITCH
    assert st["max_per_round"] == 1
    assert "preflight" in st
