"""二期 M1-M4 服务层测试（B-02 迁移、B-03 生命周期、B-04 模板、B-05 任务、
B-06 交付、B-07 执行记录、B-08 文档/收藏、B-09 审计）。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import migrate, service, sources, store


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _slug_for(repo: Path) -> str:
    return repo.resolve().as_posix().replace("/", "-")


def _build_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _write(
        repo / "ai-team" / "TASK_BOARD.md",
        "# Board\n\n"
        "| Task ID | 标题 | Owner | Status | Priority | Depends On | MR | QA |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| TEAM-001 | 镜像任务 | owner | IN_PROGRESS | P1 | — | — | — |\n",
    )
    _write(repo / "ai-team" / "status" / "general-owner.json", json.dumps({
        "role": "general-owner", "state": "running", "task": "TEAM-001",
        "updated_at": "2026-11-02T10:00:00+08:00", "note": "执行中",
    }, ensure_ascii=False))
    _write(repo / "ai-team" / "reports" / "TEAM-001-qa.md", "# QA\n")


@pytest.fixture(autouse=True)
def _no_real_ccusage(monkeypatch):
    monkeypatch.setattr(sources, "ccusage_session_costs", list)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "platform.db"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    repo = tmp_path / "repo"
    _build_repo(repo)
    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(db_path))
    # 临时配置：测试库不读真实 config/projects.json，避免自动登记真实项目
    config_file = tmp_path / "projects.json"
    config_file.write_text(json.dumps({"projects": []}), encoding="utf-8")
    monkeypatch.setenv("DDP_AGENT_PLATFORM_CONFIG", str(config_file))
    monkeypatch.setenv("DDP_AGENT_PLATFORM_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("DDP_AGENT_PLATFORM_REPO", str(repo))
    service.bootstrap(repo_root=repo)
    return SimpleNamespace(
        tmp=tmp_path, db=db_path, projects_root=projects_root, repo=repo,
        slug=_slug_for(repo),
    )


# ---------------------------------------------------------------------------
# B-02 迁移
# ---------------------------------------------------------------------------
def test_migration_upgrades_v1_preserves_rows_and_backs_up(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE agent_projects (
          id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
          description TEXT, status TEXT NOT NULL DEFAULT 'active', repo_root TEXT NOT NULL,
          session_slugs TEXT NOT NULL DEFAULT '[]', team_template_key TEXT NOT NULL DEFAULT 'standard-1-6',
          team_template_version INTEGER NOT NULL DEFAULT 1, settings_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE tasks (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_key TEXT NOT NULL,
          title TEXT NOT NULL, status TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'P2',
          owner TEXT, qa TEXT, source TEXT NOT NULL DEFAULT 'board', updated_at TEXT NOT NULL,
          UNIQUE(project_id, task_key));
        CREATE TABLE deliveries (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, task_id TEXT, title TEXT NOT NULL,
          kind TEXT NOT NULL, path TEXT, sha TEXT, created_at TEXT NOT NULL);
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_version VALUES (1, '2026-11-01T00:00:00+00:00');
        INSERT INTO agent_projects
          (id, code, name, status, repo_root, created_at, updated_at)
          VALUES ('ddp', 'ddp', 'DDP', 'active', '/tmp/repo', 'x', 'x');
        INSERT INTO tasks (id, project_id, task_key, title, status, updated_at)
          VALUES ('t1', 'ddp', 'TEAM-001', '旧任务', 'todo', 'x');
        INSERT INTO deliveries (id, project_id, title, kind, path, created_at)
          VALUES ('d1', 'ddp', '旧交付', 'other', 'docs/a.md', 'x');
    """)
    conn.close()

    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(db))
    conn = store.connect(db)
    store.init_db(conn)
    version = conn.execute("SELECT version FROM schema_version").fetchall()
    project_cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_projects)")}
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("agent_projects", "tasks", "deliveries")
    }
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert [tuple(row) for row in version] == [(2,)]
    assert {"owner", "archived_at", "version"} <= project_cols
    assert counts == {"agent_projects": 1, "tasks": 1, "deliveries": 1}
    assert {"task_dependencies", "executions", "audit_logs"} <= tables
    backups = list(db.parent.glob("legacy.db.bak-*"))
    assert len(backups) == 1

    # 二次 init 幂等，不再新增备份
    conn = store.connect(db)
    store.init_db(conn)
    conn.close()
    assert len(list(db.parent.glob("legacy.db.bak-*"))) == 1


