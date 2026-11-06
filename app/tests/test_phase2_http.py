"""二期 HTTP 契约测试：错误体、归档只读、状态机、依赖环、交付验收、
收藏往返、审计查询与幂等头。"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agents_dashboard as dash
from agent_platform import service, sources


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _write(
        repo / "ai-team" / "TASK_BOARD.md",
        "# Board\n\n"
        "| Task ID | 标题 | Owner | Status | Priority | Depends On | MR | QA |\n"
        "|---|---|---|---|---|---|---|---|\n"
        "| TEAM-001 | 镜像任务 | owner | IN_PROGRESS | P1 | — | — | — |\n",
    )
    _write(repo / "ai-team" / "status" / "general-owner.json",
           json.dumps({"role": "general-owner", "state": "running"}, ensure_ascii=False))
    _write(repo / "docs" / "reports" / "notes.md", "# notes\n")


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
    return SimpleNamespace(tmp=tmp_path, db=db_path, projects_root=projects_root, repo=repo,
                           slug=repo.resolve().as_posix().replace("/", "-"))


@pytest.fixture()
def http_server(env, monkeypatch):
    monkeypatch.setattr(dash, "REPO", env.repo)
    monkeypatch.setattr(dash, "PROJECTS_ROOT", env.projects_root)
    monkeypatch.setattr(dash, "STATUS_DIR", env.repo / "ai-team" / "status")
    monkeypatch.setattr(dash, "SLUG_PREFIXES", (env.slug,))
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


def _request(base: str, path: str, method: str = "GET", body: dict | None = None,
             headers: dict[str, str] | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    req = Request(base + path, data=data, method=method, headers=merged)
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_error_body_has_detail_code(http_server):
    code, data = _request(http_server, "/api/projects/nope")
    assert code == 404
    assert data["detail"] == {"code": "project_not_found", "message": "project_not_found"}
    assert data["error"] == "project_not_found"  # 兼容一期字段


def test_project_lifecycle_over_http(http_server):
    code, created = _request(http_server, "/api/projects", "POST",
                             {"code": "alpha", "name": "Alpha", "session_slugs": []})
    assert code == 200 and created["project"]["version"] == 1
    assert created["project"]["favorite"] is False

    code, patched = _request(http_server, "/api/projects/alpha", "PATCH",
                             {"name": "Alpha 2", "version": 1})
    assert code == 200 and patched["project"]["version"] == 2
    code, conflict = _request(http_server, "/api/projects/alpha", "PATCH",
                              {"name": "Alpha 3", "version": 1})
    assert code == 409 and conflict["detail"]["code"] == "version_conflict"

    code, archived = _request(http_server, "/api/projects/alpha/archive", "POST", {})
    assert code == 200 and archived["project"]["status"] == "archived"
    code, blocked = _request(http_server, "/api/projects/alpha/tasks", "POST",
                             {"title": "归档后创建"})
    assert code == 403 and blocked["detail"]["code"] == "project_archived"

    code, restored = _request(http_server, "/api/projects/alpha/restore", "POST", {})
    assert code == 200 and restored["project"]["status"] == "active"
    code, task = _request(http_server, "/api/projects/alpha/tasks", "POST", {"title": "恢复后创建"})
    assert code == 200 and task["task"]["status"] == "todo"


def test_idempotency_key_over_http(http_server):
    payload = {"code": "idem", "name": "Idem", "session_slugs": []}
    headers = {"Idempotency-Key": "create-idem-1"}
    code, first = _request(http_server, "/api/projects", "POST", payload, headers)
    assert code == 200
    code, second = _request(http_server, "/api/projects", "POST", payload, headers)
    assert code == 200 and second == first
    # 同键不同请求体 → 冲突
    code, conflict = _request(http_server, "/api/projects", "POST",
                              {"code": "idem2", "name": "Idem2", "session_slugs": []}, headers)
    assert code == 409 and conflict["detail"]["code"] == "idempotency_conflict"


def test_task_transition_and_dependencies_over_http(http_server):
    code, left = _request(http_server, "/api/projects/default/tasks", "POST",
                          {"task_key": "A-1", "title": "A1"})
    assert code == 200
    code, right = _request(http_server, "/api/projects/default/tasks", "POST",
                           {"task_key": "A-2", "title": "A2"})
    assert code == 200
    a1, a2 = left["task"], right["task"]

    code, moved = _request(http_server, f"/api/projects/default/tasks/{a1['id']}/transition", "POST",
                           {"to_status": "in_progress", "expected_version": 1})
    assert code == 200 and moved["task"]["status"] == "in_progress"

    code, invalid = _request(http_server, f"/api/projects/default/tasks/{a1['id']}/transition", "POST",
                             {"to_status": "done", "expected_version": moved["task"]["version"]})
    assert code == 409 and invalid["detail"]["code"] == "invalid_transition"

    code, dep = _request(http_server, f"/api/projects/default/tasks/{a2['id']}/dependencies", "POST",
                         {"depends_on_task_id": a1["id"]})
    assert code == 200 and dep["dependency_id"]
    code, cycle = _request(http_server, f"/api/projects/default/tasks/{a1['id']}/dependencies", "POST",
                           {"depends_on_task_id": a2["id"]})
    assert code == 422 and cycle["detail"]["code"] == "dependency_cycle"

    code, deleted = _request(
        http_server,
        f"/api/projects/default/tasks/{a2['id']}/dependencies/{dep['dependency_id']}",
        "DELETE",
    )
    assert code == 200 and deleted["ok"] is True


def test_delivery_favorites_audit_over_http(http_server):
    code, delivery = _request(http_server, "/api/projects/default/deliveries", "POST",
                              {"title": "交付", "kind": "code", "summary": "done"})
    assert code == 200 and delivery["delivery"]["status"] == "submitted"
    delivery_id = delivery["delivery"]["id"]

    code, rejected = _request(http_server, f"/api/projects/default/deliveries/{delivery_id}/review",
                              "POST", {"decision": "rejected"})
    assert code == 422 and rejected["detail"]["code"] == "invalid_payload"
    code, reviewed = _request(http_server, f"/api/projects/default/deliveries/{delivery_id}/review",
                              "POST", {"decision": "rejected", "comment": "缺测试"})
    assert code == 200 and reviewed["delivery"]["status"] == "rejected"

    code, fav = _request(http_server, "/api/projects/default/favorite", "PUT")
    assert code == 200 and fav["favorite"] is True
    code, listed = _request(http_server, "/api/projects?favorite=1")
    assert code == 200 and {item["id"] for item in listed["items"]} == {"default"}
    code, unfav = _request(http_server, "/api/projects/default/favorite", "DELETE")
    assert code == 200 and unfav["favorite"] is False

    code, visit = _request(http_server, "/api/projects/default/visit", "POST", {})
    assert code == 200 and visit["project_id"] == "default"
    code, recent = _request(http_server, "/api/me/recent-projects")
    assert code == 200 and [item["id"] for item in recent["items"]] == ["default"]

    code, logs = _request(http_server, "/api/audit-logs?project=default&action=delivery.review")
    assert code == 200 and logs["total"] == 1
    assert logs["items"][0]["detail"]["decision"] == "rejected"


def test_team_template_preview_and_apply_over_http(http_server):
    code, preview = _request(http_server, "/api/projects/default/team/preview", "POST",
                             {"template_key": "lean-1-3"})
    assert code == 200
    assert {item["role_key"] for item in preview["added"]} == {
        "fullstack-engineer", "qa-engineer", "product-architecture",
    }
    code, applied = _request(http_server, "/api/projects/default/team/apply", "POST",
                             {"template_key": "lean-1-3"})
    assert code == 200 and applied["created"] == 3
    agents = service.get_project("default")["agents"]
    assert len(agents) == 10  # 标准 7 个旧实例全部保留 + 3 个新增
    code, logs = _request(http_server, "/api/audit-logs?project=default&action=template.apply")
    assert code == 200 and logs["total"] == 1
