"""多项目 Agent 平台后端测试（真实 SQLite + fixture 仓库，不依赖真实 ~/.claude）。"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agents_dashboard as dash
from agent_platform import service, sources, store, templates

REAL_CCUSAGE_SESSION_COSTS = sources.ccusage_session_costs


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _slug_for(repo: Path) -> str:
    return repo.resolve().as_posix().replace("/", "-")


def _build_repo(repo: Path, *, title: str = "Alpha 任务") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _write(
        repo / "ai-team" / "TASK_BOARD.md",
        "\n".join([
            "# Board",
            "",
            "| Task ID | 标题 | Owner | Status | Priority | Depends On | MR | QA |",
            "|---|---|---|---|---|---|---|---|",
            f"| TEAM-001 | {title} | owner | IN_PROGRESS | P1 | — | — | — |",
            "| TEAM-002 | 已完成任务 | owner | DONE | P2 | TEAM-001 | — | — |",
            "",
        ]),
    )
    _write(
        repo / "ai-team" / "status" / "general-owner.json",
        json.dumps({
            "role": "general-owner", "state": "running", "task": "TEAM-001",
            "updated_at": "2026-11-02T10:00:00+08:00", "note": "执行中", "commit": "abc1234",
        }, ensure_ascii=False),
    )
    _write(
        repo / "ai-team" / "status" / "backend-engineer.json",
        json.dumps({
            "role": "backend-engineer", "state": "blocked", "task": "TEAM-002",
            "updated_at": "2026-11-02T09:00:00+08:00", "note": "等待接口确认",
            "worktree": "/tmp/default-be",
        }, ensure_ascii=False),
    )
    _write(repo / "ai-team" / "reports" / "TEAM-001-qa.md", "# QA\n")
    _write(repo / "docs" / "reports" / "notes.md", "# notes\n")


def _recent(hours: float = 1.0) -> str:
    """窗口内时间戳（相对当前 UTC，避免写死日期过 24h 后失效）。"""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _insert_token_event(project_id: str, session_id: str, message_uuid: str, total: int,
                        occurred_at: str | None = None) -> None:
    with store.session() as conn:
        conn.execute(
            """
            INSERT INTO token_usage_events
                (id, project_id, agent_id, session_id, message_uuid, model,
                 input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                 total_tokens, source, occurred_at)
            VALUES (?, ?, NULL, ?, ?, 'test-model', ?, 0, 0, 0, ?, 'transcript', ?)
            """,
            (store.stable_id("tok", project_id, session_id, message_uuid),
             project_id, session_id, message_uuid, total, total, occurred_at or _recent()),
        )


def _build_transcript(projects_root: Path, slug: str) -> tuple[Path, Path]:
    session_dir = projects_root / slug / "sess1"
    main = projects_root / slug / "sess1.jsonl"
    records = [
        {
            "type": "assistant", "uuid": "u1", "timestamp": "2026-11-02T02:00:00.000Z",
            "sessionId": "sess1",
            "message": {"id": "m1", "model": "test-model", "usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1,
            }},
        },
        {
            "type": "assistant", "uuid": "u2", "timestamp": "2026-11-02T02:00:01.000Z",
            "sessionId": "sess1",
            "message": {"id": "m1", "model": "test-model", "usage": {
                "input_tokens": 10, "output_tokens": 5,
                "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1,
            }},
        },
        {
            "type": "assistant", "uuid": "u3", "timestamp": "2026-11-02T02:00:02.000Z",
            "sessionId": "sess1",
            "message": {"id": "m2", "model": "test-model", "usage": {
                "input_tokens": 3, "output_tokens": 4,
            }},
        },
    ]
    _write(main, "\n".join(json.dumps(r) for r in records) + "\n")
    _write(
        session_dir / "subagents" / "agent-sub1.jsonl",
        json.dumps({
            "type": "assistant", "uuid": "s1", "timestamp": "2026-11-02T02:05:00.000Z",
            "sessionId": "sess1", "agentId": "sub1",
            "message": {"id": "m3", "model": "test-model", "usage": {
                "input_tokens": 100, "output_tokens": 0,
            }},
        }) + "\n",
    )
    _write(
        session_dir / "subagents" / "agent-sub1.meta.json",
        json.dumps({
            "agentType": "backend-engineer", "description": "实现接口",
            "finished": True, "name": "be-test",
        }, ensure_ascii=False),
    )
    return main, session_dir


@pytest.fixture(autouse=True)
def _no_real_ccusage(monkeypatch):
    """默认不调用本机 ccusage；成本用例再按需覆盖。"""
    monkeypatch.setattr(sources, "ccusage_session_costs", list)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "platform.db"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    repo = tmp_path / "repo"
    _build_repo(repo)
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(db_path))
    # 临时配置：测试库不读真实 config/projects.json，避免自动登记真实项目
    config_file = tmp_path / "projects.json"
    config_file.write_text(json.dumps({"projects": []}), encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_CONFIG", str(config_file))
    monkeypatch.setenv("AGWS_PLATFORM_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("AGWS_PLATFORM_REPO", str(repo))
    service.bootstrap(repo_root=repo)
    return SimpleNamespace(
        tmp=tmp_path, db=db_path, projects_root=projects_root, repo=repo,
        slug=_slug_for(repo),
    )


# ---------------------------------------------------------------------------
# schema / 模板
# ---------------------------------------------------------------------------
def test_schema_is_idempotent(env):
    conn = store.connect(env.db)
    store.init_db(conn)
    store.init_db(conn)
    version_rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    conn.close()
    assert version_rows == 1
    assert {
        "agent_projects", "agent_team_templates", "project_agents", "tasks",
        "deliveries", "agent_events", "token_usage_events",
    } <= tables


def test_builtin_templates_role_keys(env):
    data = service.list_team_templates()
    by_key = {item["template_key"]: item for item in data["items"]}
    assert len(by_key["standard-1-6"]["roles"]) == 7
    assert len(by_key["lean-1-3"]["roles"]) == 4
    standard_keys = [role["role_key"] for role in by_key["standard-1-6"]["roles"]]
    assert standard_keys == [
        "general-owner", "product-manager", "system-architect", "ux-designer",
        "frontend-engineer", "backend-engineer", "qa-devops",
    ]
    owner = by_key["standard-1-6"]["roles"][0]
    assert owner["agent_type"] == "main" and owner["layer_order"] == 1
    assert all(role["parent_role_key"] == "general-owner"
               for role in by_key["standard-1-6"]["roles"][1:])
    assert templates.get_template("not-exists") is None


def test_default_project_is_not_overwritten(env):
    first = service.get_project("default")
    assert first["name"] == service.DEFAULT_PROJECT_NAME
    assert len(first["agents"]) == 7
    assert env.slug in first["session_slugs"]
    service.bootstrap(repo_root=env.repo)
    second = service.get_project("default")
    assert [a["id"] for a in second["agents"]] == [a["id"] for a in first["agents"]]


# ---------------------------------------------------------------------------
# 项目 CRUD / 模板初始化
# ---------------------------------------------------------------------------
def test_create_project_initializes_template_roles(env):
    created = service.create_project({
        "code": "alpha", "name": "Alpha 项目", "description": "测试",
        "repo_root": str(env.repo), "session_slugs": [],
    })
    assert created["id"] == "alpha"
    assert len(created["agents"]) == 7
    lean = service.create_project({
        "code": "lean", "name": "Lean 项目", "team_template_key": "lean-1-3",
        "repo_root": str(env.repo), "session_slugs": [],
    })
    assert len(lean["agents"]) == 4


def test_new_project_uses_isolated_empty_workspace(env):
    created = service.create_project({"code": "solo", "name": "Solo 项目"})
    assert created["session_slugs"] == []
    workspace = env.tmp / "projects" / "solo"
    assert Path(created["repo_root"]) == workspace
    assert workspace.is_dir()
    assert env.slug not in created["session_slugs"]

    result = service.backfill_project("solo")
    assert result["counts"]["tokens"] == 0
    assert result["counts"]["tasks"] == 0
    assert result["counts"]["deliveries"] == 0
    assert service.list_tasks("solo", page_size=100)["items"] == []
    stats = service.project_stats_for("solo")
    assert stats["token_24h"] == 0
    assert stats["cost_24h"] is None


def test_cost_24h_is_project_scoped_and_null_safe(env, monkeypatch):
    now = _recent(1)
    old = _recent(72)
    monkeypatch.setattr(sources, "ccusage_session_costs", lambda: [
        {"project_path": env.slug, "session_id": "s1", "last_activity": now,
         "total_cost": 1.5, "total_tokens": 1000},
        {"project_path": "-Users-hualei-deepseek", "session_id": "s2", "last_activity": now,
         "total_cost": 2.0, "total_tokens": 200},
        {"project_path": "alpha-slug", "session_id": "s3", "last_activity": now,
         "total_cost": 3.25, "total_tokens": 400},
        {"project_path": "unrelated-slug", "session_id": "s4", "last_activity": now,
         "total_cost": 9.0, "total_tokens": 100},
        {"project_path": env.slug, "session_id": "s5", "last_activity": old,
         "total_cost": 100.0, "total_tokens": 1000},
    ])
    service.create_project({
        "code": "alpha", "name": "Alpha", "repo_root": str(env.repo),
        "session_slugs": ["alpha-slug"],
    })
    _insert_token_event("default", "s1", "m1", 200)
    _insert_token_event("default", "s2", "m2", 50)
    _insert_token_event("alpha", "s3", "m3", 100)

    # 按 token 占比分摊：s1 200/1000、s2 50/200、s3 100/400
    assert service.project_stats_for("default")["cost_24h"] == 0.8
    assert service.project_stats_for("alpha")["cost_24h"] == 0.8125
    assert service.project_usage("default") == {
        "token_24h": 250, "cost_24h": 0.8, "source": "transcript/ccusage-session",
    }
    overview = service.overview()
    assert overview["cost_24h"] == 1.6125
    costs = {item["id"]: item["stats"]["cost_24h"] for item in overview["projects"]}
    assert costs == {"default": 0.8, "alpha": 0.8125}


def test_cost_allocates_cross_day_session_by_token_share(env, monkeypatch):
    monkeypatch.setattr(sources, "ccusage_session_costs", lambda: [{
        "project_path": env.slug, "session_id": "cross-day",
        "last_activity": _recent(1),
        "total_cost": 10.0, "total_tokens": 1000,
    }])
    _insert_token_event("default", "cross-day", "part1", 200)
    # 跨天会话只分摊窗口内 200/1000 = 20%：10 * 0.2 = 2.0，而不是整段 10.0
    assert service.project_stats_for("default")["cost_24h"] == 2.0


def test_cost_is_null_when_no_window_tokens(env, monkeypatch):
    monkeypatch.setattr(sources, "ccusage_session_costs", lambda: [{
        "project_path": env.slug, "session_id": "s1",
        "last_activity": _recent(1),
        "total_cost": 5.0, "total_tokens": 0,
    }])
    assert service.project_stats_for("default")["cost_24h"] is None


def test_ccusage_session_parser_and_failures(monkeypatch):
    monkeypatch.setattr(sources, "ccusage_session_costs", REAL_CCUSAGE_SESSION_COSTS)

    class Proc:
        returncode = 0
        stdout = json.dumps({"sessions": [{
            "projectPath": "-Users-hualei-demo",
            "sessionId": "abc",
            "lastActivity": "2026-11-02T09:00:00.000Z",
            "totalCost": 1.25,
            "totalTokens": 100,
        }]})

    monkeypatch.setattr(sources, "_find_ccusage", lambda: "/fake/ccusage")
    monkeypatch.setattr(sources.subprocess, "run", lambda *args, **kwargs: Proc())
    sources.clear_ccusage_cache()
    rows = sources.ccusage_session_costs()
    assert rows == [{
        "project_path": "-Users-hualei-demo",
        "session_id": "abc",
        "last_activity": "2026-11-02T09:00:00+00:00",
        "total_cost": 1.25,
        "total_tokens": 100,
    }]

    class Broken:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(sources.subprocess, "run", lambda *args, **kwargs: Broken())
    sources.clear_ccusage_cache()
    assert sources.ccusage_session_costs() == []


def test_duplicate_and_invalid_project_code(env):
    service.create_project({"code": "alpha", "name": "Alpha", "session_slugs": []})
    with pytest.raises(service.PlatformError) as dup:
        service.create_project({"code": "alpha", "name": "Again", "session_slugs": []})
    assert dup.value.code == "project_code_exists" and dup.value.status == 409
    for bad in ("A", "Alpha", "-abc", "a", "bad_code", "x" * 25):
        with pytest.raises(service.PlatformError) as err:
            service.create_project({"code": bad, "name": "Bad", "session_slugs": []})
        assert err.value.code == "invalid_project_code" and err.value.status == 422


def test_projects_do_not_leak_tasks_or_agents(env):
    alpha_repo = env.tmp / "alpha-repo"
    beta_repo = env.tmp / "beta-repo"
    _build_repo(alpha_repo, title="Alpha 独有任务")
    _build_repo(beta_repo, title="Beta 独有任务")
    service.create_project({
        "code": "alpha", "name": "Alpha", "repo_root": str(alpha_repo), "session_slugs": [],
    })
    service.create_project({
        "code": "beta", "name": "Beta", "repo_root": str(beta_repo), "session_slugs": [],
    })
    service.backfill_project("alpha")
    service.backfill_project("beta")

    alpha_tasks = service.list_tasks("alpha", page_size=100)["items"]
    beta_tasks = service.list_tasks("beta", page_size=100)["items"]
    assert {t["task_key"] for t in alpha_tasks} == {"TEAM-001", "TEAM-002"}
    assert next(t for t in alpha_tasks if t["task_key"] == "TEAM-001")["title"] == "Alpha 独有任务"
    assert next(t for t in beta_tasks if t["task_key"] == "TEAM-001")["title"] == "Beta 独有任务"

    alpha_agents = service.get_project("alpha")["agents"]
    beta_agents = service.get_project("beta")["agents"]
    assert len(alpha_agents) == len(beta_agents) == 7
    assert not ({a["id"] for a in alpha_agents} & {a["id"] for a in beta_agents})


def test_archived_project_is_read_only(env):
    service.create_project({"code": "alpha", "name": "Alpha", "session_slugs": []})
    service.patch_project("alpha", {"status": "archived"})
    with pytest.raises(service.PlatformError) as err:
        service.backfill_project("alpha")
    assert err.value.code == "project_archived" and err.value.status == 403
    with pytest.raises(service.PlatformError):
        service.patch_project("alpha", {"name": "改名"})
    restored = service.patch_project("alpha", {"status": "active"})
    assert restored["status"] == "active"


# ---------------------------------------------------------------------------
# 采集：任务 / 状态 / 交付 / Token
# ---------------------------------------------------------------------------
def test_backfill_tasks_status_and_deliveries(env):
    result = service.backfill_project("default")
    assert result["counts"]["tasks"] == 2
    assert result["counts"]["agents"] == 7
    agents = {a["role_key"]: a for a in service.get_project("default")["agents"]}
    assert agents["general-owner"]["status"] == "working"
    assert agents["backend-engineer"]["status"] == "blocked"
    assert agents["general-owner"]["current_task_id"]
    deliveries = service.list_deliveries("default", page_size=100)["items"]
    qa = next(d for d in deliveries if d["title"] == "TEAM-001-qa.md")
    assert qa["kind"] == "test_report"
    assert qa["task_key"] == "TEAM-001"
    assert qa["task_id"]


def test_token_events_are_deduplicated_and_incremental(env):
    main, _ = _build_transcript(env.projects_root, env.slug)
    service.backfill_project("default")

    conn = sqlite3.connect(env.db)
    rows = conn.execute(
        "SELECT message_uuid, total_tokens FROM token_usage_events WHERE project_id = 'default'"
    ).fetchall()
    assert sorted(row[0] for row in rows) == ["m1", "m2", "m3"]
    assert sum(row[1] for row in rows) == 125

    service.backfill_project("default")  # 幂等：cursor 未变，不重复
    count = conn.execute(
        "SELECT COUNT(*) FROM token_usage_events WHERE project_id = 'default'"
    ).fetchone()[0]
    assert count == 3

    with main.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant", "uuid": "u4", "timestamp": "2026-11-02T02:10:00.000Z",
            "sessionId": "sess1",
            "message": {"id": "m4", "model": "test-model", "usage": {
                "input_tokens": 1000, "output_tokens": 0,
            }},
        }) + "\n")
    service.backfill_project("default")
    count = conn.execute(
        "SELECT COUNT(*) FROM token_usage_events WHERE project_id = 'default'"
    ).fetchone()[0]
    total = conn.execute(
        "SELECT SUM(total_tokens) FROM token_usage_events WHERE project_id = 'default'"
    ).fetchone()[0]
    conn.close()
    assert count == 4
    assert total == 1125


def test_overview_aggregates_project_scope(env):
    service.backfill_project("default")
    data = service.overview()
    assert data["project_total"] >= 1
    assert data["project_active"] >= 1
    assert data["task_in_progress"] == 1
    assert isinstance(data["token_24h"], int)
    assert data["projects"][0]["stats"]["agent_total"] == 7


# ---------------------------------------------------------------------------
# HTTP 兼容层（临时端口 + fixture 仓库）
# ---------------------------------------------------------------------------
@pytest.fixture()
def http_server(env, monkeypatch):
    slug = env.slug
    monkeypatch.setattr(dash, "REPO", env.repo)
    monkeypatch.setattr(dash, "PROJECTS_ROOT", env.projects_root)
    monkeypatch.setattr(dash, "STATUS_DIR", env.repo / "ai-team" / "status")
    monkeypatch.setattr(dash, "SLUG_PREFIXES", (slug,))
    monkeypatch.setattr(dash, "scan_usage", lambda *_a, **_k: {
        "source": "test", "available": True, "total_tokens": 0,
        "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "note": "",
    })
    server = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()


def _request(base: str, path: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(base + path, data=data, method=method,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_state_endpoint_isolation_and_legacy_fields(http_server, env, monkeypatch):
    monkeypatch.setattr(sources, "ccusage_session_costs", lambda: [{
        "project_path": env.slug, "session_id": "s1",
        "last_activity": _recent(1), "total_cost": 7.5,
        "total_tokens": 1000,
    }])
    _insert_token_event("default", "s1", "http-m1", 100)
    service.create_project({
        "code": "alpha", "name": "Alpha", "repo_root": str(env.repo),
        "session_slugs": [],
    })
    service.backfill_project("alpha")

    code, default_state = _request(http_server, "/api/state")
    assert code == 200
    assert default_state["project"]["id"] == "default"
    for key in ("generated_at", "owner", "roster", "statuses", "kanban",
                "tasks", "agents", "worktrees", "reports", "usage", "stats"):
        assert key in default_state
    assert default_state["project_usage"] == {
        "token_24h": 100, "cost_24h": 0.75, "source": "transcript/ccusage-session",
    }
    # 旧 usage 字段保持不变（由旧扫描逻辑产生）
    assert default_state["usage"]["source"] == "test"

    code, alpha_state = _request(http_server, "/api/state?project=alpha")
    assert code == 200
    assert alpha_state["project"] == {
        "id": "alpha", "code": "alpha", "name": "Alpha",
        "status": "active", "repo_root": str(env.repo.resolve()),
    }
    assert {t["id"] for t in alpha_state["tasks"]} == {"TEAM-001", "TEAM-002"}
    assert alpha_state["project_usage"]["cost_24h"] is None
    assert alpha_state["project_usage"]["source"] == "transcript/ccusage-session"

    code, missing = _request(http_server, "/api/state?project=nope")
    assert code == 404 and missing["error"] == "project_not_found"
    assert missing["detail"]["code"] == "project_not_found"


def test_state_roster_uses_platform_status_when_no_ai_team(env, http_server, tmp_path):
    """模式 B：仓库没有 ai-team/status 时，花名册读平台库，默认 idle。"""
    plain = tmp_path / "plain-repo"
    plain.mkdir()
    service.create_project({
        "code": "plain", "name": "Plain", "repo_root": str(plain),
        "session_slugs": [],
    })
    code, state = _request(http_server, "/api/state?project=plain")
    assert code == 200
    assert state["roster"]
    assert all(item["status"] == "idle" for item in state["roster"])


def test_projects_api_contract(http_server):
    code, data = _request(http_server, "/api/projects")
    assert code == 200
    assert data["default_project_id"] == "default"
    assert data["total"] >= 1
    item = next(p for p in data["items"] if p["id"] == "default")
    for key in ("agent_total", "agent_active", "task_todo", "task_in_progress",
                "task_blocked", "token_24h"):
        assert key in item["stats"]

    code, created = _request(http_server, "/api/projects", "POST", {
        "code": "beta", "name": "Beta 项目", "session_slugs": [],
    })
    assert code == 200 and created["project"]["id"] == "beta"

    code, dup = _request(http_server, "/api/projects", "POST", {
        "code": "beta", "name": "Beta Again", "session_slugs": [],
    })
    assert code == 409 and dup["error"] == "project_code_exists"

    code, bad = _request(http_server, "/api/projects", "POST", {"code": "Bad", "name": "Bad"})
    assert code == 422 and bad["error"] == "invalid_project_code"

    code, patched = _request(http_server, "/api/projects/beta", "PATCH", {"name": "Beta 2"})
    assert code == 200 and patched["project"]["name"] == "Beta 2"

    code, templates_data = _request(http_server, "/api/team-templates")
    assert code == 200 and templates_data["total"] == 2
    code, template = _request(http_server, "/api/team-templates/lean-1-3")
    assert code == 200 and len(template["template"]["roles"]) == 4


def _request_raw(base: str, path: str):
    try:
        with urlopen(base + path, timeout=10) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def test_file_access_is_confined_to_project_root(http_server, env):
    code, body = _request_raw(http_server, "/api/file?project=default&path=ai-team/TASK_BOARD.md")
    assert code == 200 and b"# Board" in body
    code, body = _request_raw(http_server, "/api/file?project=default&path=../../../../etc/passwd")
    assert code == 404 and json.loads(body)["error"] == "not_found"
    code, body = _request_raw(http_server, "/api/file?project=nope&path=ai-team/TASK_BOARD.md")
    assert code == 404 and json.loads(body)["error"] == "project_not_found"
    outside = env.tmp / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    code, body = _request_raw(http_server, f"/api/file?project=default&path={outside}")
    assert code == 404 and json.loads(body)["error"] == "not_found"


def test_backfill_endpoint_and_archive_guard(http_server, env):
    code, _ = _request(http_server, "/api/projects/default/backfill", "POST", {})
    assert code == 200
    service.patch_project("default", {"status": "archived"})
    code, data = _request(http_server, "/api/projects/default/backfill", "POST", {})
    assert code == 403 and data["error"] == "project_archived"
    code, _ = _request(http_server, "/api/projects/default/tasks?page=1&page_size=500")
    assert code == 200


# ---------------------------------------------------------------------------
# /api/state 会话扫描的项目隔离（回归：material 曾显示其他项目的 Owner 与 agent）
# ---------------------------------------------------------------------------
def _build_slug_transcript(projects_root: Path, slug: str, agent_id: str,
                           description: str) -> None:
    session = projects_root / slug / "sess-shared"
    _write(session.with_suffix(".jsonl"), json.dumps({
        "type": "assistant", "uuid": "m1", "timestamp": "2026-11-02T02:00:00.000Z",
        "sessionId": "sess-shared",
        "message": {"id": "m1", "model": "test-model",
                    "usage": {"input_tokens": 1, "output_tokens": 1}},
    }) + "\n")
    _write(session / "custom-title.json", json.dumps({"customTitle": f"{slug} Owner"}))
    _write(session / "subagents" / f"agent-{agent_id}.jsonl", json.dumps({
        "type": "assistant", "uuid": "s1", "timestamp": "2026-11-02T02:05:00.000Z",
        "sessionId": "sess-shared", "agentId": agent_id,
        "message": {"id": "m2", "model": "test-model",
                    "usage": {"input_tokens": 2, "output_tokens": 0}},
    }) + "\n")
    _write(session / "subagents" / f"agent-{agent_id}.meta.json", json.dumps({
        "agentType": "backend-engineer", "description": description, "finished": True,
    }, ensure_ascii=False))


def test_agent_scan_is_scoped_to_project_slugs(env, monkeypatch):
    repo_b = env.tmp / "repo-b"
    _build_repo(repo_b, title="B 任务")
    slug_b = _slug_for(repo_b)
    assert slug_b != env.slug
    _build_slug_transcript(env.projects_root, env.slug, "a", "A 项目后端")
    _build_slug_transcript(env.projects_root, slug_b, "b", "B 项目后端")

    monkeypatch.setattr(dash, "PROJECTS_ROOT", env.projects_root)
    monkeypatch.setattr(dash, "REPO", env.repo)
    monkeypatch.setattr(dash, "SLUG_PREFIXES", (env.slug, slug_b))

    state_a = dash.build_state(env.repo)
    state_b = dash.build_state(repo_b)

    assert {a["id"] for a in state_a["agents"]} == {"a"}
    assert {a["id"] for a in state_b["agents"]} == {"b"}
    assert state_a["owner"]["slug"] == env.slug
    assert state_b["owner"]["slug"] == slug_b
    assert state_a["owner"]["title"] == f"{env.slug} Owner"
    assert state_b["owner"]["title"] == f"{slug_b} Owner"

    # 未在配置里登记的项目退回「只认自身 slug」，不借用默认项目的旧 slug
    assert dash._prefixes_for(repo_b) == (slug_b,)


# ---------------------------------------------------------------------------
# 配置驱动登记：config/projects.json 是纳管清单的唯一事实源
# ---------------------------------------------------------------------------
def test_bootstrap_registers_configured_projects_once(env, monkeypatch, tmp_path):
    extra_repo = tmp_path / "repo-c"
    _build_repo(extra_repo, title="C 任务")
    cfg = tmp_path / "projects.json"
    cfg.write_text(json.dumps({
        "default_project_id": "default",
        "projects": [
            {"id": "proj-c", "code": "proj-c", "name": "Project C",
             "repo_root": str(extra_repo), "session_slugs": ["c-slug"]},
            {"id": "ghost", "code": "ghost", "name": "Ghost",
             "repo_root": str(tmp_path / "not-there")},
        ],
    }), encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_CONFIG", str(cfg))

    service.bootstrap(repo_root=env.repo)
    project = service.get_project("proj-c")
    assert project["name"] == "Project C"
    assert project["repo_root"] == str(extra_repo.resolve())
    assert project["session_slugs"] == ["c-slug"]
    assert len(project["agents"]) == 7

    # 仓库路径不存在的条目不登记，避免配置笔误落库
    with pytest.raises(service.PlatformError) as exc:
        service.get_project("ghost")
    assert exc.value.code == "project_not_found"

    # 幂等且不覆盖：库里改过的名字不会被配置重置
    with store.session() as conn:
        conn.execute("UPDATE agent_projects SET name = '改过的名字' WHERE id = 'proj-c'")
    service.bootstrap(repo_root=env.repo)
    assert service.get_project("proj-c")["name"] == "改过的名字"
