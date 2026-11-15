"""三模式测试（P3-M3）。

契约：``docs/phase3/contracts/CF-4_THREE_MODES_v1.md``

核心保证是**结构性**的，不是靠约定：
`shadow` / `recommend` 根本不写 `dispatch_items`，而执行器只认
`dispatch_items`——所以这两种模式在结构上不可能起进程。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import registry, scheduler, store


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    conn = store.connect()
    store.init_db(conn)
    conn.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('p1','p1','项目一','/tmp/r','2026-11-15T00:00:00+00:00',"
        " '2026-11-15T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO project_agents (id, project_id, role_key, display_name,"
        " instance_key, agent_type, execution_mode)"
        " VALUES ('a1','p1','backend-engineer','BE','a1','claude','worktree')")
    conn.execute(
        "INSERT INTO tasks (id, project_id, task_key, title, status, priority,"
        " assignee_agent_id, source, updated_at, version)"
        " VALUES ('t1','p1','t1','任务一','todo','P1','a1','platform',"
        " '2026-11-15T00:00:00+00:00',1)")
    conn.commit()
    yield conn
    conn.close()


def _counts(conn, plan_id):
    items = conn.execute(
        "SELECT COUNT(*) FROM dispatch_items WHERE plan_id = ?", (plan_id,)
    ).fetchone()[0]
    cands = conn.execute(
        "SELECT COUNT(*) FROM dispatch_plan_candidates WHERE plan_id = ?", (plan_id,)
    ).fetchone()[0]
    return items, cands


def _cleanup(conn, plan_id):
    conn.execute("DELETE FROM dispatch_plan_candidates WHERE plan_id = ?", (plan_id,))
    conn.execute("DELETE FROM dispatch_items WHERE plan_id = ?", (plan_id,))
    conn.execute("DELETE FROM dispatch_plans WHERE id = ?", (plan_id,))
    conn.commit()


# ---- 结构保证 ----
def test_shadow_creates_no_items(env):
    """SHADOW 绝不能创建 dispatch_item——执行器看不到就不可能执行。"""
    plan = scheduler.create_plan("p1", mode="shadow")
    items, cands = _counts(env, plan["id"])
    assert items == 0, "SHADOW 不能创建 dispatch_item"
    assert cands == 1, "SHADOW 应写候选"
    assert plan["creates_dispatch_items"] is False
    _cleanup(env, plan["id"])


def test_recommend_creates_no_items(env):
    plan = scheduler.create_plan("p1", mode="recommend")
    items, cands = _counts(env, plan["id"])
    assert items == 0, "RECOMMEND 不能创建 dispatch_item"
    assert cands == 1
    _cleanup(env, plan["id"])


def test_assisted_creates_items(env):
    plan = scheduler.create_plan("p1", mode="assisted")
    items, cands = _counts(env, plan["id"])
    assert items == 1, "ASSISTED 才创建 dispatch_item"
    assert cands == 0, "ASSISTED 不需要候选表"
    assert plan["creates_dispatch_items"] is True
    _cleanup(env, plan["id"])


def test_default_mode_is_recommend(env):
    """平台默认必须是 RECOMMEND，不是直接可执行。"""
    assert scheduler.DEFAULT_MODE == "recommend"
    plan = scheduler.create_plan("p1")
    assert plan["mode"] == "recommend"
    items, _ = _counts(env, plan["id"])
    assert items == 0, "默认模式不能创建可执行项"
    _cleanup(env, plan["id"])


def test_invalid_mode_rejected(env):
    with pytest.raises(scheduler.SchedulerError) as exc:
        scheduler.create_plan("p1", mode="full_auto")
    assert exc.value.code == "invalid_mode"


# ---- 候选可解释 ----
def test_candidates_have_score_and_reason(env):
    plan = scheduler.create_plan("p1", mode="recommend")
    cands = plan["candidates"]
    assert len(cands) == 1
    c = cands[0]
    assert c["score"] > 0
    assert c["reason"], "每个候选必须能回答「为什么推荐它」"
    assert "优先级" in c["reason"]
    detail = c["score_detail"]
    for key in ("priority", "dependency", "capability", "wait"):
        assert key in detail
    _cleanup(env, plan["id"])


def test_candidate_rank_order_by_score(env):
    env.execute(
        "INSERT INTO tasks (id, project_id, task_key, title, status, priority,"
        " assignee_agent_id, source, updated_at, version)"
        " VALUES ('t2','p1','t2','低优先级','todo','P3','a1','platform',"
        " '2026-11-15T00:00:00+00:00',1)")
    env.commit()
    plan = scheduler.create_plan("p1", mode="recommend")
    cands = plan["candidates"]
    assert len(cands) == 2
    # P1 应排在 P3 前面
    assert cands[0]["score"] >= cands[1]["score"]
    assert cands[0]["rank"] == 1
    assert cands[1]["rank"] == 2
    _cleanup(env, plan["id"])


def test_missing_capability_does_not_penalize(env):
    """没登记能力不减分，只标记 unknown——不能用数据缺失惩罚功能。"""
    plan = scheduler.create_plan("p1", mode="recommend")
    c = plan["candidates"][0]
    assert c["score_detail"]["capability"] == 0
    assert c["score_detail"]["capability_note"] == "capability_unknown"
    # 但优先级和依赖分照给
    assert c["score_detail"]["priority"] == 30
    assert c["score_detail"]["dependency"] == 20
    _cleanup(env, plan["id"])


def test_capability_boosts_score(env):
    """登记了能力的角色，评分应高于没登记的同一任务。"""
    plan_before = scheduler.create_plan("p1", mode="recommend")
    before = plan_before["candidates"][0]["score"]
    _cleanup(env, plan_before["id"])

    registry.create_role(env, role_key="backend-engineer", display_name="BE",
                         execution_mode="worktree")
    registry.add_capability(env, role_id="role_backend-engineer",
                            capability="coding", proficiency=5,
                            evidence="R3 实测")

    plan_after = scheduler.create_plan("p1", mode="recommend")
    after = plan_after["candidates"][0]["score"]
    assert after > before, f"有能力应加分：{before} → {after}"
    _cleanup(env, plan_after["id"])


# ---- 硬过滤先于评分 ----
def test_revoked_role_excluded_before_scoring(env):
    """Registry 撤销的角色不进候选——硬过滤在评分之前。"""
    registry.create_role(env, role_key="backend-engineer", display_name="BE",
                         execution_mode="worktree")
    env.execute("UPDATE registry_agent_roles SET status='revoked'"
                " WHERE role_key='backend-engineer'")
    env.commit()
    ready, skipped = scheduler.schedulable(env, "p1")
    assert ready == [], "撤销角色不应进入候选"
    assert any("Registry" in s["reason"] for s in skipped)


def test_readonly_role_excluded(env):
    env.execute("UPDATE project_agents SET execution_mode='readonly' WHERE id='a1'")
    env.commit()
    ready, skipped = scheduler.schedulable(env, "p1")
    assert ready == []
    assert any("不允许自动执行" in s["reason"] for s in skipped)


# ---- 历史计划兼容 ----
def test_legacy_plan_defaults_to_assisted(env):
    """v24 之前创建的计划没有 mode 列，迁移默认标 assisted。
    标成 recommend 会撒谎——历史计划确实是可执行形态。"""
    env.execute(
        "INSERT INTO dispatch_plans (id, project_id, status, strategy, created_by,"
        " created_at, version) VALUES ('old1','p1','draft','priority','local',"
        " '2026-11-15T00:00:00+00:00', 1)")
    env.commit()
    row = env.execute("SELECT mode FROM dispatch_plans WHERE id='old1'").fetchone()
    assert row["mode"] == "assisted"
    env.execute("DELETE FROM dispatch_plans WHERE id='old1'")
    env.commit()
