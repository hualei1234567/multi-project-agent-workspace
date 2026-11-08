"""Workflow 域 V1：DAG 校验、发布门禁、绑定物化成第三期任务。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import store
from agent_platform.p4 import workflow


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    conn = store.connect()
    store.init_db(conn)
    for pid in ("p1", "p2"):
        conn.execute(
            "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
            " VALUES (?, ?, ?, '/tmp/repo', '2026-11-06T00:00:00+00:00',"
            " '2026-11-06T00:00:00+00:00')", (pid, pid, pid),
        )
    conn.commit()
    yield conn
    conn.close()


GOOD_DSL = {
    "nodes": [
        {"key": "design", "type": "task", "title": "设计"},
        {"key": "review", "type": "human_gate", "title": "人工评审", "depends_on": ["design"]},
        {"key": "build", "type": "task", "title": "实现", "depends_on": ["review"]},
    ]
}


def test_validate_ok():
    assert workflow.validate_dsl(GOOD_DSL) == []


def test_validate_detects_cycle():
    dsl = {"nodes": [
        {"key": "a", "title": "A", "depends_on": ["b"]},
        {"key": "b", "title": "B", "depends_on": ["a"]},
    ]}
    errors = workflow.validate_dsl(dsl)
    assert any("环" in e for e in errors)


def test_validate_detects_orphan():
    dsl = {"nodes": [
        {"key": "a", "title": "A", "depends_on": ["b"]},
        {"key": "b", "title": "B"},
        {"key": "c", "title": "C"},
    ]}
    errors = workflow.validate_dsl(dsl)
    assert any("孤立" in e for e in errors)


def test_validate_detects_unknown_dependency_and_duplicate():
    dsl = {"nodes": [
        {"key": "a", "title": "A", "depends_on": ["ghost"]},
        {"key": "a", "title": "A2"},
    ]}
    errors = workflow.validate_dsl(dsl)
    assert any("重复" in e for e in errors)
    assert any("不存在" in e for e in errors)


def test_validate_requires_title():
    errors = workflow.validate_dsl({"nodes": [{"key": "a"}]})
    assert any("title" in e for e in errors)


def test_reject_invalid_dsl_on_create_version(conn):
    definition = workflow.create_definition(conn, "p1", {"key": "deploy", "name": "部署"})
    with pytest.raises(workflow.WorkflowError) as exc:
        workflow.create_version(conn, definition["id"], {"dsl": {"nodes": []}})
    assert exc.value.code == "invalid_dsl"


def test_publish_requires_valid_and_test(conn):
    definition = workflow.create_definition(conn, "p1", {"key": "deploy", "name": "部署"})
    version = workflow.create_version(conn, definition["id"], {"dsl": GOOD_DSL})
    assert version["status"] == "draft"

    result = workflow.test_version(conn, version["id"])
    assert result["valid"] is True
    assert result["human_gates"] == ["review"]
    assert workflow.get_version(conn, version["id"])["status"] == "tested"

    workflow.publish_version(conn, version["id"], published_by="owner")
    assert workflow.get_version(conn, version["id"])["status"] == "published"


def test_bind_requires_published(conn):
    definition = workflow.create_definition(conn, "p1", {"key": "x", "name": "X"})
    version = workflow.create_version(conn, definition["id"], {"dsl": GOOD_DSL})
    with pytest.raises(workflow.WorkflowError) as exc:
        workflow.bind(conn, "p1", version["id"])
    assert exc.value.code == "invalid_state"


def test_bind_materializes_tasks_and_dependencies(conn):
    definition = workflow.create_definition(conn, "p1", {"key": "deploy", "name": "部署"})
    version = workflow.create_version(conn, definition["id"], {"dsl": GOOD_DSL})
    workflow.test_version(conn, version["id"])
    workflow.publish_version(conn, version["id"], published_by="owner")

    result = workflow.bind(conn, "p1", version["id"])
    assert len(result["tasks_created"]) == 3

    tasks = conn.execute("SELECT * FROM tasks WHERE project_id = 'p1'").fetchall()
    assert len(tasks) == 3
    assert {t["task_key"] for t in tasks} == {
        "wf:deploy:design", "wf:deploy:review", "wf:deploy:build",
    }
    assert all(t["status"] == "todo" and t["source"] == "workflow" for t in tasks)

    deps = conn.execute("SELECT * FROM task_dependencies").fetchall()
    assert len(deps) == 2


def test_rebind_is_idempotent(conn):
    definition = workflow.create_definition(conn, "p1", {"key": "deploy", "name": "部署"})
    version = workflow.create_version(conn, definition["id"], {"dsl": GOOD_DSL})
    workflow.publish_version(conn, version["id"], published_by="owner")
    workflow.bind(conn, "p1", version["id"])
    second = workflow.bind(conn, "p1", version["id"])
    assert second["tasks_created"] == []
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0] == 2


def test_bind_other_project_rejected(conn):
    definition = workflow.create_definition(conn, "p1", {"key": "deploy", "name": "部署"})
    version = workflow.create_version(conn, definition["id"], {"dsl": GOOD_DSL})
    workflow.publish_version(conn, version["id"], published_by="owner")
    with pytest.raises(workflow.WorkflowError) as exc:
        workflow.bind(conn, "p2", version["id"])
    assert exc.value.code == "forbidden"
