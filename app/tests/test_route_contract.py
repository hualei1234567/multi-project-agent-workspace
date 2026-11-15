"""路由契约完整性：OpenAPI 里登记的每条接口都必须真的被路由到。

目的很具体：防止「文档里有、路由没接」或「路由改名后文档没跟」这类遗漏。
判定方式：用不存在的项目 id 打过去，如果响应还是「接口不存在」，说明该路径
根本没有命中任何 handler；其它任何错误都说明路由已接上。
"""

from __future__ import annotations

import json
import re
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
from agent_platform import openapi, service, sources
from conftest import local_urlopen


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_real_ccusage(monkeypatch):
    monkeypatch.setattr(sources, "ccusage_session_costs", list)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "platform.db"
    projects_root = tmp_path / "projects"
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
    return SimpleNamespace(tmp=tmp_path, repo=repo, projects_root=projects_root,
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


def _request(base: str, path: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(base + path, data=data, method=method,
                  headers={"Content-Type": "application/json"})
    try:
        with local_urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover - 非 JSON 响应直接判失败
            return exc.code, {"raw": raw[:200]}


def _concrete(path: str) -> str:
    """把路径模板换成具体值；项目 id 用不存在的值，确保失败发生在业务之前。"""
    path = path.replace("{project_id}", "__missing__")
    return re.sub(r"\{[^}]+\}", "x", path)


def _is_unrouted(data) -> bool:
    return isinstance(data, dict) and data.get("message") == "接口不存在"


def test_every_project_scoped_route_is_wired(http_server):
    """102 条项目级接口：每条都必须命中 handler（失败在项目校验，而不是没路由）。"""
    unrouted = []
    for method, path in openapi.API_PATHS:
        if "{project_id}" not in path:
            continue
        body = {} if method in {"POST", "PATCH", "PUT"} else None
        code, data = _request(http_server, _concrete(path), method, body)
        assert 200 <= code < 600
        if _is_unrouted(data):
            unrouted.append(f"{method} {path}")
    assert not unrouted, "以下接口没有接路由：" + ", ".join(unrouted)


def test_every_root_get_route_is_wired(http_server):
    """根级只读接口（无副作用）也必须全部可达。"""
    unrouted = []
    for method, path in openapi.API_PATHS:
        if "{project_id}" in path or method != "GET":
            continue
        code, data = _request(http_server, _concrete(path))
        assert 200 <= code < 600
        if _is_unrouted(data):
            unrouted.append(f"{method} {path}")
    assert not unrouted, "以下只读接口没有接路由：" + ", ".join(unrouted)


def test_openapi_paths_are_unique_and_documented():
    """登记表本身不能有重复，且每条都能在 _paths() 里找到定义。"""
    seen = set()
    dups = []
    for method, path in openapi.API_PATHS:
        key = (method, path)
        if key in seen:
            dups.append(key)
        seen.add(key)
    assert not dups, f"重复登记：{dups}"
    spec = openapi.build_openapi()
    missing = [
        f"{method.upper()} {path}"
        for method, path in openapi.API_PATHS
        if method.lower() not in spec["paths"].get(path, {})
    ]
    assert not missing, f"OpenAPI 文档缺少操作定义：{missing}"
