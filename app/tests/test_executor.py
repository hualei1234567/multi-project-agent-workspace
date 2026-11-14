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

import shutil as _shutil

from agent_platform import executor, scheduler, service, store
from agent_platform import executor_policy as policy

# 执行器依赖 claude CLI；没有该二进制的机器（如部署服务器）跳过本模块，
# 不把"环境缺依赖"伪装成"测试通过"。
pytestmark = pytest.mark.skipif(
    _shutil.which("claude") is None,
    reason="需要 claude CLI（执行器依赖），本机未安装",
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(db))
    monkeypatch.setenv("AGWS_PLATFORM_REPO", str(repo))
    monkeypatch.setenv("AGWS_PLATFORM_PROJECTS_ROOT", str(tmp_path / "projects"))
    cfg = tmp_path / "projects.json"
    cfg.write_text('{"projects": []}', encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_CONFIG", str(cfg))
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
            "SELECT * FROM project_agents WHERE project_id='default' AND role_key=?", (role,)
        ).fetchone()
    finally:
        conn.close()


def _enable_project(env):
    conn = _conn(env)
    try:
        conn.execute(
            "UPDATE agent_projects SET settings_json=? WHERE id='default'",
            (json.dumps({"execution_enabled": True}),),
        )
    finally:
        conn.close()


def _item(env, key="EX-100", role="frontend-engineer"):
    agent = _agent(env, role)
    service.create_task("default", {
        "title": f"任务 {key}", "task_key": key, "assignee_agent_id": agent["id"],
    })
    plan = scheduler.create_plan("default")
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
            "SELECT * FROM project_agents WHERE project_id='default' AND role_key='qa-devops'"
        ).fetchone()
        now = store.now_iso()
        conn.execute(
            "INSERT INTO dispatch_plans (id,project_id,status,strategy,created_at)"
            " VALUES ('pl1','default','draft','manual',?)", (now,),
        )
        conn.execute(
            "INSERT INTO dispatch_items (id,plan_id,project_id,task_id,agent_id,role_key,"
            " status,priority,attempt,max_attempts,result_json,created_at,updated_at)"
            " VALUES ('it1','pl1','default',NULL,?,'qa-devops','queued','P1',0,2,'{}',?,?)",
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
    # 目标仓库没有 .claude/agents/frontend-engineer.md → 回退 general-purpose
    assert result["argv"][result["argv"].index("--agent") + 1] == "general-purpose"
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
    service.create_task("default", {
        "title": "带验收标准", "task_key": "EX-105", "assignee_agent_id": agent["id"],
        "acceptance_criteria": ["接口返回 200", "有回归用例"],
    })
    plan = scheduler.create_plan("default")
    item = next(i for i in plan["items"] if i["task_id"])
    argv = executor.run_item(item["id"], dry_run=True)["argv"]
    prompt = argv[argv.index("-p") + 1]
    assert "验收标准" in prompt
    assert "接口返回 200" in prompt


# ---------------------------------------------------------------------------
# agent 定义回退
# ---------------------------------------------------------------------------
def test_agent_falls_back_when_repo_lacks_definition(env):
    """仓库没有该角色的 .claude/agents 定义时必须回退，否则 claude 直接退出。"""
    _enable_project(env)
    item = _item(env, "EX-200")
    argv = executor.run_item(item["id"], dry_run=True)["argv"]
    assert argv[argv.index("--agent") + 1] == "general-purpose"


def test_agent_uses_role_when_definition_exists(env):
    """仓库定义了该角色 → 直接用角色名。"""
    agents_dir = env.repo / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "frontend-engineer.md").write_text("---\nname: frontend-engineer\n---\n",
                                                     encoding="utf-8")
    _enable_project(env)
    item = _item(env, "EX-201")
    argv = executor.run_item(item["id"], dry_run=True)["argv"]
    assert argv[argv.index("--agent") + 1] == "frontend-engineer"


