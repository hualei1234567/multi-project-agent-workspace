"""执行器准入策略（M3-1 T3-103）。

这是三期风险最高的一处：平台第一次能起外部进程。
这里逐条锁死边界，任何一条被放宽都必须先改测试。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import shutil as _shutil

from agent_platform import executor_policy as policy

# 执行器依赖 claude CLI；没有该二进制的机器（如部署服务器）跳过本模块，
# 不把"环境缺依赖"伪装成"测试通过"。
pytestmark = pytest.mark.skipif(
    _shutil.which("claude") is None,
    reason="需要 claude CLI（执行器依赖），本机未安装",
)


# ---------------------------------------------------------------------------
# 全局开关
# ---------------------------------------------------------------------------
def test_executor_disabled_by_default(monkeypatch):
    monkeypatch.delenv(policy.ENV_SWITCH, raising=False)
    assert policy.executor_enabled() is False
    with pytest.raises(policy.ExecutorError, match="未启用"):
        policy.build_spec(
            project_code="default", repo_root="/tmp", role_key="frontend-engineer",
            execution_mode="worktree", prompt="do it",
        )


def test_executor_enabled_by_env(monkeypatch):
    monkeypatch.setenv(policy.ENV_SWITCH, "on")
    assert policy.executor_enabled() is True


# ---------------------------------------------------------------------------
# 项目开关
# ---------------------------------------------------------------------------
def test_project_execution_disabled_by_default(tmp_path, monkeypatch):
    from agent_platform import store

    db = tmp_path / "p.db"
    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(db))
    conn = store.connect(db)
    try:
        store.init_db(conn)
        conn.execute(
            "INSERT INTO agent_projects (id, code, name, status, repo_root, created_at, updated_at)"
            " VALUES ('x','x','X','active','/tmp','t','t')"
        )
        assert policy.project_execution_enabled(conn, "x") is False
        conn.execute(
            "UPDATE agent_projects SET settings_json = ? WHERE id = 'x'",
            ('{"execution_enabled": true}',),
        )
        assert policy.project_execution_enabled(conn, "x") is True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bash 白名单
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", [
    "git status", "git diff --stat", "git log --oneline -5",
    "python -m pytest tests -q", "ruff check app", "npm run build",
    "ls -la", "grep -r foo .", "cat README.md",
])
def test_allowed_bash_commands(command):
    ok, reason = policy.check_bash_allowed(command)
    assert ok, reason


@pytest.mark.parametrize("command", [
    "git push origin main",
    "git reset --hard HEAD~1",
    "git clean -fdx",
    "rm -rf /tmp/x",
    "docker compose up -d",
    "kubectl get pods",
    "ssh user@host",
    "curl http://example.com",
    "sudo systemctl restart x",
    "bash scripts/deploy-lan.sh",
    "psql -c 'drop table users'",
    "mysql -e 'truncate tasks'",
    "chmod 777 /etc/passwd",
    "echo hi && git push",
    "echo x > /dev/sda",
])
def test_denied_bash_commands(command):
    ok, reason = policy.check_bash_allowed(command)
    assert not ok, f"{command} 应当被拒绝"
    assert reason


def test_empty_bash_command_denied():
    ok, _ = policy.check_bash_allowed("")
    assert not ok


def test_allowed_prefix_but_denied_pattern_wins():
    """前缀白名单里没有 git push，禁止模式也命中 —— 双保险。"""
    ok, reason = policy.check_bash_allowed("git push --force")
    assert not ok
    assert "禁止模式" in reason


# ---------------------------------------------------------------------------
# 工作目录
# ---------------------------------------------------------------------------
def test_cwd_inside_repo_allowed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert policy.validate_cwd("default", repo, repo) == repo.resolve()


def test_cwd_subdir_of_repo_allowed(tmp_path):
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    resolved = policy.validate_cwd("default", repo, repo / "backend")
    assert resolved == (repo / "backend").resolve()


def test_cwd_outside_repo_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(policy.ExecutorError, match="不在允许范围"):
        policy.validate_cwd("default", repo, other)


def test_cwd_traversal_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(policy.ExecutorError):
        policy.validate_cwd("default", repo, repo / ".." / "outside")


# ---------------------------------------------------------------------------
# 角色与执行模式
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["readonly", "main_session"])
def test_non_executable_modes_rejected(tmp_path, monkeypatch, mode):
    monkeypatch.setenv(policy.ENV_SWITCH, "on")
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(policy.ExecutorError, match="不允许自动执行"):
        policy.build_spec(
            project_code="default", repo_root=repo, role_key="qa-devops",
            execution_mode=mode, prompt="do it",
        )


@pytest.mark.parametrize("mode", ["worktree", "subagent"])
def test_executable_modes_accepted(tmp_path, monkeypatch, mode):
    monkeypatch.setenv(policy.ENV_SWITCH, "on")
    repo = tmp_path / "repo"
    repo.mkdir()
    spec = policy.build_spec(
        project_code="default", repo_root=repo, role_key="frontend-engineer",
        execution_mode=mode, prompt="do it",
    )
    assert spec.cwd == repo.resolve()


def test_empty_prompt_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv(policy.ENV_SWITCH, "on")
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(policy.ExecutorError, match="提示为空"):
        policy.build_spec(
            project_code="default", repo_root=repo, role_key="frontend-engineer",
            execution_mode="worktree", prompt="   ",
        )


# ---------------------------------------------------------------------------
# argv 拼装
# ---------------------------------------------------------------------------
def test_argv_uses_whitelisted_binary_and_no_shell(tmp_path, monkeypatch):
    monkeypatch.setenv(policy.ENV_SWITCH, "on")
    repo = tmp_path / "repo"
    repo.mkdir()
    spec = policy.build_spec(
        project_code="default", repo_root=repo, role_key="backend-engineer",
        execution_mode="worktree", prompt="实现接口",
    )
    argv = spec.argv()
    assert argv[0] == "claude"
    # 不给 Bash：执行器层面先禁用，避免前缀白名单被绕过
    assert "--disallowedTools" in argv
    assert argv[argv.index("--disallowedTools") + 1] == "Bash"
    assert "--output-format" in argv
    assert "stream-json" in argv
    # prompt 作为独立参数，不拼进 shell 字符串
    assert "实现接口" in argv


def test_max_turns_and_timeout_clamped(tmp_path, monkeypatch):
    monkeypatch.setenv(policy.ENV_SWITCH, "on")
    repo = tmp_path / "repo"
    repo.mkdir()
    spec = policy.build_spec(
        project_code="default", repo_root=repo, role_key="backend-engineer",
        execution_mode="worktree", prompt="x", max_turns=99999, timeout_s=1,
    )
    assert spec.max_turns == 200
    assert spec.timeout_s == 60


def test_invalid_role_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv(policy.ENV_SWITCH, "on")
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(policy.ExecutorError, match="角色标识非法"):
        policy.build_spec(
            project_code="default", repo_root=repo, role_key="../../etc/passwd",
            execution_mode="worktree", prompt="x",
        )
