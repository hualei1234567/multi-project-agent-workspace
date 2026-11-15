"""调度门面测试（P3-M3 / ADR-P3-002）。

背景：平台原先有两条构造 `claude` 命令的路径——
`executor.py` 走 Policy + 审批，`team.py` 直接 `_run()` 只套工具白名单。
蓝图 §2 点名这是「旧入口绕过 Facade」。

本测试锁定门面闸门真实生效。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import facade, store
from agent_platform.p4 import governance, killswitch


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    conn = store.connect()
    store.init_db(conn)
    conn.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('p1','p1','项目一','/tmp/r','2026-11-15T00:00:00+00:00',"
        " '2026-11-15T00:00:00+00:00')")
    conn.commit()
    killswitch.reset_cache()
    yield conn
    conn.close()
    killswitch.reset_cache()


def _principal(conn):
    return governance.get_principal(conn, governance.LOCAL_OWNER)


# ---- 允许路径 ----
def test_allowed_entry_passes(env):
    result = facade.guard(env, entry="team.launch", project_id="p1",
                          principal=_principal(env))
    assert result["policy"] == "allowed"


def test_readonly_entry_skips_policy(env):
    result = facade.guard(env, entry="team.preflight", project_id="p1",
                          principal=None)
    assert result["policy"] == "skipped_readonly"


def test_unknown_entry_rejected(env):
    with pytest.raises(facade.FacadeError) as exc:
        facade.guard(env, entry="some.random.entry", project_id="p1",
                     principal=_principal(env))
    assert exc.value.code == "unknown_entry"


# ---- Kill Switch 优先 ----
def test_kill_switch_blocks_before_policy(env):
    # 激活状态靠 released_at 为空判定，没有 active 列
    env.execute(
        "INSERT INTO kill_switches (id, scope, project_id, domain, reason,"
        " triggered_by, created_at) VALUES ('k1','global',NULL,NULL,'紧急停止',"
        " 'test','2026-11-15T00:00:00+00:00')")
    env.commit()
    killswitch.reset_cache()
    with pytest.raises(facade.FacadeError) as exc:
        facade.guard(env, entry="team.launch", project_id="p1",
                     principal=_principal(env))
    assert exc.value.code == "kill_switch_active"


# ---- 主体无效 ----
def test_no_principal_rejected(env):
    with pytest.raises(facade.FacadeError) as exc:
        facade.guard(env, entry="team.launch", project_id="p1", principal=None)
    assert exc.value.code == "policy_denied"


def test_revoked_principal_rejected(env):
    env.execute("UPDATE principals SET status='revoked' WHERE id='local-owner'")
    env.commit()
    principal = governance.get_principal(env, governance.LOCAL_OWNER)
    with pytest.raises(facade.FacadeError) as exc:
        facade.guard(env, entry="team.launch", project_id="p1", principal=principal)
    assert exc.value.code == "policy_denied"


# ---- 审计 ----
def test_denial_is_audited(env):
    facade.audit_denial(env, entry="team.launch", project_id="p1",
                        actor="intruder", reason="没有权限")
    row = env.execute(
        "SELECT * FROM audit_logs WHERE action LIKE 'facade.denied:%'"
    ).fetchone()
    assert row is not None, "拒绝必须留痕，否则查不出谁在试图绕过"
    assert row["result"] == "denied"
    assert row["actor"] == "intruder"


# ---- argv 构造收敛 ----
def test_argv_construction_centralized():
    """argv 由门面统一构造，team.py 不再自己拼。"""
    argv = facade.team_launch_argv(["--disallowedTools", "Bash"],
                                   agent="backend-engineer", prompt="干活")
    assert argv[0].endswith("claude") or argv[0] == ""
    assert "--agent" in argv
    assert "backend-engineer" in argv
    assert "--bg" in argv
    assert "--disallowedTools" in argv
    assert argv[-1] == "干活"


def test_argv_without_agent():
    argv = facade.team_launch_argv([], agent=None, prompt="p")
    assert "--agent" not in argv
    assert "--bg" in argv
