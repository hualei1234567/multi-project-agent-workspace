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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "platform.db"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "ai-team" / "TASK_BOARD.md", "# Board\n\n| Task ID | 标题 |\n|---|---|\n")
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
    launch_call = next(c for c in calls if c["args"][1] == "--bg")
    assert launch_call["args"][:2] == ["/usr/bin/claude", "--bg"]
    assert "General Owner" in launch_call["args"][2]
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
    assert bad.value.code == "invalid_payload"


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
