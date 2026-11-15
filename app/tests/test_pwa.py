"""PWA 外壳（P3-M6 / ADR-P3-005）：manifest / Service Worker / 图标 / 离线页。

两条硬约束在这里被钉死：

1. **`/api/*` 绝不缓存**——用源码断言 + 浏览器实测双重验证。
   源码断言防"以后有人顺手加个 API 缓存"，浏览器实测证明确实没缓存。
2. **图标必须真实可用**——直接解析 PNG 头校验尺寸与格式，不信任"文件存在"。
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import agents_dashboard as dash
from agent_platform import auth, pwa, service, sources
from conftest import local_urlopen


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_real_ccusage(monkeypatch):
    monkeypatch.setattr(sources, "ccusage_session_costs", list)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / "ai-team" / "TASK_BOARD.md", "# Board\n\n| Task ID | 标题 |\n|---|---|\n")
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    config_file = tmp_path / "projects.json"
    config_file.write_text(json.dumps({"projects": []}), encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_CONFIG", str(config_file))
    monkeypatch.setenv("AGWS_PLATFORM_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("AGWS_PLATFORM_REPO", str(repo))
    service.bootstrap(repo_root=repo)
    return SimpleNamespace(repo=repo, root=tmp_path)


@pytest.fixture()
def http_server(env, monkeypatch):
    monkeypatch.setattr(dash, "REPO", env.repo)
    monkeypatch.setattr(dash, "PROJECTS_ROOT", env.root / "projects")
    monkeypatch.setattr(dash, "STATUS_DIR", env.repo / "ai-team" / "status")
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


def _get(base: str, path: str):
    req = Request(base + path, method="GET")
    with local_urlopen(req, timeout=15) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def test_manifest_served_as_json_with_correct_mime(http_server):
    code, ctype, body = _get(http_server, "/manifest.webmanifest")
    assert code == 200
    assert ctype.startswith("application/manifest+json"), ctype
    payload = json.loads(body.decode("utf-8"))
    assert payload["display"] == "standalone"
    assert payload["start_url"] == "/"
    assert payload["scope"] == "/"
    assert payload["name"] and payload["short_name"]


def test_manifest_icons_all_resolve_to_real_png(http_server):
    """图标不能是 404：逐个请求并解析 PNG 头，尺寸必须与声明一致。"""
    _, _, body = _get(http_server, "/manifest.webmanifest")
    icons = json.loads(body.decode("utf-8"))["icons"]
    assert {icon["purpose"] for icon in icons} >= {"any", "maskable"}
    assert any(icon["sizes"] == "512x512" for icon in icons)

    for icon in icons:
        code, ctype, data = _get(http_server, icon["src"])
        assert code == 200, icon["src"]
        assert ctype == "image/png", f"{icon['src']} -> {ctype}"
        assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{icon['src']} 不是 PNG"
        width, height = struct.unpack(">II", data[16:24])
        assert f"{width}x{height}" == icon["sizes"], f"{icon['src']} 实际 {width}x{height}"


def test_manifest_theme_color_matches_page_background():
    """theme_color 必须跟着页面 --bg 走，不能两处手写色值漂移。"""
    html = dash.HTML_PATH.read_text(encoding="utf-8")
    dark_block = re.search(r'html\[data-theme="dark"\]\s*\{(.*?)\}', html, re.DOTALL)
    assert dark_block
    bg = re.search(r"--bg:\s*(#[0-9a-fA-F]{3,8})", dark_block.group(1))
    assert bg
    assert pwa.theme_color().lower() == bg.group(1).lower()
    payload = json.loads(pwa.manifest().decode("utf-8"))
    assert payload["theme_color"].lower() == bg.group(1).lower()
    assert payload["background_color"].lower() == bg.group(1).lower()


# ---------------------------------------------------------------------------
# Service Worker
# ---------------------------------------------------------------------------
def test_service_worker_served_with_javascript_mime(http_server):
    code, ctype, body = _get(http_server, "/sw.js")
    assert code == 200
    assert ctype.startswith("application/javascript"), ctype
    text = body.decode("utf-8")
    for event in ("install", "activate", "fetch"):
        assert f'addEventListener("{event}"' in text, event


def test_service_worker_never_caches_api():
    """ADR-P3-005 硬约束：fetch 里必须先放行 /api/，且缓存写入点不得覆盖它。"""
    text = pwa.service_worker().decode("utf-8")
    fetch_block = text.split('addEventListener("fetch"', 1)[1]
    api_guard = re.search(r'url\.pathname\.startsWith\("/api/"\)\)\s*(?:\{\s*)?return', fetch_block)
    assert api_guard, "fetch 处理器里没有 /api/ 直通分支"

    # 所有 cache.put / cache.add 的实参里都不允许出现 /api/ 字样
    for match in re.finditer(r"cache\.(?:put|add|match)\(([^;]{0,200})", text):
        assert "/api/" not in match.group(1), match.group(0)
    # 预缓存清单里也不允许有 API
    precache = re.search(r"const PRECACHE = \[(.*?)\];", text, re.DOTALL)
    assert precache and "/api/" not in precache.group(1)


def test_service_worker_precaches_core_and_bumps_version():
    text = pwa.service_worker().decode("utf-8")
    for url in ("/", "/offline.html", "/manifest.webmanifest", "/icons/icon-192.png"):
        assert url in text, url
    assert f'const VERSION = "{pwa.SW_VERSION}"' in text
    # activate 必须清掉非当前版本的缓存名
    assert "caches.delete" in text


# ---------------------------------------------------------------------------
# 离线页
# ---------------------------------------------------------------------------
def test_offline_page_is_chinese_and_not_an_error_page(http_server):
    code, ctype, body = _get(http_server, "/offline.html")
    assert code == 200
    assert ctype.startswith("text/html"), ctype
    text = body.decode("utf-8")
    assert "离线" in text
    # 说「离线」而不是「出错」：用词是产品要求，不是文案偏好
    assert "出错" in text or "不是出错" in text
    assert "离线" in text and "暂时连不上" in text


# ---------------------------------------------------------------------------
# 鉴权白名单（远端 HTTPS 下 SW 预缓存不带 Authorization 头）
# ---------------------------------------------------------------------------
def test_pwa_assets_are_public_so_precache_works_remotely(monkeypatch):
    """SW 的 install 预缓存由浏览器发起，不带 Authorization。

    生产 ``AGWS_PLATFORM_AUTH=on`` 时若这些路径要求令牌，远端 HTTPS 访问的
    预缓存会整批 401——离线能力形同虚设。这里把这一点钉住。
    """
    monkeypatch.setenv("AGWS_PLATFORM_AUTH", "on")
    auth.reset_cache()
    try:
        for path in ("/manifest.webmanifest", "/sw.js", "/offline.html",
                     "/icons/icon-192.png"):
            ok, reason = auth.check_request(
                path=path, client_address="100.101.102.103", headers={},
            )
            assert ok, f"{path} 需要令牌（{reason}）会导致远端预缓存 401"
        # 业务接口仍然必须带令牌
        ok, reason = auth.check_request(
            path="/api/projects", client_address="100.101.102.103", headers={},
        )
        assert not ok and reason == "missing_token"
    finally:
        auth.reset_cache()


# ---------------------------------------------------------------------------
# 页面接入
# ---------------------------------------------------------------------------
def test_page_links_manifest_and_stays_functional():
    html = dash.HTML_PATH.read_text(encoding="utf-8")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert "/icons/icon-192.png" in html
    # 注册必须静默：catch 里不能吞掉页面其余逻辑，也不能 alert
    assert 'navigator.serviceWorker.register("/sw.js")' in html
    assert ".catch(() => {" in html
    assert "alert(" not in html.split("initPWA")[1][:1500]


# ---------------------------------------------------------------------------
# SW 语法（真跑 JS 解析器）
# ---------------------------------------------------------------------------
def test_service_worker_passes_javascript_syntax_check(tmp_path):
    """生成的 sw.js 必须是合法 JS。

    模板里混着 Python 字符串转义，一个引号写错就会让 SW 变成语法错误——
    浏览器只会静默「不注册」，页面看起来完全正常，所以必须在测试里解析一次。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("未安装 node，无法做 JS 语法解析")
    script = tmp_path / "sw.js"
    script.write_bytes(pwa.service_worker())
    result = subprocess.run(
        [node, "--check", str(script)], check=False, capture_output=True,
        text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_service_worker_does_not_masquerade_server_errors_as_offline():
    """服务端的 404/500 必须原样返回，不能替换成离线页。

    离线页只回答「没网」；把「地址不存在」显示成「你没网」是在谎报状态，
    排查时会被带偏（这条在实现期间真实出现过）。
    """
    text = pwa.service_worker().decode("utf-8")
    non_shell = text.split("if (!isShell) {", 1)[1].split("const cachedShell", 1)[0]
    assert "if (fresh) {" in non_shell
    assert "if (fresh.ok)" in non_shell
    # 只有网络层抛错（catch 分支）才允许降级到离线页
    assert "catch {" in non_shell
    offline_branch = non_shell.split("if (fresh) {", 1)[1]
    assert "OFFLINE_URL" in offline_branch