def test_migration_downgrade_reports_and_keeps_columns(tmp_path):
    db = tmp_path / "platform.db"
    conn = store.connect(db)
    store.init_db(conn)
    result = migrate.downgrade(conn, no_backup=True)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()
    assert result["version"] == 1
    assert "executions" not in tables
    assert {"description", "version"} <= cols
    assert version == 1


# ---------------------------------------------------------------------------
# B-03 项目生命周期
# ---------------------------------------------------------------------------
def test_project_version_archive_restore(env):
    created = service.create_project({"code": "alpha", "name": "Alpha", "session_slugs": []})
    assert created["version"] == 1
    updated = service.patch_project("alpha", {"name": "Alpha 2", "version": 1})
    assert updated["version"] == 2 and updated["name"] == "Alpha 2"
    with pytest.raises(service.PlatformError) as conflict:
        service.patch_project("alpha", {"name": "Alpha 3", "version": 1})
    assert conflict.value.code == "version_conflict" and conflict.value.status == 409

    archived = service.archive_project("alpha")
    assert archived["status"] == "archived" and archived["archived_at"]
    assert service.archive_project("alpha")["archived_at"] == archived["archived_at"]  # 幂等
    with pytest.raises(service.PlatformError) as blocked:
        service.create_task("alpha", {"title": "写入"})
    assert blocked.value.code == "project_archived" and blocked.value.status == 403

    restored = service.restore_project("alpha")
    assert restored["status"] == "active" and restored["archived_at"] is None
    assert service.create_task("alpha", {"title": "恢复后可写"})["task"]["title"] == "恢复后可写"


def test_idempotency_key_stores_first_result(env):
    fingerprint = "POST /api/projects fp"
    assert service.idempotency_lookup("k1", fingerprint) is None
    service.idempotency_store("k1", fingerprint, {"project": {"id": "gamma"}})
    assert service.idempotency_lookup("k1", fingerprint) == {"project": {"id": "gamma"}}
    with pytest.raises(service.PlatformError) as err:
        service.idempotency_lookup("k1", "other fp")
    assert err.value.code == "idempotency_conflict" and err.value.status == 409


# ---------------------------------------------------------------------------
# B-04 模板治理
# ---------------------------------------------------------------------------
def test_template_versions_clone_and_preview_apply(env):
    created = service.create_template({
        "template_key": "custom-1", "name": "自定义",
        "roles": [{"role_key": "owner", "display_name": "Owner"},
                  {"role_key": "dev", "display_name": "Dev", "parent_role_key": "owner"}],
    })["template"]
    assert created["version"] == 1 and created["is_system"] == 0
    second = service.create_template({
        "template_key": "custom-1", "name": "自定义 v2",
        "roles": [{"role_key": "owner", "display_name": "Owner"}],
    })["template"]
    assert second["version"] == 2
    assert service.get_team_template("custom-1", 1)["template"]["name"] == "自定义"
    latest = service.list_team_templates()
    assert next(t for t in latest["items"] if t["template_key"] == "custom-1")["version"] == 2

    cloned = service.clone_template("custom-1", {"key": "custom-copy"})["template"]
    assert cloned["template_key"] == "custom-copy" and cloned["version"] == 1

    service.create_project({"code": "alpha", "name": "Alpha", "session_slugs": [],
                            "team_template_key": "lean-1-3"})
    preview = service.preview_team("alpha", {"template_key": "custom-1", "version": 1})
    assert {item["role_key"] for item in preview["added"]} == {"owner", "dev"}
    assert preview["removed"]  # lean 模板里的实例不会被 apply 删除，但 preview 会提示

    applied = service.apply_team("alpha", {"template_key": "custom-1", "version": 1})
    assert applied["created"] == 2
    instance_keys = {agent["instance_key"] for agent in service.get_project("alpha")["agents"]}
    # 保留旧实例：lean-1-3 的 4 个角色仍在
    assert {"general-owner-1", "fullstack-engineer-1", "qa-engineer-1"} <= instance_keys
    assert {"owner-1", "dev-1"} <= instance_keys
    project = service.get_project("alpha")
    assert project["team_template_key"] == "custom-1"
    assert len(project["agents"]) == 6

    again = service.apply_team("alpha", {"template_key": "custom-1"})
    assert again["created"] == 0 and not again["added"]


