"""OpenAPI 3.1 契约测试：结构、路径覆盖、$ref 可解析、真实 HTTP 可读。"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from urllib.request import urlopen

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agents_dashboard as dash
from agent_platform import openapi


def _doc():
    return openapi.build_openapi()


def test_version_and_info():
    doc = _doc()
    assert doc["openapi"] == "3.1.0"
    assert doc["info"]["version"] == openapi.API_VERSION == "2.0"
    assert doc["servers"][0]["url"].startswith("http://127.0.0.1:")


def test_every_declared_path_documented_with_method():
    doc = _doc()
    for method, path in openapi.API_PATHS:
        assert path in doc["paths"], f"OpenAPI 缺少路径 {path}"
        assert method.lower() in doc["paths"][path], f"{path} 缺少方法 {method}"


def test_refs_resolve():
    doc = _doc()
    schemas = doc["components"]["schemas"]
    refs: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref":
                    refs.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc)
    assert refs, "文档中应存在 $ref"
    for ref in refs:
        assert ref.startswith("#/components/schemas/"), ref
        assert ref.split("/")[-1] in schemas, f"未定义 schema: {ref}"


def test_error_schema_shape():
    err = _doc()["components"]["schemas"]["Error"]
    assert err["required"] == ["detail"]
    assert set(err["properties"]["detail"]["required"]) == {"code", "message"}


def test_core_paths_present():
    paths = _doc()["paths"]
    for path in (
        "/api/state",
        "/api/projects",
        "/api/projects/{project_id}/tasks",
        "/api/projects/{project_id}/tasks/{task_id}/transition",
        "/api/projects/{project_id}/deliveries/{delivery_id}/review",
        "/api/projects/{project_id}/executions",
        "/api/projects/{project_id}/favorite",
        "/api/team-templates",
        "/api/audit-logs",
    ):
        assert path in paths


def test_served_over_http():
    server = dash.ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urlopen(base + "/api/openapi.json", timeout=10) as resp:
            assert resp.status == 200
            assert resp.headers.get_content_type() == "application/json"
            body = json.loads(resp.read().decode("utf-8"))
        assert body["openapi"] == "3.1.0"
        assert len(body["paths"]) == len({p for _, p in openapi.API_PATHS})
    finally:
        server.shutdown()
        server.server_close()
