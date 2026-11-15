"""Global Task 的 HTTP 契约（P3-M5）。

只测真实会出错的路径，不重复模块层已经覆盖的规则：

* 创建/列表/详情/概览四条读接口真的接上了（不是 404「接口不存在」）；
* **挂接前先过目标项目写权限**——无权者拿不到「任务属于哪个项目」这种探测信息；
* 归属不符返回 422、重复挂接 409、解挂未知 link 404。
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agents_dashboard as dash
from agent_platform import service, sources, store
from conftest import local_urlopen

NOW = "2026-11-15T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _no_real_ccusage(monkeypatch):
    monkeypatch.setattr(sources, "ccusage_session_costs", list)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "platform.db"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    repo = tmp_path / "repo"
    (repo / "ai-team").mkdir(parents=True)
    (repo / "ai-team" / "TASK_BOARD.md").write_text("# Board\n", encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(db_path))
    config_file = tmp_path / "projects.json"
    config_file.write_text(json.dumps({"projects": []}), encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_CONFIG", str(config_file))
    monkeypatch.setenv("AGWS_PLATFORM_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("AGWS_PLATFORM_REPO", str(repo))
    service.bootstrap(repo_root=repo)

    with store.session() as conn:
        for project_id in ("p1", "p2"):
            conn.execute(
                "INSERT INTO agent_projects (id, code, name, repo_root, created_at,"
                " updated_at) VALUES (?, ?, ?, '/tmp/r', ?, ?)",
                (project_id, project_id, project_id.upper(), NOW, NOW),
            )
        conn.execute(
            "INSERT INTO tasks (id, project_id, task_key, title, status, priority,"
            " source, updated_at) VALUES ('t-p1', 'p1', 'T-1', 'p1 任务', 'todo',"
            " 'P2', 'platform', ?)", (NOW,))
        conn.execute(
            "INSERT INTO tasks (id, project_id, task_key, title, status, priority,"
            " source, updated_at) VALUES ('t-p2', 'p2', 'T-2', 'p2 任务', 'todo',"
            " 'P2', 'platform', ?)", (NOW,))
    return SimpleNamespace(db=db_path, repo=repo, projects_root=projects_root)


@pytest.fixture()
def server(env, monkeypatch):
    monkeypatch.setattr(dash, "REPO", env.repo)
    monkeypatch.setattr(dash, "PROJECTS_ROOT", env.projects_root)
    monkeypatch.setattr(dash, "STATUS_DIR", env.repo / "ai-team" / "status")
    monkeypatch.setattr(dash, "SLUG_PREFIXES", ())
    monkeypatch.setattr(dash, "scan_usage", lambda *_a, **_k: {
        "source": "test", "available": True, "total_tokens": 0,
        "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "note": "",
    })
    httpd = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()


def _request(base: str, path: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(base + path, data=data, method=method,
                  headers={"Content-Type": "application/json"})
    try:
        with local_urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _create(base: str, key: str = "GT-1") -> dict:
    code, data = _request(base, "/api/agent-platform/global-tasks", "POST",
                          {"global_key": key, "title": "统一任务", "priority": "P1"})
    assert code == 200, data
    return data["data"]


# ---------------------------------------------------------------------------
def test_create_list_detail_overview(server):
    item = _create(server)
    gid = item["id"]

    code, listed = _request(server, "/api/agent-platform/global-tasks")
    assert code == 200
    assert [row["global_key"] for row in listed["data"]["items"]] == ["GT-1"]

    code, detail = _request(server, f"/api/agent-platform/global-tasks/{gid}")
    assert code == 200
    assert detail["data"]["links"] == []
    assert detail["data"]["no_links"] is True

    code, rollup = _request(server, f"/api/agent-platform/global-tasks/{gid}/rollup")
    assert code == 200
    assert rollup["data"]["status"] == "open"
    assert rollup["data"]["no_links"] is True

    code, overview = _request(server, "/api/agent-platform/global-overview")
    assert code == 200
    assert overview["data"]["totals"]["global_tasks"] == 1
    assert overview["data"]["totals"]["global_tasks_without_links"] == 1


def test_create_validation_and_conflict(server):
    code, data = _request(server, "/api/agent-platform/global-tasks", "POST",
                          {"global_key": "", "title": "x"})
    assert code == 422
    assert data["error"] == "invalid_payload"

    _create(server, "GT-DUP")
    code, data = _request(server, "/api/agent-platform/global-tasks", "POST",
                          {"global_key": "GT-DUP", "title": "重复"})
    assert code == 409
    assert data["error"] == "conflict"


def test_detail_missing_is_404_not_unrouted(server):
    code, data = _request(server, "/api/agent-platform/global-tasks/gt_missing")
    assert code == 404
    # 必须是「Global Task 不存在」，不是「接口不存在」——后者代表路由没接上
    assert data["error"] == "not_found"
    assert "接口不存在" not in data["message"]


def test_link_then_rollup_then_unlink(server):
    gid = _create(server)["id"]
    code, data = _request(server, f"/api/agent-platform/global-tasks/{gid}/links", "POST",
                          {"project_id": "p1", "task_id": "t-p1"})
    assert code == 200, data
    assert data["data"]["link_count"] == 1
    link_id = data["data"]["links"][0]["link_id"]

    code, rollup = _request(server, f"/api/agent-platform/global-tasks/{gid}/rollup")
    assert rollup["data"]["status"] == "open"

    code, data = _request(
        server, f"/api/agent-platform/global-tasks/{gid}/links/{link_id}", "DELETE")
    assert code == 200, data
    assert data["data"]["task_id"] == "t-p1"

    code, rollup = _request(server, f"/api/agent-platform/global-tasks/{gid}/rollup")
    assert rollup["data"]["no_links"] is True


def test_link_rejects_task_from_other_project(server):
    gid = _create(server)["id"]
    code, data = _request(server, f"/api/agent-platform/global-tasks/{gid}/links", "POST",
                          {"project_id": "p2", "task_id": "t-p1"})
    assert code == 422
    assert data["error"] == "task_project_mismatch"


def test_link_unknown_project_and_task(server):
    gid = _create(server)["id"]
    code, data = _request(server, f"/api/agent-platform/global-tasks/{gid}/links", "POST",
                          {"project_id": "nope", "task_id": "t-p1"})
    assert code == 404
    assert data["error"] == "project_not_found"

    code, data = _request(server, f"/api/agent-platform/global-tasks/{gid}/links", "POST",
                          {"project_id": "p1", "task_id": "nope"})
    assert code == 404
    assert data["error"] == "task_not_found"


def test_link_requires_project_id(server):
    gid = _create(server)["id"]
    code, data = _request(server, f"/api/agent-platform/global-tasks/{gid}/links", "POST",
                          {"task_id": "t-p1"})
    assert code == 422
    assert data["error"] == "invalid_payload"


def test_duplicate_link_is_409(server):
    gid = _create(server)["id"]
    body = {"project_id": "p1", "task_id": "t-p1"}
    _request(server, f"/api/agent-platform/global-tasks/{gid}/links", "POST", body)
    code, data = _request(server, f"/api/agent-platform/global-tasks/{gid}/links", "POST", body)
    assert code == 409
    assert data["error"] == "conflict"


def test_unlink_unknown_link_is_404(server):
    gid = _create(server)["id"]
    code, data = _request(
        server, f"/api/agent-platform/global-tasks/{gid}/links/gtl_nope", "DELETE")
    assert code == 404
    assert data["error"] == "not_found"
    assert "接口不存在" not in data["message"]


def test_link_under_wrong_global_task_is_404(server):
    """link 不属于路径里的 Global Task 时不能删——防止跨任务误删。"""
    g1 = _create(server, "GT-A")["id"]
    g2 = _create(server, "GT-B")["id"]
    code, data = _request(server, f"/api/agent-platform/global-tasks/{g1}/links", "POST",
                          {"project_id": "p1", "task_id": "t-p1"})
    link_id = data["data"]["links"][0]["link_id"]
    code, data = _request(
        server, f"/api/agent-platform/global-tasks/{g2}/links/{link_id}", "DELETE")
    assert code == 404

    code, detail = _request(server, f"/api/agent-platform/global-tasks/{g1}")
    assert detail["data"]["link_count"] == 1


def test_list_filters(server):
    g1 = _create(server, "GT-A")["id"]
    _create(server, "GT-B")
    _request(server, f"/api/agent-platform/global-tasks/{g1}/links", "POST",
             {"project_id": "p2", "task_id": "t-p2"})

    code, data = _request(server, "/api/agent-platform/global-tasks?project=p2")
    assert [row["global_key"] for row in data["data"]["items"]] == ["GT-A"]

    code, data = _request(server, "/api/agent-platform/global-tasks?status=finished")
    assert code == 422