# ---------------------------------------------------------------------------
# B-05 任务写闭环
# ---------------------------------------------------------------------------
def test_task_transitions_and_errors(env):
    service.create_project({"code": "alpha", "name": "Alpha", "session_slugs": []})
    task = service.create_task("alpha", {
        "task_key": "TEAM-100", "title": "实现", "acceptance_criteria": ["a", "b"],
    })["task"]
    assert task["status"] == "todo" and task["version"] == 1 and task["acceptance_criteria"] == ["a", "b"]

    moved = service.transition_task("alpha", task["id"], {
        "to_status": "in_progress", "expected_version": 1,
    })["task"]
    assert moved["status"] == "in_progress" and moved["version"] == 2 and moved["started_at"]

    with pytest.raises(service.PlatformError) as stale:
        service.transition_task("alpha", task["id"], {
            "to_status": "review", "expected_version": 1,
        })
    assert stale.value.code == "version_conflict"

    with pytest.raises(service.PlatformError) as invalid:
        service.transition_task("alpha", task["id"], {
            "to_status": "done", "expected_version": 2,
        })
    assert invalid.value.code == "invalid_transition" and invalid.value.status == 409

    with pytest.raises(service.PlatformError) as need_reason:
        service.transition_task("alpha", task["id"], {
            "to_status": "blocked", "expected_version": 2,
        })
    assert need_reason.value.code == "invalid_payload" and need_reason.value.status == 422

    blocked = service.transition_task("alpha", task["id"], {
        "to_status": "blocked", "expected_version": 2, "blocked_reason": "等接口",
    })["task"]
    assert blocked["blocked_reason"] == "等接口"
    review = service.transition_task("alpha", task["id"], {
        "to_status": "in_progress", "expected_version": 3,
    })["task"]
    review = service.transition_task("alpha", task["id"], {
        "to_status": "review", "expected_version": review["version"],
    })["task"]
    done = service.transition_task("alpha", task["id"], {
        "to_status": "done", "expected_version": review["version"],
    })["task"]
    assert done["status"] == "done" and done["completed_at"]

    events = service.list_audit_logs(project="alpha", action="task.transition")
    assert events["total"] == 5
    task_events = service.get_task("alpha", task["id"])["task"]["events"]
    assert any(event["event_type"] == "task_transition" for event in task_events)


def test_dependencies_cross_project_and_cycle(env):
    service.create_project({"code": "alpha", "name": "Alpha", "session_slugs": []})
    service.create_project({"code": "beta", "name": "Beta", "session_slugs": []})
    a1 = service.create_task("alpha", {"task_key": "A-1", "title": "A1"})["task"]
    a2 = service.create_task("alpha", {"task_key": "A-2", "title": "A2"})["task"]
    b1 = service.create_task("beta", {"task_key": "B-1", "title": "B1"})["task"]

    with pytest.raises(service.PlatformError) as cross:
        service.add_dependency("alpha", a1["id"], {"depends_on_task_id": b1["id"]})
    assert cross.value.code == "cross_project_reference" and cross.value.status == 422

    dep = service.add_dependency("alpha", a2["id"], {"depends_on_task_id": a1["id"]})
    assert dep["dependency_id"]
    with pytest.raises(service.PlatformError) as cycle:
        service.add_dependency("alpha", a1["id"], {"depends_on_task_id": a2["id"]})
    assert cycle.value.code == "dependency_cycle" and cycle.value.status == 422
    with pytest.raises(service.PlatformError) as self_dep:
        service.add_dependency("alpha", a1["id"], {"depends_on_task_id": a1["id"]})
    assert self_dep.value.code == "invalid_payload"

    detail = service.get_task("alpha", a1["id"])["task"]
    assert len(detail["dependents"]) == 1
    removed = service.delete_dependency("alpha", a2["id"], dep["dependency_id"])
    assert removed["ok"] is True
    assert service.get_task("alpha", a1["id"])["task"]["dependents"] == []


