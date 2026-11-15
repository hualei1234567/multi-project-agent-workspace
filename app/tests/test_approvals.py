"""通用审批闭环测试。

覆盖背景：``policy.decide()`` 有 ``requires_approval`` 分支，但平台原先
只有 approve 没有「发起申请」入口，且 ``policy_rules`` 生产库为空。
结果是高风险操作（派发执行）点一下就起进程——见
``docs/phase3/audit/P3_M3_AUTONOMOUS_EXECUTION_SAFETY_REVIEW.md``。

本测试锁定修复后的语义：
1. 申请 → 批准 → 消费，只能消费一次；
2. 未批准不能消费；
3. 摘要不匹配视为无审批；
4. 过期不可用；
5. 规则命中后 Policy 返回 requires_approval=True。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import approvals, store
from agent_platform.p4 import governance, policy


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    c = store.connect()
    store.init_db(c)
    c.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('p1', 'p1', '项目一', '/tmp/repo',"
        " '2026-11-15T00:00:00+00:00', '2026-11-15T00:00:00+00:00')"
    )
    c.commit()
    yield c
    c.close()


def _request(conn, **kw):
    base = {"resource_type": "dispatch_item", "resource_id": "item-1",
            "action": "run", "requested_by": "local-owner"}
    base.update(kw)
    return approvals.request(conn, "p1", **base)


def _consume(conn, **kw):
    base = {"resource_type": "dispatch_item", "resource_id": "item-1", "action": "run"}
    base.update(kw)
    return approvals.consume(conn, "p1", **base)


def test_pending_approval_cannot_be_consumed(conn):
    """只有 pending 没批准，不能消费。"""
    _request(conn)
    assert _consume(conn) is None


def test_approved_consumable_exactly_once(conn):
    row = _request(conn)
    approvals.decide(conn, row["id"], approved_by="owner")
    assert _consume(conn) is not None, "批准后应能消费"
    assert _consume(conn) is None, "审批单一次性，不能复用"


def test_rejected_not_consumable(conn):
    row = _request(conn)
    approvals.decide(conn, row["id"], approved_by="owner", decision="reject")
    assert _consume(conn) is None


def test_digest_mismatch_blocks_consumption(conn):
    """批准 A 的对象，不能用来执行 B。"""
    row = _request(conn, resource_id="item-1")
    approvals.decide(conn, row["id"], approved_by="owner")
    assert _consume(conn, resource_id="item-2") is None, "对象不同不应复用审批"


def test_action_mismatch_blocks_consumption(conn):
    row = _request(conn, action="run")
    approvals.decide(conn, row["id"], approved_by="owner")
    assert _consume(conn, action="cancel") is None


def test_expired_approval_rejected(conn):
    row = _request(conn, ttl_seconds=-1)  # 立即过期
    with pytest.raises(approvals.ApprovalError) as exc:
        approvals.decide(conn, row["id"], approved_by="owner")
    assert exc.value.code == "expired"


def test_request_is_idempotent_while_pending(conn):
    a = _request(conn)
    b = _request(conn)
    assert a["id"] == b["id"], "同一对象同动作的 pending 审批应复用"
    assert b["reused"] is True


def test_decision_note_preserved_after_consumption(conn):
    """消费不能覆盖人工填写的审批意见。"""
    row = _request(conn)
    approvals.decide(conn, row["id"], approved_by="owner", note="这个可以做")
    _consume(conn)
    got = dict(conn.execute("SELECT status, decision_note FROM approvals WHERE id = ?",
                            (row["id"],)).fetchone())
    assert got["status"] == "consumed"
    assert got["decision_note"] == "这个可以做"


def test_policy_requires_approval_when_rule_present(conn):
    """配了 requires_approval 规则后，decide() 必须返回 requires_approval=True。"""
    conn.execute(
        "INSERT INTO policy_rules (id, scope, resource_type, action, environment,"
        " max_risk, effect, requires_approval, enabled, created_at)"
        " VALUES ('r1', 'global', 'dispatch_item', 'run', '*', 'R3', 'allow', 1, 1,"
        " '2026-11-15T00:00:00+00:00')"
    )
    conn.commit()
    principal = governance.get_principal(conn, governance.LOCAL_OWNER)
    decision = policy.decide(
        conn, principal=principal, project_id="p1",
        resource_type="dispatch_item", action="run", risk="R3",
    )
    assert decision.allowed is True
    assert decision.requires_approval is True, "规则要求审批时必须返回 True"
    assert "r1" in decision.matched_rules


def test_policy_no_approval_without_rule(conn):
    """没有规则时不应要求审批（这也是原缺陷的成因）。"""
    principal = governance.get_principal(conn, governance.LOCAL_OWNER)
    decision = policy.decide(
        conn, principal=principal, project_id="p1",
        resource_type="dispatch_item", action="run", risk="R3",
    )
    assert decision.requires_approval is False
