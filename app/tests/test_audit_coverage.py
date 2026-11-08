"""统一审计覆盖（三期验收补齐 G-1）。

调度、执行器、运行时、共享资源是平台最有权限的四类操作，
必须都写进 audit_logs，否则 /api/audit-logs 无法回溯「谁做了什么」。
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

from agent_platform import resources, runtime, scheduler, service, store


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
    service.bootstrap(repo_root=repo)
    return SimpleNamespace(db=db, repo=repo)


def _actions(db: Path) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute("SELECT action FROM audit_logs ORDER BY created_at")]
    finally:
        conn.close()


def _agent(project="default", role="frontend-engineer"):
    with store.session() as conn:
        return conn.execute(
            "SELECT id FROM project_agents WHERE project_id=? AND role_key=?", (project, role)
        ).fetchone()["id"]


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------
def test_plan_creation_is_audited(env):
    service.create_task("default", {
        "title": "审计用例", "task_key": "AU-001", "assignee_agent_id": _agent(),
    })
    scheduler.create_plan("default")
    assert "dispatch.plan.create" in _actions(env.db)


def test_plan_audit_records_counts(env):
    service.create_task("default", {
        "title": "审计用例2", "task_key": "AU-002", "assignee_agent_id": _agent(),
    })
    plan = scheduler.create_plan("default")
    with store.session() as conn:
        row = conn.execute(
            "SELECT detail_json FROM audit_logs WHERE action='dispatch.plan.create'"
        ).fetchone()
    detail = json.loads(row["detail_json"])
    assert detail["items"] == len(plan["items"])
    assert "strategy" in detail


# ---------------------------------------------------------------------------
# 运行时
# ---------------------------------------------------------------------------
def test_runtime_action_is_audited(env):
    runtime.run_action("docker_ps_material", preflight_token=None, confirm=False)
    assert "runtime.action.run" in _actions(env.db)


def test_runtime_audit_captures_result(env):
    result = runtime.run_action("docker_ps_material", preflight_token=None, confirm=False)
    with store.session() as conn:
        row = conn.execute(
            "SELECT result, detail_json FROM audit_logs WHERE action='runtime.action.run'"
        ).fetchone()
    detail = json.loads(row["detail_json"])
    assert detail["status"] == result["status"]
    assert row["result"] in {"ok", "failed"}


# ---------------------------------------------------------------------------
# 共享资源
# ---------------------------------------------------------------------------
def test_resource_claim_and_release_audited(env):
    res = resources.create_resource({"name": "审计资源", "kind": "host"})
    claim = resources.claim_resource(res["id"], {"project_id": "default"})
    resources.release_claim(res["id"], claim["claim_id"])
    actions = _actions(env.db)
    assert "resource.claim" in actions
    assert "resource.release" in actions


def test_resource_audit_has_project_scope(env):
    res = resources.create_resource({"name": "审计资源2", "kind": "host"})
    resources.claim_resource(res["id"], {"project_id": "default"})
    with store.session() as conn:
        row = conn.execute(
            "SELECT project_id, detail_json FROM audit_logs WHERE action='resource.claim'"
        ).fetchone()
    assert row["project_id"] == "default"
    assert json.loads(row["detail_json"])["amount"] == 1


# ---------------------------------------------------------------------------
# 审计本身不能阻断业务
# ---------------------------------------------------------------------------
def test_audit_failure_does_not_break_business(env, monkeypatch):
    """审计写失败时，业务仍须成功（审计是尽力而为）。"""
    def _boom(*_a, **_k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(service, "audit", _boom)
    res = resources.create_resource({"name": "容错资源", "kind": "host"})
    claim = resources.claim_resource(res["id"], {"project_id": "default"})
    assert claim["claim_id"]
