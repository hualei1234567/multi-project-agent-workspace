"""P4 治理闸门在 HTTP 层的效果：Kill Switch 秒级生效，本机不被打断。"""

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
from agent_platform import service, sources, store
from agent_platform.p4 import killswitch


@pytest.fixture(autouse=True)
def _no_ccusage(monkeypatch):
    monkeypatch.setattr(sources, "ccusage_session_costs", list)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "platform.db"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    repo = tmp_path / "repo"
    (repo / "ai-team").mkdir(parents=True)
    (repo / "ai-team" / "TASK_BOARD.md").write_text("# Board\n", encoding="utf-8")
    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(db_path))
    config_file = tmp_path / "projects.json"
    config_file.write_text(json.dumps({"projects": []}), encoding="utf-8")
    monkeypatch.setenv("DDP_AGENT_PLATFORM_CONFIG", str(config_file))
    monkeypatch.setenv("DDP_AGENT_PLATFORM_PROJECTS_ROOT", str(projects_root))
    monkeypatch.setenv("DDP_AGENT_PLATFORM_REPO", str(repo))
    service.bootstrap(repo_root=repo)
    killswitch.reset_cache()
    yield SimpleNamespace(db=db_path, repo=repo, projects_root=projects_root)
    killswitch.reset_cache()


@pytest.fixture()
def server(env, monkeypatch):
    monkeypatch.setattr(dash, "REPO", env.repo)
    monkeypatch.setattr(dash, "PROJECTS_ROOT", env.projects_root)
    monkeypatch.setattr(dash, "STATUS_DIR", env.repo / "ai-team" / "status")
    monkeypatch.setattr(dash, "SLUG_PREFIXES", ())
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
        with urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_api_works_normally_without_switch(server):
    code, data = _request(server, "/api/projects")
    assert code == 200
    assert "items" in data


def test_global_kill_switch_blocks_api_immediately(server, env):
    conn = store.connect(env.db)
    try:
        killswitch.trigger(conn, scope="global", reason="演练", triggered_by="test")
    finally:
        conn.close()

    code, data = _request(server, "/api/projects")
    assert code == 503
    assert data["error"] == "kill_switch"


def test_release_restores_service(server, env):
    conn = store.connect(env.db)
    try:
        switch = killswitch.trigger(
            conn, scope="global", reason="演练", triggered_by="test"
        )
        assert _request(server, "/api/projects")[0] == 503
        killswitch.release(conn, switch["id"], released_by="test")
    finally:
        conn.close()
    assert _request(server, "/api/projects")[0] == 200


def test_evaluation_flow_over_http(server):
    code, project = _request(server, "/api/projects", "POST",
                             {"code": "evalp", "name": "评估项目", "session_slugs": []})
    assert code == 200
    pid = project["project"]["id"]

    code, suite = _request(server, f"/api/projects/{pid}/eval/suites", "POST",
                           {"name": "回归套件"})
    assert code == 200, suite
    sid = suite["id"]

    code, dataset = _request(server, f"/api/projects/{pid}/eval/datasets", "POST",
                             {"name": "核心集", "suite_id": sid, "split": "regression"})
    assert code == 200, dataset

    code, version = _request(
        server, f"/api/projects/{pid}/eval/datasets/{dataset['id']}/versions", "POST",
        {"cases": [{"case_key": "c1", "expected": {"answer": "ok"}}]},
    )
    assert code == 200, version
    assert version["status"] == "frozen"

    code, evaluator = _request(server, f"/api/projects/{pid}/eval/evaluators", "POST", {
        "name": "基础规则",
        "rules": {"checks": [{"type": "contains", "field": "answer"}]},
    })
    assert code == 200, evaluator

    code, run = _request(server, f"/api/projects/{pid}/eval/runs", "POST", {
        "dataset_version_id": version["id"],
        "evaluator_version_id": evaluator["version_id"],
        "candidate_outputs": {"c1": "everything ok"},
    })
    assert code == 200, run
    assert run["decision"] == "pass"

    code, fetched = _request(server, f"/api/projects/{pid}/eval/runs/{run['id']}")
    assert code == 200
    assert len(fetched["results"]) == 1


def test_knowledge_flow_over_http(server):
    code, project = _request(server, "/api/projects", "POST",
                             {"code": "knwp", "name": "知识项目", "session_slugs": []})
    pid = project["project"]["id"]

    code, source = _request(server, f"/api/projects/{pid}/knowledge/sources", "POST",
                            {"title": "工程手册", "kind": "note"})
    assert code == 200, source

    code, item = _request(server, f"/api/projects/{pid}/knowledge/items", "POST", {
        "title": "上线流程", "source_id": source["id"],
        "content": "第一步跑 pytest。\n\n第二步部署到局域网。",
    })
    assert code == 200, item
    version_id = item["versions"][0]["id"]

    code, _ = _request(
        server, f"/api/projects/{pid}/knowledge/versions/{version_id}/publish", "POST",
        {"reviewed_by": "qa"},
    )
    assert code == 200

    code, hits = _request(server, f"/api/projects/{pid}/knowledge/search?q=pytest")
    assert code == 200
    assert hits["total"] == 1

    code, _ = _request(
        server, f"/api/projects/{pid}/knowledge/versions/{version_id}/revoke", "POST",
        {"reason": "过期"},
    )
    assert code == 200
    code, hits_after = _request(server, f"/api/projects/{pid}/knowledge/search?q=pytest")
    assert hits_after["total"] == 0


def test_knowledge_secret_blocked_over_http(server):
    code, project = _request(server, "/api/projects", "POST",
                             {"code": "secp", "name": "安全项目", "session_slugs": []})
    pid = project["project"]["id"]
    code, source = _request(server, f"/api/projects/{pid}/knowledge/sources", "POST",
                            {"title": "来源"})
    code, err = _request(server, f"/api/projects/{pid}/knowledge/items", "POST", {
        "title": "泄露", "source_id": source["id"],
        "content": "token: Bearer abcdefghijklmnopqrstuvwxyz123456",
    })
    assert code == 422
    assert err["error"] == "secret_detected"