def test_mirror_task_is_readonly(env):
    service.backfill_project("ddp")
    mirror = service.list_tasks("ddp", page_size=100)["items"][0]
    assert mirror["readonly"] is True
    with pytest.raises(service.PlatformError) as err:
        service.patch_task("ddp", mirror["id"], {"title": "改不了"})
    assert err.value.code == "readonly_resource" and err.value.status == 409


# ---------------------------------------------------------------------------
# B-06 交付
# ---------------------------------------------------------------------------
def test_delivery_review_and_mirror_readonly(env):
    task = service.create_task("ddp", {"task_key": "TEAM-200", "title": "交付任务"})["task"]
    delivery = service.create_delivery("ddp", {
        "title": "实现完成", "kind": "code", "summary": "改了 3 个文件",
        "task_id": task["id"],
    })["delivery"]
    assert delivery["status"] == "submitted" and delivery["readonly"] is False

    with pytest.raises(service.PlatformError) as no_comment:
        service.review_delivery("ddp", delivery["id"], {"decision": "rejected"})
    assert no_comment.value.code == "invalid_payload" and no_comment.value.status == 422

    rejected = service.review_delivery("ddp", delivery["id"], {
        "decision": "rejected", "comment": "缺测试",
    })["delivery"]
    assert rejected["status"] == "rejected" and rejected["rejection_reason"] == "缺测试"

    accepted = service.review_delivery("ddp", delivery["id"], {"decision": "accepted"})["delivery"]
    assert accepted["status"] == "accepted" and accepted["reviewer"] == "local"
    assert service.list_audit_logs(project="ddp", action="delivery.review")["total"] == 2

    service.backfill_project("ddp")
    merged = service.list_deliveries("ddp", page_size=100)
    mirror = next(item for item in merged["items"] if item["readonly"])
    with pytest.raises(service.PlatformError) as readonly:
        service.review_delivery("ddp", mirror["id"], {"decision": "accepted"})
    assert readonly.value.code == "readonly_resource"


# ---------------------------------------------------------------------------
# B-07 执行记录
# ---------------------------------------------------------------------------
def test_executions_are_aggregated_idempotently(env):
    session_dir = env.projects_root / env.slug / "sess-x"
    _write(env.projects_root / env.slug / "sess-x.jsonl", "\n".join([
        json.dumps({"type": "user", "timestamp": "2026-11-02T02:00:00.000Z",
                    "message": {"role": "user", "content": "实现登录"}}),
        json.dumps({"type": "assistant", "timestamp": "2026-11-02T02:00:10.000Z",
                    "message": {"id": "m1", "model": "test-model",
                                "stop_reason": "end_turn", "usage": {
                                    "input_tokens": 10, "output_tokens": 5}}}),
    ]) + "\n")
    _write(session_dir / "subagents" / "agent-x.jsonl", json.dumps({
        "type": "assistant", "timestamp": "2026-11-02T02:05:00.000Z",
        "message": {"id": "m2", "model": "test-model", "stop_reason": "tool_use",
                    "usage": {"input_tokens": 100, "output_tokens": 0}},
    }) + "\n")
    _write(session_dir / "subagents" / "agent-x.meta.json", json.dumps({
        "agentType": "backend-engineer", "description": "实现接口", "finished": True,
    }, ensure_ascii=False))

    service.backfill_project("ddp")
    service.backfill_project("ddp")
    data = service.list_executions("ddp", page_size=100)
    assert data["total"] == 2
    main = next(item for item in data["items"] if item["total_tokens"] == 15)
    sub = next(item for item in data["items"] if item["total_tokens"] == 100)
    assert main["status"] == "succeeded" and main["duration_ms"] == 10000
    assert main["session_id"] == "sess-x"
    assert sub["status"] == "succeeded" and sub["summary"] == "实现接口"
    detail = service.get_execution("ddp", main["id"])["execution"]
    assert detail["model_id"] == "test-model"


