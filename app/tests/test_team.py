"""项目 Agent Team：真实 Claude Code 会话的启动、指令与观测（子进程全部打桩）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import service, sources, store, team

SESSION = "11111111-2222-3333-4444-555555555555"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_real_ccusage(monkeypatch):
    monkeypatch.setattr(sources, "ccusage_session_costs", list)
    # 测试不读真实的 ~/.claude.json；默认视为已信任，未信任场景单独覆盖
    monkeypatch.setattr(team, "_trusted", lambda repo: True)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "platform.db"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "ai-team" / "TASK_BOARD.md", "# Board\n\n| Task ID | 标题 |\n|---|---|\n")
    # 默认项目定义好总负责人角色（真实项目里有 .claude/agents/general-owner.md）
    _write(repo / ".claude" / "agents" / "general-owner.md", "# General Owner\n")
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(db_path))
    config_file = tmp_path / "projects.json"
    config_file.write_text(json.dumps({"projects": []}), encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_CONFIG", str(config_file))
    monkeypatch.setenv("AGWS_PLATFORM_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("AGWS_PLATFORM_REPO", str(repo))
    service.bootstrap(repo_root=repo)
    return SimpleNamespace(tmp=tmp_path, repo=repo, db=db_path)


def _project(env):
    return service._project_dict(store.connect().execute(
        "SELECT * FROM agent_projects WHERE id = 'default'").fetchone())


def _fake_run(monkeypatch, *, stdout="", stderr="", returncode=0, agents_stdout="[]"):
    calls = []

    def run(args, cwd, timeout):
        calls.append({"args": list(args), "cwd": str(cwd), "timeout": timeout})
        # preflight 会先调 `claude agents --json`，必须返回合法 JSON
        out = agents_stdout if "agents" in args else stdout
        return subprocess.CompletedProcess(args, returncode, out, stderr)

    monkeypatch.setattr(team, "_run", run)
    return calls


def test_preflight_surfaces_every_blocker(env, monkeypatch):
    monkeypatch.delenv(team.EXECUTOR_ENV, raising=False)
    monkeypatch.setattr(team, "_claude", lambda: "")
    project = _project(env)
    pre = team.preflight(project)
    assert pre["ready"] is False
    keys = {c["key"]: c for c in pre["checks"]}
    assert keys["cli"]["ok"] is False
    assert keys["switch"]["ok"] is False
    assert keys["repo"]["ok"] is True
    assert "trust" in keys


def test_launch_requires_executor_switch(env, monkeypatch):
    monkeypatch.delenv(team.EXECUTOR_ENV, raising=False)
    monkeypatch.setattr(team, "_claude", lambda: "/usr/bin/claude")
    with pytest.raises(team.TeamError) as err:
        team.launch_team(_project(env), "开始干活")
    assert err.value.code == "team_preflight_failed" and err.value.status == 409


def test_launch_parses_session_and_records(env, monkeypatch):
    monkeypatch.setenv(team.EXECUTOR_ENV, "on")
    monkeypatch.setattr(team, "_claude", lambda: "/usr/bin/claude")
    calls = _fake_run(monkeypatch, stdout=f"started {SESSION}\n")
    result = team.launch_team(_project(env), "把 DDP-105 做完")
    assert result["session_id"] == SESSION
    launch_call = next(c for c in calls if "--bg" in c["args"])
    assert launch_call["args"][:2] == ["/usr/bin/claude", "--agent"]
    assert launch_call["args"][2] == "general-owner"
    assert "General Owner" in launch_call["args"][-1]
    assert launch_call["cwd"] == str(env.repo)
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM team_sessions WHERE project_id = 'default'").fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'team.launch'").fetchone()
    finally:
        conn.close()
    assert row["session_id"] == SESSION and row["status"] == "launched"
    assert audit is not None


def test_instruct_validates_and_resumes(env, monkeypatch):
    monkeypatch.setenv(team.EXECUTOR_ENV, "on")
    monkeypatch.setattr(team, "_claude", lambda: "/usr/bin/claude")
    calls = _fake_run(monkeypatch, stdout="ok")
    team.launch_team(_project(env), "启动")
    calls.clear()
    team.instruct_team(_project(env), SESSION, "继续推进 FE 修复")
    instruct_call = next(c for c in calls if "--resume" in c["args"])
    assert instruct_call["args"][:3] == ["/usr/bin/claude", "--bg", "--resume"]
    assert instruct_call["args"][3] == SESSION
    with pytest.raises(team.TeamError) as bad:
        team.instruct_team(_project(env), "not-a-session", "x")
    assert bad.value.code == "not_found"  # 解析不到会话，明确 404 而不是误发指令
    with pytest.raises(team.TeamError) as invalid:
        team.instruct_team(_project(env), "bad id!", "x")
    assert invalid.value.code == "invalid_payload"


def test_status_merges_live_sessions(env, monkeypatch):
    monkeypatch.setenv(team.EXECUTOR_ENV, "on")
    monkeypatch.setattr(team, "_claude", lambda: "/usr/bin/claude")
    live = [{"pid": 1, "cwd": str(env.repo), "kind": "interactive",
             "sessionId": SESSION, "name": "General Owner", "status": "busy"}]
    _fake_run(monkeypatch, agents_stdout=json.dumps(live))
    conn = store.connect()
    try:
        store.init_db(conn)
        status = team.team_status(_project(env), conn)
    finally:
        conn.close()
    assert status["total"] == 1 and status["running"] == 1
    assert status["sessions"][0]["sessionId"] == SESSION
    assert status["executor_enabled"] is True


def test_untrusted_workspace_blocks_launch(env, monkeypatch):
    monkeypatch.setenv(team.EXECUTOR_ENV, "on")
    monkeypatch.setattr(team, "_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(team, "_trusted", lambda repo: False)
    pre = team.preflight(_project(env))
    trust = next(c for c in pre["checks"] if c["key"] == "trust")
    assert trust["ok"] is False
    assert "claude" in trust["detail"]
    with pytest.raises(team.TeamError) as err:
        team.launch_team(_project(env), "开始")
    assert "工作区信任" in err.value.message


def test_short_session_id_is_resolved_to_full(env, monkeypatch):
    """claude --bg 只打印短 id；要能通过 agents --json 解析成全 id。"""
    monkeypatch.setenv(team.EXECUTOR_ENV, "on")
    monkeypatch.setattr(team, "_claude", lambda: "/usr/bin/claude")
    short = "94eaba41"
    live = [{"pid": 1, "cwd": str(env.repo), "kind": "background",
             "id": short, "sessionId": SESSION, "status": "busy"}]
    _fake_run(
        monkeypatch,
        stdout=f"backgrounded · {short}\n  claude attach {short}\n",
        agents_stdout=json.dumps(live),
    )
    result = team.launch_team(_project(env), "只回复 OK")
    assert result["session_id"] == SESSION


def test_launch_uses_configured_owner_agent(env, monkeypatch):
    monkeypatch.setenv(team.EXECUTOR_ENV, "on")
    monkeypatch.setattr(team, "_claude", lambda: "/usr/bin/claude")
    _write(env.repo / ".claude" / "agents" / "system-architect.md", "# System Architect\n")
    service.patch_project("default", {"settings_json": {"team_owner_agent": "system-architect"}})
    calls = _fake_run(monkeypatch, stdout=f"started {SESSION}\n")
    team.launch_team(_project(env), "做架构评估")
    launch = next(c for c in calls if "--bg" in c["args"])
    assert launch["args"][:3] == ["/usr/bin/claude", "--agent", "system-architect"]


def test_launch_skips_agent_when_definition_missing(env, monkeypatch):
    """项目没有 .claude/agents/<role>.md 时不要传 --agent（CLI 只会警告并退回默认模板）。"""
    monkeypatch.setenv(team.EXECUTOR_ENV, "on")
    monkeypatch.setattr(team, "_claude", lambda: "/usr/bin/claude")
    (env.repo / ".claude" / "agents" / "general-owner.md").unlink()
    calls = _fake_run(monkeypatch, stdout=f"backgrounded · {SESSION}\n")
    result = team.launch_team(_project(env), "待命")
    launch = next(c for c in calls if "--bg" in c["args"])
    assert "--agent" not in launch["args"]
    assert result["agent_defined"] is False
    assert "默认模板" in (result["note"] or "")


def test_session_logs_returns_tail(env, monkeypatch):
    monkeypatch.setattr(team, "_claude", lambda: "/usr/bin/claude")
    lines = "\n".join(f"line-{i}" for i in range(500))
    _fake_run(monkeypatch, stdout=lines, agents_stdout=json.dumps([
        {"id": "94eaba41", "sessionId": SESSION, "cwd": str(env.repo),
         "kind": "background", "status": "busy"},
    ]))
    out = team.session_logs(_project(env), "94eaba41", lines=50)
    assert out["ok"] is True
    # CLI 认短 id：请求传全 id 也要换回短 id
    out2 = team.session_logs(_project(env), SESSION, lines=50)
    assert out2["ok"] is True
    assert out["line_count"] == 50
    assert out["log"].splitlines()[-1] == "line-499"
    assert out["session_id"] == SESSION
    assert out["supported"] is True


def test_session_logs_explains_interactive_sessions(env, monkeypatch):
    monkeypatch.setattr(team, "_claude", lambda: "/usr/bin/claude")
    _fake_run(monkeypatch, agents_stdout=json.dumps([
        {"sessionId": SESSION, "cwd": str(env.repo), "kind": "interactive", "status": "idle"},
    ]))
    out = team.session_logs(_project(env), SESSION)
    assert out["ok"] is True and out["supported"] is False
    assert "交互式" in out["log"]
