"""B-14 三项目隔离回归：DDP / PROJECT_B / PROJECT_C 之间的读写边界。

覆盖：跨项目 task/delivery/execution、归档只读、依赖跨项目与成环、
交付验收校验、收藏隔离、最近访问顺序、``/api/state`` 项目隔离、审计过滤。

每个用例使用独立临时库（``AGWS_PLATFORM_DB``）与临时仓库，
保证可重复且不污染本机平台库。
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agents_dashboard as dash
from agent_platform import service, sources, store

DDP = "default"
PROJECT_B = "proj-b"
PROJECT_C = "proj-c"


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
    """每个用例一份独立临时库 + 临时项目根 + 临时仓库。"""
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
        slug=repo.resolve().as_posix().replace("/", "-"),
    )


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


def _create_project(base: str, code: str, name: str | None = None) -> dict:
    status, data = _request(base, "/api/projects", "POST",
                            {"code": code, "name": name or code.upper(),
                             "session_slugs": []})
    assert status == 200, data
    return data["project"]


def _create_task(base: str, project: str, task_key: str, title: str | None = None) -> dict:
    status, data = _request(base, f"/api/projects/{project}/tasks", "POST",
                            {"task_key": task_key, "title": title or task_key})
    assert status == 200, data
    return data["task"]


def _create_delivery(base: str, project: str, title: str,
                     task_id: str | None = None) -> dict:
    payload: dict = {"title": title, "kind": "code"}
    if task_id:
        payload["task_id"] = task_id
    status, data = _request(base, f"/api/projects/{project}/deliveries", "POST", payload)
    assert status == 200, data
    return data["delivery"]


# ---------------------------------------------------------------------------
# a. 跨项目 task / delivery / execution
# ---------------------------------------------------------------------------
def test_cross_project_task_and_delivery_are_isolated(http_server):
    _create_project(http_server, PROJECT_B, "Project B")
    a_task = _create_task(http_server, DDP, "A-1", "A 项目任务")

    # B 读 A 的任务 → 404
    code, data = _request(http_server, f"/api/projects/{PROJECT_B}/tasks/{a_task['id']}")
    assert code == 404 and data["detail"]["code"] == "not_found"

    # B 改 / 流转 A 的任务 → 404
    code, data = _request(http_server, f"/api/projects/{PROJECT_B}/tasks/{a_task['id']}",
                          "PATCH", {"title": "跨项目篡改", "version": 1})
    assert code == 404 and data["detail"]["code"] == "not_found"
    code, data = _request(
        http_server, f"/api/projects/{PROJECT_B}/tasks/{a_task['id']}/transition", "POST",
        {"to_status": "in_progress", "expected_version": 1},
    )
    assert code == 404 and data["detail"]["code"] == "not_found"

    # B 的任务列表不包含 A 的任务
    code, listed = _request(http_server, f"/api/projects/{PROJECT_B}/tasks")
    assert code == 200 and listed["total"] == 0

    # B 建交付引用 A 的任务 → 422 cross_project_reference
    code, data = _request(http_server, f"/api/projects/{PROJECT_B}/deliveries", "POST",
                          {"title": "越界交付", "kind": "code", "task_id": a_task["id"]})
    assert code == 422 and data["detail"]["code"] == "cross_project_reference"

    # B 审 A 的交付 → 404；B 的交付列表不含 A 的交付
    a_delivery = _create_delivery(http_server, DDP, "A 项目交付")
    code, data = _request(
        http_server, f"/api/projects/{PROJECT_B}/deliveries/{a_delivery['id']}/review",
        "POST", {"decision": "accepted"},
    )
    assert code == 404 and data["detail"]["code"] == "not_found"
    code, b_deliveries = _request(http_server, f"/api/projects/{PROJECT_B}/deliveries")
    assert code == 200
    assert a_delivery["id"] not in {item["id"] for item in b_deliveries["items"]}


def test_cross_project_execution_is_isolated(http_server, env):
    _create_project(http_server, PROJECT_B, "Project B")
    session_dir = env.projects_root / env.slug / "sess-b14"
    _write(session_dir / "subagents" / "agent-b14.jsonl", json.dumps({
        "type": "assistant", "timestamp": "2026-11-03T02:00:00.000Z",
        "message": {"id": "m-b14", "model": "test-model", "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5}},
    }) + "\n")
    _write(session_dir / "subagents" / "agent-b14.meta.json", json.dumps({
        "agentType": "backend-engineer", "description": "隔离用例", "finished": True,
    }, ensure_ascii=False))

    code, backfilled = _request(http_server, f"/api/projects/{DDP}/backfill", "POST", {})
    assert code == 200 and backfilled["counts"]["executions"] == 1

    code, executions = _request(http_server, f"/api/projects/{DDP}/executions")
    assert code == 200 and executions["total"] == 1
    exec_id = executions["items"][0]["id"]

    # B 读 A 的执行记录 → 404；B 列表为空
    code, data = _request(http_server, f"/api/projects/{PROJECT_B}/executions/{exec_id}")
    assert code == 404 and data["detail"]["code"] == "not_found"
    code, b_executions = _request(http_server, f"/api/projects/{PROJECT_B}/executions")
    assert code == 200 and b_executions["total"] == 0


# ---------------------------------------------------------------------------
# b. 归档项目只读
# ---------------------------------------------------------------------------
def test_archived_project_is_read_only(http_server):
    _create_project(http_server, PROJECT_C, "Project C")
    task = _create_task(http_server, PROJECT_C, "C-1", "归档前任务")

    code, archived = _request(http_server, f"/api/projects/{PROJECT_C}/archive", "POST", {})
    assert code == 200 and archived["project"]["status"] == "archived"

    # 读仍然 200
    code, project = _request(http_server, f"/api/projects/{PROJECT_C}")
    assert code == 200 and project["project"]["status"] == "archived"
    code, tasks = _request(http_server, f"/api/projects/{PROJECT_C}/tasks")
    assert code == 200 and {item["task_key"] for item in tasks["items"]} == {"C-1"}
    code, detail = _request(http_server, f"/api/projects/{PROJECT_C}/tasks/{task['id']}")
    assert code == 200 and detail["task"]["task_key"] == "C-1"

    # 写操作 403 project_archived
    code, data = _request(http_server, f"/api/projects/{PROJECT_C}/tasks", "POST",
                          {"task_key": "C-2", "title": "归档后创建"})
    assert code == 403 and data["detail"]["code"] == "project_archived"
    code, data = _request(http_server, f"/api/projects/{PROJECT_C}/deliveries", "POST",
                          {"title": "归档后交付"})
    assert code == 403 and data["detail"]["code"] == "project_archived"
    code, data = _request(http_server, f"/api/projects/{PROJECT_C}/tasks/{task['id']}",
                          "PATCH", {"title": "归档后修改", "version": 1})
    assert code == 403 and data["detail"]["code"] == "project_archived"
    code, data = _request(
        http_server, f"/api/projects/{PROJECT_C}/tasks/{task['id']}/transition", "POST",
        {"to_status": "in_progress", "expected_version": 1},
    )
    assert code == 403 and data["detail"]["code"] == "project_archived"

    # 恢复后可写
    code, restored = _request(http_server, f"/api/projects/{PROJECT_C}/restore", "POST", {})
    assert code == 200 and restored["project"]["status"] == "active"
    _create_task(http_server, PROJECT_C, "C-2", "恢复后创建")


# ---------------------------------------------------------------------------
# c. 依赖跨项目 / 成环
# ---------------------------------------------------------------------------
def test_dependency_cross_project_and_cycle(http_server):
    _create_project(http_server, PROJECT_B, "Project B")
    a1 = _create_task(http_server, DDP, "A-1")
    a2 = _create_task(http_server, DDP, "A-2")
    b1 = _create_task(http_server, PROJECT_B, "B-1")

    code, data = _request(
        http_server, f"/api/projects/{DDP}/tasks/{a2['id']}/dependencies", "POST",
        {"depends_on_task_id": b1["id"]},
    )
    assert code == 422 and data["detail"]["code"] == "cross_project_reference"

    code, dep = _request(
        http_server, f"/api/projects/{DDP}/tasks/{a2['id']}/dependencies", "POST",
        {"depends_on_task_id": a1["id"]},
    )
    assert code == 200 and dep["dependency_id"]

    code, data = _request(
        http_server, f"/api/projects/{DDP}/tasks/{a1['id']}/dependencies", "POST",
        {"depends_on_task_id": a2["id"]},
    )
    assert code == 422 and data["detail"]["code"] == "dependency_cycle"

    code, data = _request(
        http_server, f"/api/projects/{DDP}/tasks/{a1['id']}/dependencies", "POST",
        {"depends_on_task_id": a1["id"]},
    )
    assert code == 422 and data["detail"]["code"] == "invalid_payload"

    # 跨项目依赖没有落库：A-2 只有 A-1 一个依赖
    code, detail = _request(http_server, f"/api/projects/{DDP}/tasks/{a2['id']}")
    assert code == 200
    assert [item["depends_on_task_id"] for item in detail["task"]["dependencies"]] == [a1["id"]]


# ---------------------------------------------------------------------------
# d. 交付验收
# ---------------------------------------------------------------------------
def test_delivery_review_isolation_and_validation(http_server):
    _create_project(http_server, PROJECT_B, "Project B")
    a_delivery = _create_delivery(http_server, DDP, "A 交付")
    b_delivery = _create_delivery(http_server, PROJECT_B, "B 交付")

    # rejected 必须带 comment
    code, data = _request(
        http_server, f"/api/projects/{PROJECT_B}/deliveries/{b_delivery['id']}/review",
        "POST", {"decision": "rejected"},
    )
    assert code == 422 and data["detail"]["code"] == "invalid_payload"

    # 用 B 的项目路径审 A 的交付 → 404
    code, data = _request(
        http_server, f"/api/projects/{PROJECT_B}/deliveries/{a_delivery['id']}/review",
        "POST", {"decision": "rejected", "comment": "跨项目"},
    )
    assert code == 404 and data["detail"]["code"] == "not_found"

    # 本项目的交付正常审核
    code, reviewed = _request(
        http_server, f"/api/projects/{PROJECT_B}/deliveries/{b_delivery['id']}/review",
        "POST", {"decision": "accepted", "comment": "通过"},
    )
    assert code == 200 and reviewed["delivery"]["status"] == "accepted"
    assert reviewed["delivery"]["reviewer"] == "local"

    # A 的交付未被跨项目审核影响
    code, a_deliveries = _request(http_server, f"/api/projects/{DDP}/deliveries")
    assert code == 200
    a_item = next(item for item in a_deliveries["items"] if item["id"] == a_delivery["id"])
    assert a_item["status"] == "submitted"


# ---------------------------------------------------------------------------
# e. 收藏隔离
# ---------------------------------------------------------------------------
def test_favorites_are_scoped_per_project(http_server):
    _create_project(http_server, PROJECT_B, "Project B")
    _create_project(http_server, PROJECT_C, "Project C")

    code, fav = _request(http_server, f"/api/projects/{PROJECT_B}/favorite", "PUT")
    assert code == 200 and fav["favorite"] is True
    code, b = _request(http_server, f"/api/projects/{PROJECT_B}")
    assert code == 200 and b["project"]["favorite"] is True
    code, a = _request(http_server, f"/api/projects/{DDP}")
    assert code == 200 and a["project"]["favorite"] is False
    code, c = _request(http_server, f"/api/projects/{PROJECT_C}")
    assert code == 200 and c["project"]["favorite"] is False

    code, listed = _request(http_server, "/api/projects?favorite=1")
    assert code == 200 and {item["id"] for item in listed["items"]} == {PROJECT_B}

    code, _ = _request(http_server, f"/api/projects/{PROJECT_C}/favorite", "PUT")
    assert code == 200
    code, listed = _request(http_server, "/api/projects?favorite=1")
    assert {item["id"] for item in listed["items"]} == {PROJECT_B, PROJECT_C}

    code, unfav = _request(http_server, f"/api/projects/{PROJECT_B}/favorite", "DELETE")
    assert code == 200 and unfav["favorite"] is False
    code, listed = _request(http_server, "/api/projects?favorite=1")
    assert {item["id"] for item in listed["items"]} == {PROJECT_C}
    code, b = _request(http_server, f"/api/projects/{PROJECT_B}")
    assert b["project"]["favorite"] is False


# ---------------------------------------------------------------------------
# f. 最近访问顺序
# ---------------------------------------------------------------------------
def test_recent_projects_follow_visit_order(http_server, monkeypatch):
    _create_project(http_server, PROJECT_B, "Project B")
    _create_project(http_server, PROJECT_C, "Project C")

    # 固定递增时钟，避免同一秒内 rowid 兜底导致断言不稳定。
    ticks = count()
    base_time = datetime(2026, 11, 3, 0, 0, 0, tzinfo=timezone.utc)

    def _fake_now() -> str:
        return (base_time + timedelta(minutes=next(ticks))).isoformat(timespec="seconds")

    monkeypatch.setattr(store, "now_iso", _fake_now)

    for project in (DDP, PROJECT_B, PROJECT_C):
        code, _ = _request(http_server, f"/api/projects/{project}/visit", "POST", {})
        assert code == 200
    code, recent = _request(http_server, "/api/me/recent-projects")
    assert code == 200
    assert [item["id"] for item in recent["items"]] == [PROJECT_C, PROJECT_B, DDP]

    # 再次访问 DDP 后排到最前
    code, _ = _request(http_server, f"/api/projects/{DDP}/visit", "POST", {})
    assert code == 200
    code, recent = _request(http_server, "/api/me/recent-projects")
    assert [item["id"] for item in recent["items"]] == [DDP, PROJECT_C, PROJECT_B]


# ---------------------------------------------------------------------------
# g. /api/state 项目隔离且结构一致
# ---------------------------------------------------------------------------
def test_state_endpoint_is_project_scoped(http_server):
    _create_project(http_server, PROJECT_B, "Project B")
    _create_task(http_server, PROJECT_B, "B-1", "B 项目任务")

    code, state_a = _request(http_server, f"/api/state?project={DDP}")
    assert code == 200
    code, state_b = _request(http_server, f"/api/state?project={PROJECT_B}")
    assert code == 200

    assert state_a["project"]["id"] == DDP
    assert state_b["project"]["id"] == PROJECT_B
    assert state_a["project"]["name"] != state_b["project"]["name"]

    # 兼容层字段结构一致
    assert set(state_a) == set(state_b)
    assert [item["key"] for item in state_a["kanban"]] == [item["key"] for item in state_b["kanban"]]

    ids_a = {item.get("id") for item in state_a["tasks"]}
    ids_b = {item.get("id") for item in state_b["tasks"]}
    titles_b = {item.get("title") for item in state_b["tasks"]}
    assert "TEAM-001" in ids_a and "TEAM-001" not in ids_b
    assert "B-1" in ids_b and "B-1" not in ids_a
    assert "B 项目任务" in titles_b


# ---------------------------------------------------------------------------
# h. 审计按项目过滤
# ---------------------------------------------------------------------------
def test_audit_logs_are_filtered_by_project(http_server):
    _create_project(http_server, PROJECT_B, "Project B")
    _create_project(http_server, PROJECT_C, "Project C")
    for project, key in ((PROJECT_B, "B-1"), (PROJECT_C, "C-1")):
        task = _create_task(http_server, project, key)
        code, moved = _request(
            http_server, f"/api/projects/{project}/tasks/{task['id']}/transition", "POST",
            {"to_status": "in_progress", "expected_version": task["version"]},
        )
        assert code == 200 and moved["task"]["status"] == "in_progress"

    code, logs_b = _request(
        http_server, f"/api/audit-logs?project={PROJECT_B}&action=task.transition",
    )
    assert code == 200 and logs_b["total"] == 1
    assert logs_b["items"][0]["project_id"] == PROJECT_B
    assert logs_b["items"][0]["detail"]["task_key"] == "B-1"

    code, logs_c = _request(
        http_server, f"/api/audit-logs?project={PROJECT_C}&action=task.transition",
    )
    assert code == 200 and logs_c["total"] == 1
    assert logs_c["items"][0]["project_id"] == PROJECT_C
    assert logs_c["items"][0]["detail"]["task_key"] == "C-1"

    # 全量查询能看到两个项目，但按项目查询不串项目
    code, logs_all = _request(http_server, "/api/audit-logs?action=project.create")
    assert code == 200 and logs_all["total"] == 2
    assert {item["project_id"] for item in logs_all["items"]} == {PROJECT_B, PROJECT_C}

    code, logs_b_all = _request(http_server, f"/api/audit-logs?project={PROJECT_B}")
    assert code == 200
    assert {item["project_id"] for item in logs_b_all["items"]} == {PROJECT_B}
