"""执行时间线：四态只能由证据推导，缺证据不得显示为成功。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import service, sources, store


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
    return SimpleNamespace(tmp=tmp_path, db=db_path, repo=repo)


def _iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def _event(
    idx: str, event_type: str, ok, minutes_ago: int, *, tool=None, message=""
) -> None:
    conn = store.connect()
    try:
        store.init_db(conn)
        conn.execute(
            "INSERT INTO agent_events (id, project_id, agent_id, session_id, event_type,"
            " message, tool, ok, occurred_at) VALUES (?, 'default', NULL, NULL, ?, ?, ?, ?, ?)",
            (idx, event_type, message, tool, ok, _iso(minutes_ago)),
        )
    finally:
        conn.close()


def _report(idx: str, reported_state: str, minutes_ago: int, message: str = "") -> None:
    """插入一条状态自报事件（带自报状态，ok 恒为 NULL）。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        conn.execute(
            "INSERT INTO agent_events (id, project_id, agent_id, session_id, event_type,"
            " message, tool, ok, reported_state, occurred_at)"
            " VALUES (?, 'default', NULL, NULL, 'status_report', ?, NULL, NULL, ?, ?)",
            (idx, message, reported_state, _iso(minutes_ago)),
        )
    finally:
        conn.close()


def test_no_events_is_explicitly_no_evidence(env):
    tl = service.execution_timeline("default")
    assert tl["status"] == "no_evidence"
    assert tl["total"] == 0 and tl["items"] == []
    assert tl["counts"] == {"running": 0, "failed": 0, "unverified": 0, "ok": 0}


def test_states_are_derived_from_evidence(env):
    _event("e-ok", "subagent_run", 1, 5)
    _event("e-fresh-unfinished", "subagent_run", 0, 3)
    _event("e-stale-unfinished", "subagent_run", 0, 90)
    _event("e-fresh-report", "status_report", None, 2)
    _event("e-stale-report", "status_report", None, 120)
    _event("e-failed-tool", "tool_use", 0, 10, tool="Bash")

    tl = service.execution_timeline("default")
    assert tl["status"] == "has_evidence"
    by_id = {item["id"]: item["state"] for item in tl["items"]}
    assert by_id == {
        "e-ok": "ok",
        "e-fresh-unfinished": "running",
        "e-stale-unfinished": "unverified",
        "e-fresh-report": "running",
        "e-stale-report": "unverified",
        "e-failed-tool": "failed",
    }
    assert tl["counts"] == {"running": 2, "failed": 1, "unverified": 2, "ok": 1}
    # 最新事件在最前
    assert tl["items"][0]["id"] == "e-fresh-report"


def test_status_report_echoes_reported_state_without_becoming_evidence(env):
    """自报只做回显：窗口内的「运行中」算运行中，过期的仍是未验证，自报空闲绝不等于成功。"""
    _report("r-fresh-running", "running", 3)
    _report("r-stale-running", "running", 120)
    _report("r-idle", "idle", 1, "DDP-212 已合入 b5d510c；329 pytest 通过")
    _report("r-blocked", "blocked", 2)

    tl = service.execution_timeline("default")
    by_id = {item["id"]: item for item in tl["items"]}
    assert by_id["r-fresh-running"]["state"] == "running"
    assert by_id["r-stale-running"]["state"] == "unverified"
    assert by_id["r-idle"]["state"] == "unverified"
    assert by_id["r-idle"]["reported_label"] == "空闲"
    assert by_id["r-blocked"]["reported_label"] == "阻塞"
    # 自报不产生「成功」
    assert tl["counts"]["ok"] == 0
    assert all(item["state"] != "ok" for item in tl["items"])


