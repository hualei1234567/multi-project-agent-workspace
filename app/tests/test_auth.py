"""访问控制（M3-4 T3-401）：本机免令牌，远端必须带 Bearer 令牌。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import auth


class _Headers(dict):
    """模拟 http.client 的 headers（get 大小写不敏感由 BaseHTTPRequestHandler 保证）。"""


@pytest.fixture()
def token_env(tmp_path, monkeypatch):
    path = tmp_path / "token"
    monkeypatch.setenv("DDP_AGENT_PLATFORM_TOKEN_FILE", str(path))
    # 这些用例验证的是"开启鉴权"时的行为；默认已改为 off，这里显式打开。
    monkeypatch.setenv("DDP_AGENT_PLATFORM_AUTH", "on")
    auth.reset_cache()
    yield path
    auth.reset_cache()


def _check(path="/api/projects", addr="127.0.0.1", headers=None, query=None):
    return auth.check_request(
        path=path, client_address=addr, headers=_Headers(headers or {}),
        query_params=query or {},
    )


# ---------------------------------------------------------------------------
# 本机放行
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("addr", ["127.0.0.1", "::1", "127.0.0.5"])
def test_loopback_allowed_without_token(token_env, addr):
    ok, reason = _check(addr=addr)
    assert ok, reason


def test_loopback_allowed_for_open_path(token_env):
    ok, _ = _check(path="/api/open", addr="127.0.0.1")
    assert ok


# ---------------------------------------------------------------------------
# 远端必须带令牌
# ---------------------------------------------------------------------------
def test_remote_without_token_rejected(token_env):
    ok, reason = _check(addr="100.117.129.23")
    assert not ok
    assert reason == "missing_token"


def test_remote_with_valid_token_allowed(token_env):
    token = auth.current_token()
    ok, reason = _check(addr="100.117.129.23", headers={"Authorization": f"Bearer {token}"})
    assert ok, reason


def test_remote_with_wrong_token_rejected(token_env):
    ok, reason = _check(addr="100.117.129.23", headers={"Authorization": "Bearer wrong"})
    assert not ok
    assert reason == "invalid_token"


def test_remote_can_use_query_token_for_first_page_load(token_env):
    """移动端首次打开页面时用 ?token=，之后前端存 localStorage。"""
    token = auth.current_token()
    ok, _ = _check(addr="100.117.129.23", query={"token": [token]})
    assert ok


def test_open_path_loopback_only_even_with_token(token_env):
    """/api/open 会用系统程序打开本机文件，远端一律拒绝。"""
    token = auth.current_token()
    ok, reason = _check(
        path="/api/open", addr="100.117.129.23",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert not ok
    assert reason == "loopback_only"


# ---------------------------------------------------------------------------
# 令牌文件
# ---------------------------------------------------------------------------
def test_token_file_created_with_0600(token_env):
    token = auth.current_token()
    assert token
    assert token_env.is_file()
    assert oct(token_env.stat().st_mode)[-3:] == "600"


def test_token_is_stable_across_reads(token_env):
    first = auth.current_token()
    auth.reset_cache()
    assert auth.current_token() == first


def test_public_paths_need_no_token(token_env):
    for path in ("/healthz", "/api/auth/token"):
        ok, _ = _check(path=path, addr="100.117.129.23")
        assert ok, path


# ---------------------------------------------------------------------------
# 鉴权关闭时（默认）：非回环也放行，但 /api/open 仍只限本机
# ---------------------------------------------------------------------------
def _check_off(path, addr, headers=None, query=None):
    return auth.check_request(
        path=path, client_address=addr, headers=_Headers(headers or {}),
        query_params=query or {}, auth_enabled=False,
    )


def test_disabled_allows_remote_without_token(token_env):
    ok, reason = _check_off("/api/projects", "203.0.113.7")
    assert ok is True and reason == ""


def test_disabled_still_blocks_open_endpoint_for_remote(token_env):
    ok, reason = _check_off("/api/open", "203.0.113.7")
    assert ok is False and reason == "loopback_only"


def test_default_env_is_off(monkeypatch, token_env):
    monkeypatch.delenv("DDP_AGENT_PLATFORM_AUTH", raising=False)
    ok, _ = auth.check_request(
        path="/api/projects", client_address="203.0.113.7", headers=_Headers({})
    )
    assert ok is True
