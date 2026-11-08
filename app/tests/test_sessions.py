"""会话实时流：从 transcript 还原 Agent 真正在做什么。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import service, store

SLUG = "-tmp-repo"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _lines() -> str:
    records = [
        {"timestamp": "2026-11-08T10:00:00Z",
         "message": {"model": "claude-x", "usage": {"input_tokens": 10, "output_tokens": 5},
                     "content": [{"type": "text", "text": "开始处理任务"}]}},
        {"timestamp": "2026-11-08T10:00:01Z",
         "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                  "input": {"file_path": "docs/a.md"}}]}},
        {"timestamp": "2026-11-08T10:00:02Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                  "content": "内容", "is_error": False}]}},
    ]
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "platform.db"
    projects_root = tmp_path / "claude-projects"
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
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE agent_projects SET session_slugs = ? WHERE id = 'default'",
            (json.dumps([SLUG]),),
        )
    finally:
        conn.close()
    return SimpleNamespace(tmp=tmp_path, root=projects_root, repo=repo, slug=SLUG)


def _main_session(env, session_id="sess-main-1"):
    path = env.root / env.slug / f"{session_id}.jsonl"
    _write(path, _lines())
    return path


def test_list_sessions_reports_running_and_last_step(env):
    _main_session(env)
    data = service.list_sessions("default")
    assert data["total"] == 1 and data["running"] == 1
    item = data["items"][0]
    assert item["kind"] == "main"
    assert item["status"] == "running"
    assert item["events_total"] == 2
    assert item["last_step"] == "Read"
    assert item["agent_name"]  # 主会话归到 general-owner


def test_session_trace_pairs_tool_result(env):
    _main_session(env)
    trace = service.session_trace("default", "sess-main-1")
    assert trace["ok"] is True
    tools = [e for e in trace["events"] if e["kind"] == "tool"]
    assert tools and tools[0]["tool"] == "Read"
    assert tools[0]["detail"].endswith("docs/a.md")
    assert tools[0]["result"] == "内容" and tools[0]["ok"] is True
    assert trace["tokens"] == 15
    assert trace["model"] == "claude-x"


def test_subagent_session_finished_is_done(env):
    sub = env.root / env.slug / "sess-main-1" / "subagents"
    _write(sub / "agent-abc123.jsonl", _lines())
    _write(sub / "agent-abc123.meta.json", json.dumps({
        "agentType": "frontend-engineer", "description": "实现页面", "finished": True,
    }, ensure_ascii=False))
    data = service.list_sessions("default")
    item = next(x for x in data["items"] if x["kind"] == "subagent")
    assert item["status"] == "done"
    assert item["agent_id"] == "abc123"
    assert item["description"] == "实现页面"
    assert service.session_trace("default", "sess-main-1")["kind"] in {"main", "subagent"}


def test_unknown_session_is_not_found(env):
    with pytest.raises(service.PlatformError) as err:
        service.session_trace("default", "nope")
    assert err.value.code == "not_found" and err.value.status == 404


def test_project_slugs_union_derived(env):
    """配置里的 slug 可能是别的机器的；扫描时始终并上由 repo_root 推导的本机 slug。"""
    from agent_platform import sources as src

    project = service.get_project("default")
    derived = str(project["repo_root"]).replace("/", "-")
    assert derived in src.project_slugs(project)
