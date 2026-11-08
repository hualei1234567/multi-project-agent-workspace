"""运行时控制（M3-3 T3-301/302/303）。

重点验证「受控」：动作白名单、预检令牌、二次确认、只读动作免确认。
不真正执行有副作用的动作（restart/deploy），只测拒绝路径与只读路径。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import runtime, service


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


# ---------------------------------------------------------------------------
# 动作白名单
# ---------------------------------------------------------------------------
def test_unknown_action_rejected():
    with pytest.raises(runtime.RuntimeError_) as exc:
        runtime.preflight("rm_rf_everything")
    assert exc.value.code == "not_found"


def test_catalog_has_no_shell_strings():
    """每个动作的 argv 必须是元组，不接受单个可拼接的 shell 字符串。"""
    for key, spec in runtime.ACTION_CATALOG.items():
        assert isinstance(spec.argv, tuple), key
        assert all(isinstance(part, str) for part in spec.argv), key


def test_destructive_actions_require_confirm():
    for key in ("restart_dashboard", "docker_restart_material"):
        assert runtime.ACTION_CATALOG[key].requires_confirm is True


def test_readonly_actions_do_not_require_confirm():
    for key in ("docker_ps_material", "disk_usage"):
        assert runtime.ACTION_CATALOG[key].requires_confirm is False


# ---------------------------------------------------------------------------
# 预检令牌
# ---------------------------------------------------------------------------
def test_preflight_returns_token_and_displayed_command():
    result = runtime.preflight("docker_restart_material")
    assert result["preflight_token"]
    assert "docker compose" in result["command_display"]
    assert result["requires_confirm"] is True


def test_run_without_preflight_token_rejected():
    with pytest.raises(runtime.RuntimeError_) as exc:
        runtime.run_action("docker_restart_material", preflight_token=None, confirm=True)
    assert exc.value.code == "preflight_required"
    assert exc.value.status == 412


def test_run_with_wrong_token_rejected():
    with pytest.raises(runtime.RuntimeError_) as exc:
        runtime.run_action("docker_restart_material", preflight_token="bogus", confirm=True)
    assert exc.value.code == "preflight_required"


def test_run_without_confirm_rejected():
    token = runtime.preflight("docker_restart_material")["preflight_token"]
    with pytest.raises(runtime.RuntimeError_) as exc:
        runtime.run_action("docker_restart_material", preflight_token=token, confirm=False)
    assert exc.value.code == "confirm_required"


def test_token_is_action_specific():
    """A 动作的令牌不能用于 B 动作。"""
    token = runtime.preflight("restart_dashboard")["preflight_token"]
    with pytest.raises(runtime.RuntimeError_) as exc:
        runtime.run_action("docker_restart_material", preflight_token=token, confirm=True)
    assert exc.value.code == "preflight_required"


def test_dry_run_does_not_execute():
    token = runtime.preflight("docker_restart_material")["preflight_token"]
    result = runtime.run_action(
        "docker_restart_material", preflight_token=token, confirm=True, dry_run=True
    )
    assert result["dry_run"] is True
    assert "docker" in result["command_display"]


# ---------------------------------------------------------------------------
# 只读动作真实执行
# ---------------------------------------------------------------------------
def test_readonly_action_runs_and_records(env):
    result = runtime.run_action("docker_ps_material", preflight_token=None, confirm=False)
    assert result["status"] in {"succeeded", "failed"}
    runs = runtime.list_action_runs()
    assert runs["total"] >= 1
    assert runs["items"][0]["action_id"] == "docker_ps_material"


# ---------------------------------------------------------------------------
# 服务登记与采集
# ---------------------------------------------------------------------------
def test_register_and_probe_service(env):
    service_reg = runtime.register_service({
        "name": "本机测试端口", "kind": "port", "target": "127.0.0.1:8099",
    })
    assert service_reg["kind"] == "port"
    listed = runtime.list_services()
    assert listed["total"] == 1
    assert listed["items"][0]["name"] == "本机测试端口"
    assert listed["items"][0]["actions"], "每个服务应暴露可选动作"


def test_probe_unreachable_port_is_down(env):
    runtime.register_service({"name": "死端口", "kind": "port", "target": "127.0.0.1:1"})
    results = runtime.collect_once()
    assert results[0]["status"] == "down"


def test_probe_never_raises(env):
    """采集层任何异常都降级为 unknown，不能打断平台。"""
    runtime.register_service({"name": "怪服务", "kind": "port", "target": "not-a-port"})
    results = runtime.collect_once()
    assert results[0]["status"] in {"down", "unknown"}


def test_duplicate_service_name_rejected(env, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(env.db))
    runtime.register_service({"name": "重复", "kind": "port", "target": "127.0.0.1:1",
                              "project_id": "default"})
    with pytest.raises(runtime.RuntimeError_) as exc:
        runtime.register_service({"name": "重复", "kind": "port", "target": "127.0.0.1:2",
                                  "project_id": "default"})
    assert exc.value.code == "conflict"


def test_invalid_kind_rejected(env):
    with pytest.raises(runtime.RuntimeError_):
        runtime.register_service({"name": "x", "kind": "magic", "target": "y"})
