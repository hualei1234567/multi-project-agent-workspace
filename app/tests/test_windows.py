"""窗口派发：跨机器窗口登记、绑定总负责人、指令队列与回报。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import service, store, windows


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ai-team").mkdir()
    (repo / "ai-team" / "TASK_BOARD.md").write_text("# Board\n", encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"projects": []}), encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_CONFIG", str(config))
    (tmp_path / "projects").mkdir()
    monkeypatch.setenv("AGWS_PLATFORM_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("AGWS_PLATFORM_REPO", str(repo))
    service.bootstrap(repo_root=repo)
    return SimpleNamespace(repo=repo, tmp=tmp_path)


def _report(repo: Path, session_id: str = "s-1", name: str = "General Owner 总指挥") -> None:
    windows.report_sessions("mac-1", [{
        "sessionId": session_id, "name": name, "cwd": str(repo),
        "kind": "interactive", "status": "idle", "pid": 4242, "project_id": "default",
    }])


def test_report_lists_window_for_project(env):
    _report(env.repo)
    with store.session() as conn:
        data = windows.list_windows(conn, "default")
    assert [w["session_id"] for w in data["windows"]] == ["s-1"]
    win = data["windows"][0]
    assert win["online"] is True and win["label"] == "General Owner 总指挥"
    assert data["bound"] is None


def test_bind_marks_owner_window(env):
    _report(env.repo)
    windows.bind_window("default", "mac-1", "s-1")
    with store.session() as conn:
        data = windows.list_windows(conn, "default")
    assert data["bound"]["device_id"] == "mac-1"
    assert data["bound"]["session_id"] == "s-1"
    assert data["bound"]["online"] is True


def test_dispatch_queue_and_result(env):
    _report(env.repo)
    created = windows.enqueue_command("default", "mac-1", "s-1", "推进 DDP-216")
    assert created["status"] == "pending"

    pulled = windows.pull_commands("mac-1")
    assert [c["id"] for c in pulled["commands"]] == [created["id"]]
    # 领取后不重复下发
    assert windows.pull_commands("mac-1")["commands"] == []

    windows.command_result("mac-1", created["id"], ok=True, session_id="copy-9", result="backgrounded · copy-9")
    with store.session() as conn:
        data = windows.list_windows(conn, "default")
    cmd = next(c for c in data["commands"] if c["id"] == created["id"])
    assert cmd["status"] == "done"
    assert cmd["session_id"] == "copy-9"      # 实际续跑的会话（可能是副本）


def test_offline_window_rejects_dispatch(env):
    _report(env.repo)
    with store.session() as conn:
        conn.execute("UPDATE device_sessions SET reported_at = '2000-01-01T00:00:00+00:00'")
    with pytest.raises(windows.WindowError) as err:
        windows.enqueue_command("default", "mac-1", "s-1", "推进")
    assert err.value.code == "window_offline"


def test_unknown_window_and_empty_instruction(env):
    _report(env.repo)
    with pytest.raises(windows.WindowError) as err:
        windows.enqueue_command("default", "mac-1", "nope", "推进")
    assert err.value.code == "not_found"
    with pytest.raises(windows.WindowError) as err2:
        windows.enqueue_command("default", "mac-1", "s-1", "   ")
    assert err2.value.code == "invalid_payload"


def test_agent_activity_exposes_bound_window_on_owner(env):
    """绑定后：总负责人卡片要能拿到「窗口」信息（settings_json 是字符串或 dict 都要支持）。"""
    _report(env.repo)
    windows.bind_window("default", "mac-1", "s-1")
    data = service.agent_activity("default")
    owner = next((a for a in data["agents"] if a.get("window")), None)
    assert owner is not None, "没有任何 Agent 带上窗口信息"
    assert owner["window"]["session_id"] == "s-1"
    assert owner["window"]["device_id"] == "mac-1"
    assert owner["window"]["online"] is True
    assert str(owner.get("role_key") or "").lower() in ("general-owner", "owner")
