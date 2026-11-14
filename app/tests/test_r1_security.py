"""R1 安全回归：S-1 / S-2 / S-4 / S-5 / S-6。

这些用例锁的是「审计确认的越权路径必须被堵住」，不是功能行为。
每个用例都对应 R1 的一项修复，改动实现时若绕过就会红。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import executor_policy, service, store
from agent_platform.p4 import governance


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ai-team").mkdir()
    (repo / "ai-team" / "TASK_BOARD.md").write_text("# Board\n", encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"projects": []}), encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_CONFIG", str(config))
    (tmp_path / "projects").mkdir()
    monkeypatch.setenv("AGWS_PLATFORM_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("AGWS_PLATFORM_REPO", str(repo))
    service.bootstrap(repo_root=repo)
    return SimpleNamespace(repo=repo, tmp=tmp_path)


# ---------------------------------------------------------------- S-4
def test_bg_policy_blocks_dangerous_tools():
    """S-4：后台会话的统一策略必须真的包含高危工具 deny 规则。"""
    args = executor_policy.bg_policy_args()
    assert args[0] == "--disallowedTools"
    rules = set(args[1:])
    for must in ("Bash(git push:*)", "Bash(rm:*)", "Bash(sudo:*)", "Bash(curl:*)", "WebFetch"):
        assert must in rules, f"缺少必须禁止的能力：{must}"


def test_bg_policy_covers_all_launch_paths():
    """S-4：team 的三条 --bg 路径都必须带策略，不允许存在绕过入口。"""
    src = (SCRIPTS / "agent_platform" / "team.py").read_text(encoding="utf-8")
    # 每一处 "--bg" 启动都要伴随 bg_policy_args()
    bg_sites = [i for i, line in enumerate(src.splitlines(), 1) if '"--bg"' in line]
    assert bg_sites, "没有找到 --bg 启动点，测试本身失效"
    for lineno in bg_sites:
        window = "\n".join(src.splitlines()[lineno - 1: lineno + 6])
        assert ("bg_policy_args" in window or "policy_args" in window), \
            f"第 {lineno} 行的 --bg 启动未接入策略"


def test_bridge_deny_list_matches_platform():
    """S-4：桥接内置的 deny 列表必须与平台一致，两边不一致就是旁路。"""
    bridge = (SCRIPTS.parent / "scripts" / "session-bridge.py").read_text(encoding="utf-8")
    for rule in executor_policy.BG_DISALLOWED_TOOLS:
        assert rule in bridge, f"桥接缺少规则：{rule}"


# ---------------------------------------------------------------- S-5
def test_governance_gate_fails_closed(monkeypatch):
    """S-5：治理层异常必须拒绝，不能降级为匿名全权限。"""
    src = (SCRIPTS / "agents_dashboard.py").read_text(encoding="utf-8")
    # 关键断言：旧实现的 "except Exception: self.principal = None; return True" 已不存在
    assert "self.principal = None\n        return True" not in src
    # 新实现必须显式区分三种失败
    for code in ("unauthenticated", "unknown_principal", "governance_unavailable"):
        assert code in src, f"缺少失败码 {code}"


def test_principal_active_rejects_inactive(env):
    """S-5：停用/过期主体不得被视为可用。"""
    assert governance.principal_active(None) == (False, "unknown_principal")
    assert governance.principal_active({"status": "disabled", "id": "x"})[0] is False
    assert governance.principal_active({"status": "active", "id": "x"})[0] is True


# ---------------------------------------------------------------- S-1
def test_state_route_has_project_gate():
    """S-1：/api/state 必须过 _gate_project，不能只查项目是否存在。"""
    src = (SCRIPTS / "agents_dashboard.py").read_text(encoding="utf-8")
    idx = src.index('path == "/api/state"')
    window = src[idx: idx + 700]
    assert "_gate_project" in window, "/api/state 未接入项目级授权"


def test_file_routes_raise_access_denied():
    """S-1：/view 与 /api/file 在授权失败时必须停止，不能继续读文件。"""
    src = (SCRIPTS / "agents_dashboard.py").read_text(encoding="utf-8")
    assert src.count("except PlatformError_AccessDenied:") >= 2
    # _project_from_query 内部必须调用 gate
    idx = src.index("def _project_from_query")
    window = src[idx: idx + 2500]
    assert "_gate_project" in window, "_project_from_query 未校验项目权限"


# ---------------------------------------------------------------- S-2
def test_no_identity_from_request_body():
    """S-2：审批身份不得再取自请求体。"""
    src = (SCRIPTS / "agents_dashboard.py").read_text(encoding="utf-8")
    for bad in ('body.get("approved_by")', 'body.get("requested_by")'):
        assert bad not in src, f"仍从请求体取身份：{bad}"
    assert "_actor_id" in src, "缺少可信身份取值方法"


def test_separation_of_duties_uses_real_ids(env):
    """S-2：同一主体不能既请求又批准。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        from agent_platform.p4 import release as release_service
        # 直接验证服务层职责分离逻辑对相同 id 生效
        conn.execute(
            "INSERT INTO approvals (id, project_id, resource_type, resource_id, action,"
            " requested_by, status, digest, created_at)"
            " VALUES ('ap1', 'default', 'agent_release', 'r1', 'publish', 'alice', 'pending', 'd', ?)",
            (store.now_iso(),),
        )
    finally:
        conn.commit()
        conn.close()

    conn = store.connect()
    try:
        with pytest.raises(release_service.ReleaseError) as err:
            release_service.approve(conn, "ap1", approved_by="alice")
        assert err.value.code == "separation_of_duties"
    finally:
        conn.close()


