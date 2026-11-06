"""调度器排程逻辑（三期 M3-1 T3-102）。

调度器只做计算：验证「谁该做什么」排得对，不验证执行。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import scheduler, service, store


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(db))
    monkeypatch.setenv("DDP_AGENT_PLATFORM_REPO", str(repo))
    monkeypatch.setenv("DDP_AGENT_PLATFORM_PROJECTS_ROOT", str(tmp_path / "projects"))
    cfg = tmp_path / "projects.json"
    cfg.write_text('{"projects": []}', encoding="utf-8")
    monkeypatch.setenv("DDP_AGENT_PLATFORM_CONFIG", str(cfg))
    service.bootstrap(repo_root=repo)
    fe = _agent_id(env_db=db, role="frontend-engineer")
    return SimpleNamespace(db=db, repo=repo, fe=fe)


def _agent_id(env_db: Path, role: str) -> str:
    conn = store.connect(env_db)
    try:
        row = conn.execute(
            "SELECT id FROM project_agents WHERE project_id='default' AND role_key=?", (role,)
        ).fetchone()
        return row["id"]
    finally:
        conn.close()


def _task(env, key, *, priority="P2", agent=None, status="todo"):
    task = service.create_task("default", {
        "title": f"任务 {key}", "task_key": key, "priority": priority,
        "assignee_agent_id": agent or env.fe,
    })["task"]
    if status != "todo":
        service.transition_task("default", task["id"], {
            "to_status": "in_progress", "expected_version": task["version"],
        })
    return task


def test_unassigned_task_not_schedulable(env):
    task = service.create_task("default", {"title": "无人任务", "task_key": "S-001"})["task"]
    ready, skipped = _schedulable(env)
    assert task["id"] not in {t["id"] for t in ready}
    assert any(s["task_key"] == "S-001" and "未指派" in s["reason"] for s in skipped)


def test_assigned_todo_task_is_schedulable(env):
    task = _task(env, "S-002")
    ready, _ = _schedulable(env)
    assert task["id"] in {t["id"] for t in ready}


def test_readonly_role_not_schedulable(env):
    """readonly 角色（qa-devops）不应被自动拉起：自动执行会绕过人工评审。"""
    qa = _agent_id(env.db, "qa-devops")
    task = _task(env, "S-003", agent=qa)
    ready, skipped = _schedulable(env)
    assert task["id"] not in {t["id"] for t in ready}
    assert any(s["task_key"] == "S-003" and "不允许自动执行" in s["reason"] for s in skipped)


def test_main_session_role_not_schedulable(env):
    """general-owner 是 main_session，不是可拉起的子代理。"""
    owner = _agent_id(env.db, "general-owner")
    task = _task(env, "S-003b", agent=owner)
    ready, _ = _schedulable(env)
    assert task["id"] not in {t["id"] for t in ready}


def test_subagent_role_is_schedulable(env):
    """subagent 模式（PM/架构/UX）允许自动执行。"""
    architect = _agent_id(env.db, "system-architect")
    task = _task(env, "S-003c", agent=architect)
    ready, _ = _schedulable(env)
    assert task["id"] in {t["id"] for t in ready}


def test_blocked_by_unfinished_dependency(env):
    first = _task(env, "S-004")
    second = _task(env, "S-005")
    service.add_dependency("default", second["id"], {"depends_on_task_id": first["id"]})
    ready, skipped = _schedulable(env)
    ready_ids = {t["id"] for t in ready}
    assert first["id"] in ready_ids
    assert second["id"] not in ready_ids
    assert any(s["task_key"] == "S-005" and "未完成" in s["reason"] for s in skipped)


def test_dependency_satisfied_after_done(env):
    first = _task(env, "S-006")
    second = _task(env, "S-007")
    service.add_dependency("default", second["id"], {"depends_on_task_id": first["id"]})
    # first: todo -> in_progress -> review -> done
    v = service.get_task("default", first["id"])["task"]["version"]
    service.transition_task("default", first["id"], {"to_status": "in_progress", "expected_version": v})
    v = service.get_task("default", first["id"])["task"]["version"]
    service.transition_task("default", first["id"], {"to_status": "review", "expected_version": v})
    v = service.get_task("default", first["id"])["task"]["version"]
    service.transition_task("default", first["id"], {"to_status": "done", "expected_version": v})

    ready, _ = _schedulable(env)
    assert second["id"] in {t["id"] for t in ready}


def test_priority_ordering(env):
    low = _task(env, "S-008", priority="P3")
    high = _task(env, "S-009", priority="P0")
    ready, _ = _schedulable(env)
    order = [t["id"] for t in ready]
    assert order.index(high["id"]) < order.index(low["id"])


def test_plan_creates_items_and_is_idempotent_against_active(env):
    task = _task(env, "S-010")
    plan = scheduler.create_plan("default")
    assert plan["status"] == "draft"
    keys = [i["task_id"] for i in plan["items"]]
    assert task["id"] in keys

    # 计划里的项在 queued 状态：同一任务不应再进新计划
    ready, skipped = _schedulable(env)
    assert task["id"] not in {t["id"] for t in ready}
    assert any(s["task_key"] == "S-010" and "在途" in s["reason"] for s in skipped)


def test_plan_max_items(env):
    for i in range(3):
        _task(env, f"S-02{i}")
    plan = scheduler.create_plan("default", max_items=2)
    assert len(plan["items"]) == 2


def test_plan_detail_and_list(env):
    _task(env, "S-030")
    plan = scheduler.create_plan("default")
    detail = scheduler.get_plan("default", plan["id"])
    assert detail["id"] == plan["id"]
    assert detail["items"]
    listing = scheduler.list_plans("default")
    assert listing["total"] >= 1
    item = scheduler.get_item("default", detail["items"][0]["id"])
    assert item["status"] == "queued"
    assert item["logs"] == []


def test_invalid_strategy_rejected(env):
    with pytest.raises(scheduler.SchedulerError):
        scheduler.create_plan("default", strategy="whatever")


def _schedulable(env):
    conn = store.connect(env.db)
    try:
        store.init_db(conn)
        return scheduler.schedulable(conn, "default")
    finally:
        conn.close()