def test_agent_card_echoes_self_report(env):
    """Agent 卡片要能回显「自报状态/任务」，同时执行状态仍按证据判定。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        conn.execute(
            "INSERT INTO project_agents (id, project_id, role_key, instance_key, agent_type,"
            " execution_mode, display_name, status, metadata_json, last_heartbeat_at, last_seen_at)"
            " VALUES ('ag-1', 'default', 'backend-engineer', 'backend-engineer', 'subagent',"
            " 'worktree', 'Backend Engineer', 'idle', ?, ?, ?)",
            (
                json.dumps({"self_report": {"state": "idle", "task": "DDP-212",
                                            "note": "已合入 b5d510c", "commit": "b5d510c"}}),
                _iso(60), _iso(60),
            ),
        )
        conn.execute(
            "INSERT INTO agent_events (id, project_id, agent_id, session_id, event_type,"
            " message, tool, ok, reported_state, occurred_at)"
            " VALUES ('ag-1-ev', 'default', 'ag-1', NULL, 'status_report',"
            " '已合入 b5d510c', NULL, NULL, 'idle', ?)",
            (_iso(20),),
        )
    finally:
        conn.close()

    tl = service.execution_timeline("default")
    card = next(a for a in tl["agents"] if a["agent_id"] == "ag-1")
    assert card["reported"]["label"] == "空闲"
    assert card["reported"]["task"] == "DDP-212"
    assert card["state"] == "unverified"      # 自报空闲不是执行证据

    activity = service.agent_activity("default")
    act = next(a for a in activity["agents"] if a["agent_id"] == "ag-1")
    assert act["reported_label"] == "空闲"
    assert act["self_report"]["commit"] == "b5d510c"


def test_lifecycle_events_are_not_execution_evidence(env):
    """项目/任务创建事件不代表有人跑过，不能把「未运行」装饰成「成功」。"""
    _event("life-1", "project_created", 1, 1)
    _event("life-2", "task_created", 1, 1)
    tl = service.execution_timeline("default")
    assert tl["status"] == "no_evidence"
    assert tl["total"] == 0


def test_state_filter_and_validation(env):
    _event("a", "tool_use", 0, 5, tool="Edit")
    _event("b", "subagent_run", 1, 6)
    only_failed = service.execution_timeline("default", state="failed")
    assert [item["id"] for item in only_failed["items"]] == ["a"]
    assert only_failed["total"] == 2
    with pytest.raises(service.PlatformError) as err:
        service.execution_timeline("default", state="whatever")
    assert err.value.code == "invalid_payload"


def test_limit_is_capped(env):
    for i in range(5):
        _event(f"x{i}", "tool_use", 1, i + 1)
    tl = service.execution_timeline("default", limit=2)
    assert len(tl["items"]) == 2 and tl["total"] == 5


def test_agent_summary_uses_latest_event(env):
    _event("a1", "subagent_run", 1, 30)
    _event("a2", "tool_use", 0, 10)
    _event("b1", "subagent_run", 1, 20)
    tl = service.execution_timeline("default")
    by_name = {a["agent_id"]: a for a in tl["agents"]}
    # 两条事件都没有 agent_id，归入同一「未归属」桶
    assert list(by_name) == [None]
    entry = by_name[None]
    assert entry["agent_name"] == "未归属"
    assert entry["state"] == "failed"          # 最近一条是失败
    assert entry["counts"] == {"running": 0, "failed": 1, "unverified": 0, "ok": 2}
    assert entry["last_activity_at"] == tl["items"][0]["occurred_at"]


def test_agent_activity_reports_state_and_history(env):
    conn = store.connect()
    try:
        agent = conn.execute("SELECT id, role_key FROM project_agents LIMIT 1").fetchone()
    finally:
        conn.close()
    assert agent is not None
    _event("act-ok", "subagent_run", 1, 5)
    conn = store.connect()
    try:
        conn.execute("UPDATE agent_events SET agent_id = ? WHERE id = 'act-ok'", (agent["id"],))
    finally:
        conn.close()

    data = service.agent_activity("default")
    entry = next(a for a in data["agents"] if a["agent_id"] == agent["id"])
    assert entry["state"] == "ok"
    assert entry["counts"]["ok"] == 1
    assert entry["events"][0]["id"] == "act-ok"
    # 没有任何证据的 Agent 必须是 none，而不是「正常」
    others = [a for a in data["agents"] if a["agent_id"] != agent["id"]]
    assert others and all(a["state"] == "none" for a in others)


def test_agent_activity_explains_missing_evidence(env):
    data = service.agent_activity("default")
    assert data["collection"]["event_total"] == 0
    assert data["collection"]["execution_total"] == 0
    assert "未采集到任何执行事件" in (data["collection"]["note"] or "")
    assert all(a["state"] == "none" for a in data["agents"])


def test_executions_using_role_key_still_map_to_agent(env):
    """历史采集里 executions.agent_id 可能是 role_key，不能因此显示「无执行记录」。"""
    conn = store.connect()
    agent = conn.execute("SELECT id, role_key FROM project_agents ORDER BY role_key LIMIT 1").fetchone()
    conn.execute(
        "INSERT INTO executions (id, project_id, agent_id, session_id, status, started_at, finished_at)"
        " VALUES ('ex-role-key', 'default', ?, 's-1', 'succeeded', ?, ?)",
        (agent["role_key"], _iso(20), _iso(10)),
    )
    conn.close()

    data = service.agent_activity("default")
    entry = next(a for a in data["agents"] if a["agent_id"] == agent["id"])
    assert entry["execution_total"] == 1
    assert entry["last_execution"]["status"] == "succeeded"
    assert entry["state"] == "ok"
