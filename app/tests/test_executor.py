"""执行器状态机与拒绝路径（M3-1 T3-103）。

**不真正启动 claude 进程**：只验证租约、状态流转、各道拒绝闸门与 dry-run。
真实进程启动属于人工验收项，不作为自动化测试（避免 CI 里跑 LLM）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import executor, scheduler, service, store
from agent_platform import executor_policy as policy


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
    monkeypatch.setenv(policy.ENV_SWITCH, "on")
    service.bootstrap(repo_root=repo)
    return SimpleNamespace(db=db, repo=repo)


def _conn(env):
    conn = store.connect(env.db)
    store.init_db(conn)
    return conn


def _agent(env, role="frontend-engineer"):
    conn = _conn(env)
    try:
        return conn.execute(
            "SELECT * FROM project_agents WHERE project_id='ddp' AND role_key=?", (role,)
        ).fetchone()
    finally:
        conn.close()


def _enable_project(env):
    conn = _conn(env)
    try:
        conn.execute(
            "UPDATE agent_projects SET settings_json=? WHERE id='ddp'",
            (json.dumps({"execution_enabled": True}),),
        )
    finally:
        conn.close()


def _item(env, key="EX-100", role="frontend-engineer"):
    agent = _agent(env, role)
    service.create_task("ddp", {
        "title": f"任务 {key}", "task_key": key, "assignee_agent_id": agent["id"],
    })
    plan = scheduler.create_plan("ddp")
    return next(i for i in plan["items"] if i["role_key"] == role)


# ---------------------------------------------------------------------------
# 拒绝路径
# ---------------------------------------------------------------------------
def test_rejected_when_project_not_enabled(env):
    item = _item(env)
    with pytest.raises(executor.ExecutorError) as exc:
        executor.run_item(item["id"])
    assert exc.value.code == "project_disabled"
    # 拒绝后不能留下 running 状态
    conn = _conn(env)
    try:
        row = conn.execute("SELECT status FROM dispatch_items WHERE id=?", (item["id"],)).fetchone()
        assert row["status"] == "failed"
    finally:
        conn.close()


def test_rejected_when_global_switch_off(env, monkeypatch):
    monkeypatch.setenv(policy.ENV_SWITCH, "off")
    _enable_project(env)
    item = _item(env)
    with pytest.raises(executor.ExecutorError) as exc:
        executor.run_item(item["id"])
    assert exc.value.code == "executor_disabled"


def test_rejected_for_readonly_role(env):
    """readonly 角色根本进不了调度；这里直接构造 item 验证执行器也会挡。"""
    _enable_project(env)
    conn = _conn(env)
    try:
        qa = conn.execute(
            "SELECT * FROM project_agents WHERE project_id='ddp' AND role_key='qa-devops'"
        ).fetchone()
        now = store.now_iso()
        conn.execute(
            "INSERT INTO dispatch_plans (id,project_id,status,strategy,created_at)"
            " VALUES ('pl1','ddp','draft','manual',?)", (now,),
        )
        conn.execute(
            "INSERT INTO dispatch_items (id,plan_id,project_id,task_id,agent_id,role_key,"
            " status,priority,attempt,max_attempts,result_json,created_at,updated_at)"
            " VALUES ('it1','pl1','ddp',NULL,?,'qa-devops','queued','P1',0,2,'{}',?,?)",
            (qa["id"], now, now),
        )
    finally:
        conn.close()
    with pytest.raises(executor.ExecutorError) as exc:
        executor.run_item("it1")
    assert exc.value.code == "role_not_executable"


# ---------------------------------------------------------------------------
# 租约
# ---------------------------------------------------------------------------
def test_claim_and_release(env):
    item = _item(env, "EX-101")
    conn = _conn(env)
    try:
        claimed = executor.claim_item(conn, item["id"], "owner-a")
        assert claimed["status"] == "leased"
        assert claimed["lease_owner"] == "owner-a"
        # 第二次租用被拒（未过期）
        with pytest.raises(executor.ExecutorError) as exc:
            executor.claim_item(conn, item["id"], "owner-b")
        assert exc.value.code == "already_leased"
        executor.release_lease(conn, item["id"])
        row = conn.execute("SELECT status, lease_owner FROM dispatch_items WHERE id=?",
                           (item["id"],)).fetchone()
        assert row["status"] == "queued"
        assert row["lease_owner"] is None
    finally:
        conn.close()


def test_claim_writes_event(env):
    item = _item(env, "EX-102")
    conn = _conn(env)
    try:
        executor.claim_item(conn, item["id"], "owner-a")
        events = conn.execute(
            "SELECT event_type, from_status, to_status FROM dispatch_events WHERE item_id=?",
            (item["id"],),
        ).fetchall()
        assert [(e["event_type"], e["from_status"], e["to_status"]) for e in events] == [
            ("claimed", "queued", "leased")
        ]
    finally:
        conn.close()


def test_claim_unknown_item(env):
    conn = _conn(env)
    try:
        with pytest.raises(executor.ExecutorError) as exc:
            executor.claim_item(conn, "nope", "x")
        assert exc.value.code == "not_found"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------
def test_dry_run_returns_argv_and_releases_lease(env):
    _enable_project(env)
    item = _item(env, "EX-103")
    result = executor.run_item(item["id"], dry_run=True)
    assert result["dry_run"] is True
    assert result["argv"][0] == "claude"
    assert result["argv"][result["argv"].index("--agent") + 1] == "frontend-engineer"
    assert "EX-103" in result["argv"][result["argv"].index("-p") + 1]
    conn = _conn(env)
    try:
        row = conn.execute("SELECT status FROM dispatch_items WHERE id=?", (item["id"],)).fetchone()
        assert row["status"] == "queued"
    finally:
        conn.close()


def test_prompt_contains_task_and_guardrails(env):
    _enable_project(env)
    item = _item(env, "EX-104")
    result = executor.run_item(item["id"], dry_run=True)
    prompt = result["argv"][result["argv"].index("-p") + 1]
    assert "EX-104" in prompt
    assert "只在本工作目录内改动" in prompt
    assert "不要执行 git push" in prompt


def test_prompt_includes_acceptance_criteria(env):
    _enable_project(env)
    agent = _agent(env)
    service.create_task("ddp", {
        "title": "带验收标准", "task_key": "EX-105", "assignee_agent_id": agent["id"],
        "acceptance_criteria": ["接口返回 200", "有回归用例"],
    })
    plan = scheduler.create_plan("ddp")
    item = next(i for i in plan["items"] if i["task_id"])
    argv = executor.run_item(item["id"], dry_run=True)["argv"]
    prompt = argv[argv.index("-p") + 1]
    assert "验收标准" in prompt
    assert "接口返回 200" in prompt