# ---------------------------------------------------------------- S-6
def test_execution_enabled_is_high_risk():
    """S-6：execution_enabled 必须被识别为高风险设置。"""
    src = (SCRIPTS / "agents_dashboard.py").read_text(encoding="utf-8")
    assert 'HIGH_RISK_SETTINGS = frozenset({"execution_enabled"})' in src
    idx = src.index('path == "/api/projects"' if False else "def do_PATCH")
    window = src[idx: idx + 1200]
    assert "_guard_high_risk_settings" in window, "PATCH 未接入高风险设置闸门"


def test_operator_cannot_toggle_execution(env):
    """S-6：运营者没有 exec_runtime:toggle 权限（只有 owner 有）。"""
    operator = governance.ROLE_PERMISSIONS["operator"]
    assert "toggle" not in operator.get("exec_runtime", frozenset())
    assert "*" in governance.ROLE_PERMISSIONS["owner"]["*"]


# ---------------------------------------------------------------- S-1 端到端越权
def test_remote_principal_cannot_read_other_project(env, monkeypatch):
    """S-1 端到端：非项目成员的主体访问 /api/state、/view、/api/file 必须被拒。

    这是审计里最关键的越权路径。用真实服务栈（策略层 + 主体解析）验证，
    不依赖端口和网络：直接调用 Handler 的三个入口所依赖的授权函数。
    """
    from agent_platform.p4 import governance, policy

    conn = store.connect()
    try:
        store.init_db(conn)
        # remote-token 是真实库里就存在的主体：不是任何项目的成员
        remote = governance.get_principal(conn, "remote-token")
        assert remote is not None

        for project_id in ("default",):
            for action in ("read", "update"):
                decision = policy.decide(
                    conn, principal=remote, project_id=project_id,
                    resource_type="*", action=action, risk="R1", domain=None,
                )
                assert decision.allowed is False, (
                    f"remote-token 竟然可以 {action} 项目 {project_id}"
                )
    finally:
        conn.close()


def test_owner_principal_can_read_own_project(env):
    """S-1 反向断言：owner 对自己项目的读取必须仍然放行（不能把正常路径堵死）。"""
    from agent_platform.p4 import governance, policy

    conn = store.connect()
    try:
        store.init_db(conn)
        now = store.now_iso()
        conn.execute(
            "INSERT OR IGNORE INTO principals (id, principal_type, display_name,"
            " is_platform_owner, status, created_at, updated_at)"
            " VALUES ('p-test', 'human', '测试用户', 0, 'active', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO project_members (id, project_id, principal_id, role, created_at)"
            " VALUES ('pm-test', 'default', 'p-test', 'owner', ?)",
            (now,),
        )
        conn.commit()
        owner = governance.get_principal(conn, "p-test")
        decision = policy.decide(
            conn, principal=owner, project_id="default",
            resource_type="*", action="read", risk="R1", domain=None,
        )
        assert decision.allowed is True, "owner 被误拒，正常路径被破坏"
    finally:
        conn.close()
