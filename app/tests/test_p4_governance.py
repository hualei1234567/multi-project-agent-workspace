"""P4-M1 地基测试：身份授权 / Policy / Kill Switch / 事件平面。

覆盖风险 P4-R19（无身份）、P4-R20（无 Kill Switch）、P4-R14（事件丢失）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import migrate, store
from agent_platform.p4 import events, governance, killswitch, policy


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(tmp_path / "platform.db"))
    conn = store.connect()
    store.init_db(conn)
    for project_id, code, name in (("p1", "p1", "项目一"), ("p2", "p2", "项目二")):
        conn.execute(
            "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
            " VALUES (?, ?, ?, '/tmp/repo', '2026-11-06T00:00:00+00:00',"
            " '2026-11-06T00:00:00+00:00')",
            (project_id, code, name),
        )
    # 迁移在项目为空时就跑过了；这里再跑一次回填，模拟"已有项目 + 升级到 v4"，
    # 同时验证回填是幂等的。
    migrate._migrate_v3_to_v4(conn)
    migrate._migrate_v3_to_v4(conn)
    conn.commit()
    killswitch.reset_cache()
    yield conn
    conn.close()


def test_v4_schema_present(conn: sqlite3.Connection) -> None:
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for table in ("principals", "project_members", "policy_rules",
                  "kill_switches", "approvals", "event_outbox", "event_inbox"):
        assert table in names


def test_schema_version_is_latest(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert row[0] == store.SCHEMA_VERSION


def test_local_owner_bootstrapped_and_is_platform_owner(conn: sqlite3.Connection) -> None:
    owner = governance.get_principal(conn, governance.LOCAL_OWNER)
    assert owner is not None
    assert owner["is_platform_owner"] == 1
    # 迁移后已有的两个项目都应被授权
    for project_id in ("p1", "p2"):
        assert governance.role_in_project(conn, governance.LOCAL_OWNER, project_id) == "owner"


def test_remote_principal_has_no_project_and_is_denied(conn: sqlite3.Connection) -> None:
    remote = governance.resolve_principal(conn, is_loopback=False)
    assert remote is not None and remote["id"] == governance.REMOTE_TOKEN
    allowed, reason = governance.authorize(
        conn, principal=remote, project_id="p1", resource_type="task", action="read"
    )
    assert allowed is False
    assert reason == "not_a_project_member"


def test_operator_can_run_but_not_approve_release(conn: sqlite3.Connection) -> None:
    governance.create_principal(
        conn, principal_id="op1", principal_type="user", display_name="操作员"
    )
    governance.set_member(conn, project_id="p1", principal_id="op1", role="operator")
    op = governance.get_principal(conn, "op1")

    ok, _ = governance.authorize(
        conn, principal=op, project_id="p1", resource_type="dispatch_item", action="run"
    )
    assert ok is True

    ok, reason = governance.authorize(
        conn, principal=op, project_id="p1", resource_type="agent_release", action="approve"
    )
    assert ok is False
    assert "cannot" in reason


def test_agent_cannot_cross_project(conn: sqlite3.Connection) -> None:
    governance.create_principal(
        conn, principal_id="agent-a", principal_type="agent",
        display_name="Agent A", agent_project_id="p1",
    )
    governance.set_member(conn, project_id="p2", principal_id="agent-a", role="owner")
    agent = governance.get_principal(conn, "agent-a")
    ok, reason = governance.authorize(
        conn, principal=agent, project_id="p2", resource_type="task", action="read"
    )
    assert ok is False
    assert reason == "agent_project_mismatch"


def test_kill_switch_blocks_and_release_unblocks(conn: sqlite3.Connection) -> None:
    owner = governance.get_principal(conn, governance.LOCAL_OWNER)
    before = policy.decide(
        conn, principal=owner, project_id="p1", resource_type="task", action="write"
    )
    assert before.allowed is True

    switch = killswitch.trigger(
        conn, scope="project", project_id="p1", reason="演练", triggered_by="test"
    )
    blocked = policy.decide(
        conn, principal=owner, project_id="p1", resource_type="task", action="write"
    )
    assert blocked.allowed is False
    assert blocked.blocked_by == "kill_switch"

    killswitch.release(conn, switch["id"], released_by="test")
    after = policy.decide(
        conn, principal=owner, project_id="p1", resource_type="task", action="write"
    )
    assert after.allowed is True


def test_global_kill_switch_blocks_all_projects(conn: sqlite3.Connection) -> None:
    owner = governance.get_principal(conn, governance.LOCAL_OWNER)
    killswitch.trigger(conn, scope="global", reason="全局演练", triggered_by="test")
    for project_id in ("p1", "p2"):
        decision = policy.decide(
            conn, principal=owner, project_id=project_id,
            resource_type="task", action="read",
        )
        assert decision.allowed is False


def test_domain_kill_switch_only_blocks_that_domain(conn: sqlite3.Connection) -> None:
    owner = governance.get_principal(conn, governance.LOCAL_OWNER)
    killswitch.trigger(
        conn, scope="project_domain", project_id="p1", domain="evaluation",
        reason="评估演练", triggered_by="test",
    )
    blocked = killswitch.is_blocked(conn, project_id="p1", domain="evaluation")
    assert blocked[0] is True
    other = killswitch.is_blocked(conn, project_id="p1", domain="knowledge")
    assert other[0] is False
    assert policy.decide(
        conn, principal=owner, project_id="p1", resource_type="task", action="read"
    ).allowed is True


def test_policy_deny_rule_beats_platform_owner(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO policy_rules (id, scope, resource_type, action, effect,"
        " requires_approval, enabled, note, created_at)"
        " VALUES ('r-deny', 'global', 'execution', '*', 'deny', 0, 1,"
        " '生产执行一律禁止', '2026-11-06T00:00:00+00:00')"
    )
    conn.commit()
    owner = governance.get_principal(conn, governance.LOCAL_OWNER)
    decision = policy.decide(
        conn, principal=owner, project_id="p1", resource_type="execution", action="run"
    )
    assert decision.allowed is False
    assert decision.blocked_by == "policy_rule"


def test_policy_rule_requiring_approval(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO policy_rules (id, scope, resource_type, action, effect,"
        " requires_approval, enabled, note, created_at)"
        " VALUES ('r-appr', 'global', 'agent_release', 'publish', 'allow', 1, 1,"
        " '发布需审批', '2026-11-06T00:00:00+00:00')"
    )
    conn.commit()
    owner = governance.get_principal(conn, governance.LOCAL_OWNER)
    decision = policy.decide(
        conn, principal=owner, project_id="p1",
        resource_type="agent_release", action="publish",
    )
    assert decision.allowed is True
    assert decision.requires_approval is True
    with pytest.raises(policy.PolicyDenied):
        policy.require(
            conn, principal=owner, project_id="p1",
            resource_type="agent_release", action="publish",
        )


def test_event_outbox_emit_dispatch_and_dedupe(conn: sqlite3.Connection) -> None:
    seen: list[dict] = []
    with store.write_tx(conn):
        event_id = events.emit(
            conn, "evaluation.run.created", project_id="p1",
            resource_type="evaluation_run", resource_id="r1",
            payload={"suite": "s1"}, correlation_id="corr-1",
        )
    assert len(events.pending(conn)) == 1

    stats = events.dispatch_pending(conn, seen.append)
    assert stats["dispatched"] == 1
    assert seen[0]["event_id"] == event_id
    assert seen[0]["correlation_id"] == "corr-1"

    # 已经投递过，不应重复产生副作用
    with store.write_tx(conn):
        events.emit(conn, "evaluation.run.created")
    again = events.dispatch_pending(conn, seen.append)
    assert again["dispatched"] == 1
    assert len(seen) == 2

    # 重放同一条事件：清掉去重记录后才能再次投递
    assert events.replay(conn, event_id, consumer="default") is True
    replayed = events.dispatch_pending(conn, seen.append)
    assert replayed["dispatched"] >= 1


def test_event_handler_failure_is_recorded(conn: sqlite3.Connection) -> None:
    with store.write_tx(conn):
        events.emit(conn, "workflow.test.completed", project_id="p1")

    def boom(_envelope):
        raise RuntimeError("consumer down")

    stats = events.dispatch_pending(conn, boom, max_attempts=1)
    assert stats["failed"] == 1
    row = conn.execute("SELECT status, last_error FROM event_outbox").fetchone()
    assert row["status"] == "failed"
    assert "consumer down" in row["last_error"]


def test_write_tx_rolls_back_outbox_with_business_state(conn: sqlite3.Connection) -> None:
    """outbox 与业务写入同事务：业务回滚时事件也不应留下。"""
    with pytest.raises(RuntimeError), store.write_tx(conn):
        events.emit(conn, "knowledge.version.published", project_id="p1")
        raise RuntimeError("业务失败")
    assert events.pending(conn) == []
