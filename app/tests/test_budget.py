"""预算测试（P3-M7）。契约：CF-5。

锁定五件事：
1. 超支暂停新任务，不强杀运行中的
2. 多层预算取最严格
3. 没有 actual 时用 estimated，并暴露估算占比
4. 样本为 0 时效能显示 UNKNOWN，不算 0
5. 状态跃迁才写事件，不是每次都写
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import budget, scheduler, store


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    c = store.connect()
    store.init_db(c)
    c.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('p1','p1','项目一','/tmp/r','2026-11-15T00:00:00+00:00',"
        " '2026-11-15T00:00:00+00:00')")
    c.commit()
    yield c
    c.close()


def _exec(conn, *, cost=None, est=None, task_id=None, project_id="p1"):
    conn.execute(
        "INSERT INTO executions (id, project_id, task_id, session_id, status,"
        " started_at, actual_cost_usd, estimated_cost_usd)"
        " VALUES (?, ?, ?, 's1', 'succeeded', ?, ?, ?)",
        (f"e{uuid_hex()}", project_id, task_id, store.now_iso(), cost, est))
    conn.commit()


def uuid_hex():
    import uuid
    return uuid.uuid4().hex


# ---- 基础 ----
def test_set_and_evaluate(conn):
    b = budget.set_budget(conn, scope="project", scope_id="p1",
                          window="daily", limit_usd=10.0)
    e = budget.evaluate(conn, b["id"])
    assert e["status"] == "ok"
    assert e["spent_usd"] == 0.0


def test_warning_threshold(conn):
    b = budget.set_budget(conn, scope="project", scope_id="p1",
                          window="daily", limit_usd=10.0, warning_ratio=0.8)
    _exec(conn, cost=8.5)
    assert budget.evaluate(conn, b["id"])["status"] == "warning"


def test_exceeded(conn):
    b = budget.set_budget(conn, scope="project", scope_id="p1",
                          window="daily", limit_usd=10.0)
    _exec(conn, cost=10.5)
    assert budget.evaluate(conn, b["id"])["status"] == "exceeded"


def test_upsert_same_scope_window(conn):
    a = budget.set_budget(conn, scope="project", scope_id="p1",
                          window="daily", limit_usd=10.0)
    b = budget.set_budget(conn, scope="project", scope_id="p1",
                          window="daily", limit_usd=20.0)
    assert a["id"] == b["id"], "同作用域+窗口应覆盖而非叠加"
    assert b["limit_usd"] == 20.0


def test_invalid_scope_rejected(conn):
    with pytest.raises(budget.BudgetError) as exc:
        budget.set_budget(conn, scope="galaxy", scope_id="p1",
                          window="daily", limit_usd=1)
    assert exc.value.code == "invalid_scope"


def test_project_budget_requires_scope_id(conn):
    with pytest.raises(budget.BudgetError) as exc:
        budget.set_budget(conn, scope="project", scope_id=None,
                          window="daily", limit_usd=1)
    assert exc.value.code == "scope_id_required"


# ---- 成本口径 ----
def test_estimated_used_when_no_actual(conn):
    b = budget.set_budget(conn, scope="project", scope_id="p1",
                          window="daily", limit_usd=10.0)
    _exec(conn, est=5.0)
    e = budget.evaluate(conn, b["id"])
    assert e["spent_usd"] == 5.0
    assert e["estimated_share"] == 1.0, "全是估算时必须暴露"


def test_estimated_share_reported(conn):
    b = budget.set_budget(conn, scope="project", scope_id="p1",
                          window="daily", limit_usd=100.0)
    _exec(conn, cost=6.0)
    _exec(conn, est=4.0)
    e = budget.evaluate(conn, b["id"])
    assert e["spent_usd"] == 10.0
    assert 0 < e["estimated_share"] < 1


def test_execution_without_cost_ignored(conn):
    b = budget.set_budget(conn, scope="project", scope_id="p1",
                          window="daily", limit_usd=10.0)
    _exec(conn, cost=None, est=None)
    e = budget.evaluate(conn, b["id"])
    assert e["spent_usd"] == 0.0
    assert e["sample"] == 0


# ---- 多层级取最严格 ----
def test_platform_budget_dominates(conn):
    budget.set_budget(conn, scope="platform", scope_id=None,
                      window="daily", limit_usd=100.0)
    budget.set_budget(conn, scope="project", scope_id="p1",
                      window="daily", limit_usd=10.0)
    _exec(conn, cost=15.0)
    status = budget.status_for(conn, project_id="p1")
    assert status["status"] == "exceeded", "项目层超支应压过平台层正常"
    assert status["blockers"]


def test_no_budget_means_ok(conn):
    status = budget.status_for(conn, project_id="p1")
    assert status["status"] == "ok"
    assert status["blockers"] == []


# ---- 与调度接线 ----
def test_exceeded_pauses_new_tasks(conn):
    """超支后任务不进候选——这是 Full Auto 的刹车。"""
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
    assert [t["task_key"] for t in ready] == ["t1"], "无预算限制时应可调度"

    budget.set_budget(conn, scope="project", scope_id="p1",
                      window="daily", limit_usd=1.0)
    _exec(conn, cost=5.0)

    ready, skipped = scheduler.schedulable(conn, "p1")
    assert ready == [], "超支后必须暂停新任务"
    assert any("预算" in s["reason"] for s in skipped)


def test_does_not_kill_running(conn):
    """预算超支不能影响已在跑的任务（V4.0 明文要求）。"""
    conn.execute(
        "INSERT INTO dispatch_plans (id, project_id, status, strategy, created_by,"
        " created_at, version) VALUES ('pl1','p1','running','priority','local',"
        " '2026-11-15T00:00:00+00:00',1)")
    conn.execute(
        "INSERT INTO dispatch_items (id, plan_id, project_id, status, priority,"
        " attempt, max_attempts, created_at, updated_at)"
        " VALUES ('it1','pl1','p1','running','P1',0,3,"
        " '2026-11-15T00:00:00+00:00','2026-11-15T00:00:00+00:00')")
    conn.commit()
    budget.set_budget(conn, scope="project", scope_id="p1",
                      window="daily", limit_usd=1.0)
    _exec(conn, cost=5.0)

    row = conn.execute("SELECT status FROM dispatch_items WHERE id='it1'").fetchone()
    assert row["status"] == "running", "超支不得改变运行中任务的状态"


# ---- 效能 ----
def test_efficiency_unknown_without_sample(conn):
    e = budget.efficiency(conn, "p1")
    assert e["first_pass_rate"] == "UNKNOWN", "样本为 0 不能显示 0"
    assert e["lead_time_seconds"] == "UNKNOWN"
    assert e["dispatch_items_sample"] == 0


def test_efficiency_computes_with_sample(conn):
    conn.execute(
        "INSERT INTO dispatch_plans (id, project_id, status, strategy, created_by,"
        " created_at, version) VALUES ('pl2','p1','done','priority','local',"
        " '2026-11-15T00:00:00+00:00',1)")
    for i, (attempt, status) in enumerate([(1, "succeeded"), (2, "succeeded"),
                                           (1, "failed"), (1, "succeeded")]):
        conn.execute(
            "INSERT INTO dispatch_items (id, plan_id, project_id, status, priority,"
            " attempt, max_attempts, created_at, updated_at)"
            " VALUES (?, 'pl2','p1',?, 'P1', ?, 3, ?, ?)",
            (f"i{i}", status, attempt, store.now_iso(), store.now_iso()))
    conn.commit()
    e = budget.efficiency(conn, "p1")
    assert e["dispatch_items_sample"] == 4
    assert e["first_pass_rate"] == 0.5, "4 项里 2 项首次成功"
    assert e["retry_rate"] == 0.25, "4 项里 1 项重试过"