# ---------------------------------------------------------------------------
# B-08 文档 / 收藏 / 最近访问
# ---------------------------------------------------------------------------
def test_documents_favorites_and_recent(env):
    doc = service.create_document("ddp", {
        "title": "架构", "document_type": "adr", "uri": "docs/reports/notes.md",
    })["document"]
    with pytest.raises(service.PlatformError) as escape:
        service.create_document("ddp", {"title": "越界", "uri": "../outside.md"})
    assert escape.value.code == "invalid_payload"
    with pytest.raises(service.PlatformError) as duplicate:
        service.create_document("ddp", {"title": "重复", "uri": "docs/reports/notes.md"})
    assert duplicate.value.code == "conflict"

    patched = service.patch_document("ddp", doc["id"], {"document_type": "api"})["document"]
    assert patched["document_type"] == "api"
    assert service.list_documents("ddp")["total"] == 1
    service.delete_document("ddp", doc["id"])
    assert service.list_documents("ddp")["total"] == 0

    service.create_project({"code": "alpha", "name": "Alpha", "session_slugs": []})
    assert service.set_favorite("ddp", True)["favorite"] is True
    favorite_ids = {item["id"] for item in service.list_projects(favorite="1")["items"]}
    assert favorite_ids == {"ddp"}
    assert service.get_project("ddp")["favorite"] is True
    service.set_favorite("ddp", False)
    assert service.get_project("ddp")["favorite"] is False

    service.record_visit("ddp")
    service.record_visit("alpha")
    recent = service.recent_projects()
    assert [item["id"] for item in recent["items"]][:2] == ["alpha", "ddp"]
    assert recent["total"] == 2


# ---------------------------------------------------------------------------
# B-09 审计
# ---------------------------------------------------------------------------
def test_audit_log_queries(env):
    service.create_project({"code": "alpha", "name": "Alpha", "session_slugs": []})
    service.patch_project("alpha", {"name": "Alpha 2"})
    service.archive_project("alpha")
    service.restore_project("alpha")
    logs = service.list_audit_logs(project="alpha")
    actions = [item["action"] for item in logs["items"]]
    assert {"project.create", "project.update", "project.archive", "project.restore"} <= set(actions)
    assert all(item["detail"] == {} or isinstance(item["detail"], dict) for item in logs["items"])
    filtered = service.list_audit_logs(action="project.archive")
    assert filtered["total"] == 1 and filtered["items"][0]["project_id"] == "alpha"
    paged = service.list_audit_logs(project="alpha", page=1, page_size=2)
    assert len(paged["items"]) == 2 and paged["total"] == 4


def test_task_and_delivery_pagination_slices(env):
    """任务/交付分页：total 为分页前总数，越界页返回空 items。"""
    project = service.create_project(
        {"code": "pages", "name": "Pages", "session_slugs": []}
    )
    pid = project["id"]
    for i in range(105):
        service.create_task(pid, {"task_key": f"P-{i:03d}", "title": f"任务 {i}"})
    first = service.list_tasks(pid, page=1, page_size=100)
    assert first["total"] == 105 and len(first["items"]) == 100
    assert first["page"] == 1 and first["page_size"] == 100
    second = service.list_tasks(pid, page=2, page_size=100)
    assert second["total"] == 105 and len(second["items"]) == 5
    empty = service.list_tasks(pid, page=9, page_size=100)
    assert empty["total"] == 105 and empty["items"] == []

    for i in range(103):
        service.create_delivery(pid, {"title": f"交付 {i}", "kind": "document"})
    d1 = service.list_deliveries(pid, page=1, page_size=50)
    assert d1["total"] == 103 and len(d1["items"]) == 50
    d3 = service.list_deliveries(pid, page=3, page_size=50)
    assert d3["total"] == 103 and len(d3["items"]) == 3