# ---------------------------------------------------------------------------
# Agent 状态同步（底部资源池）
# ---------------------------------------------------------------------------
def test_agent_status_synced_working_then_idle(env):
    """执行开始写 working、结束写 idle，否则资源池永远显示 0。"""
    agent = _agent(env, "backend-engineer")
    conn = _conn(env)
    try:
        with store.write_tx(conn):
            executor._set_agent_status(conn, agent["id"], "working", "task-1")
        row = conn.execute(
            "SELECT status, current_task_id, last_heartbeat_at FROM project_agents"
            " WHERE id = ?", (agent["id"],)
        ).fetchone()
        assert row["status"] == "working"
        assert row["current_task_id"] == "task-1"
        assert row["last_heartbeat_at"]

        with store.write_tx(conn):
            executor._set_agent_status(conn, agent["id"], "idle", None)
        row = conn.execute(
            "SELECT status, current_task_id FROM project_agents WHERE id = ?",
            (agent["id"],),
        ).fetchone()
        assert row["status"] == "idle"
        assert row["current_task_id"] is None

        assert service.overview(conn)["agent_idle"] >= 1
    finally:
        conn.close()


def test_set_agent_status_ignores_missing_agent():
    """没有 agent_id 时静默返回，不影响调度结果。"""
    executor._set_agent_status(None, None, "idle", None)


def test_bootstrapped_agents_start_idle_and_counted(env):
    """平台创建的 Agent 默认 idle，底部资源池才能显示「可用」。"""
    conn = _conn(env)
    try:
        rows = conn.execute(
            "SELECT status FROM project_agents WHERE project_id='default'"
        ).fetchall()
        assert len(rows) == 7
        assert all(row["status"] == "idle" for row in rows)
        assert service.overview(conn)["agent_idle"] == 7
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# R2 / D-2：执行粒度成本
# ---------------------------------------------------------------------------
def _fake_claude(tmp_path, payload):
    """造一个假的 claude 可执行文件，输出给定的 stream-json 结果。

    真正起 claude 会消耗额度且不可复现，这里验证的是**平台如何消费 CLI 输出**，
    所以用一个只说固定话的替身，比接真实 LLM 更严格也更确定。
    """
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        f"{payload}\n"
        "EOF\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_execution_records_provider_cost(env, tmp_path, monkeypatch):
    """D-2：CLI 给出的 total_cost_usd 必须落进 executions.actual_cost_usd。"""
    item = _item(env, key="EX-COST-1")
    _enable_project(env)
    fake = _fake_claude(
        tmp_path,
        '{"type":"result","subtype":"success","total_cost_usd":0.0425,'
        '"modelUsage":{"m1":{"costUSD":0.04,"costBasis":"list"}}}',
    )
    monkeypatch.setattr(policy, "ALLOWED_BINARY", str(fake))
    executor.run_item(item["id"])

    conn = _conn(env)
    try:
        row = conn.execute(
            "SELECT actual_cost_usd, cost_quality, link_quality, task_id"
            " FROM executions WHERE source = 'dispatcher'",
        ).fetchone()
        assert row is not None, "执行记录没写入"
        assert row["actual_cost_usd"] == 0.0425
        assert row["cost_quality"] == "actual"
        # D-1：调度器执行必须带 task 关联
        assert row["task_id"] == item["task_id"]
        assert row["link_quality"] == "linked"
    finally:
        conn.close()


def test_execution_without_cost_is_unknown_not_zero(env, tmp_path, monkeypatch):
    """D-2：CLI 没给 cost 时必须是 UNKNOWN，不能写 0 冒充账单。"""
    item = _item(env, key="EX-COST-2")
    _enable_project(env)
    fake = _fake_claude(tmp_path, '{"type":"result","subtype":"success"}')
    monkeypatch.setattr(policy, "ALLOWED_BINARY", str(fake))
    executor.run_item(item["id"])

    conn = _conn(env)
    try:
        # executions.id 是 stable_id 哈希，不能按前缀找；
        # 只有这一个调度项，直接取 source='dispatcher' 的唯一那条。
        row = conn.execute(
            "SELECT actual_cost_usd, cost_quality FROM executions"
            " WHERE source = 'dispatcher'",
        ).fetchone()
        assert row is not None
        assert row["actual_cost_usd"] is None
        assert row["cost_quality"] == "unknown"
    finally:
        conn.close()
