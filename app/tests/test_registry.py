"""资源注册中心测试（P3-M2）。

契约：``docs/phase3/contracts/CF-2_REGISTRY_SCHEMA_v1.md``

锁定六条硬约束：
1. 撤销不删行
2. 无 evidence 的能力评分打折
3. 未扫描的 Skill 不能派发
4. 兼容矩阵无记录默认兼容
5. 运行时心跳超时 → offline（读取时计算）
6. 所有变更写 registry_changes
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import registry, store


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    c = store.connect()
    store.init_db(c)
    yield c
    c.close()


# ---- 角色 ----
def test_create_role_basic(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA",
                                execution_mode="worktree")
    assert role["id"] == "role_qa"
    assert role["status"] == "active"
    assert role["version"] == 1


def test_duplicate_role_rejected(conn):
    registry.create_role(conn, role_key="qa", display_name="QA")
    with pytest.raises(registry.RegistryError) as exc:
        registry.create_role(conn, role_key="qa", display_name="QA2")
    assert exc.value.code == "role_exists"


def test_invalid_execution_mode_rejected(conn):
    with pytest.raises(registry.RegistryError) as exc:
        registry.create_role(conn, role_key="x", display_name="X",
                             execution_mode="bogus")
    assert exc.value.code == "invalid_execution_mode"


def test_main_session_mode_allowed(conn):
    """main_session 是生产库真实存在的模式，不能被枚举漏掉。"""
    role = registry.create_role(conn, role_key="owner", display_name="Owner",
                                execution_mode="main_session")
    assert role["execution_mode"] == "main_session"


# ---- 能力与评分 ----
def test_capability_with_evidence_full_score(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA")
    registry.add_capability(conn, role_id=role["id"], capability="test-writing",
                            proficiency=4, evidence="写了 40+ 用例")
    assert registry.capability_score(conn, role["id"], "test-writing") == 4.0


def test_capability_without_evidence_discounted(conn):
    """没有依据的能力不禁止登记，但评分打 5 折——诚实优于完美。"""
    role = registry.create_role(conn, role_key="qa", display_name="QA")
    registry.add_capability(conn, role_id=role["id"], capability="review",
                            proficiency=4, evidence=None)
    assert registry.capability_score(conn, role["id"], "review") == 2.0


def test_capability_absent_scores_zero(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA")
    assert registry.capability_score(conn, role["id"], "deploy") == 0.0


def test_proficiency_out_of_range_rejected(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA")
    with pytest.raises(registry.RegistryError):
        registry.add_capability(conn, role_id=role["id"], capability="x",
                                proficiency=9)


# ---- 技能扫描 ----
def test_new_skill_starts_unscanned(conn):
    skill = registry.create_skill(conn, skill_key="runner", display_name="Runner")
    assert skill["scan_status"] == "unscanned"


def test_unscanned_skill_blocks_dispatch(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA",
                                execution_mode="worktree")
    skill = registry.create_skill(conn, skill_key="runner", display_name="Runner")
    registry.grant_skill(conn, skill_id=skill["id"], role_id=role["id"])
    result = registry.dispatch_precheck(conn, role_id=role["id"],
                                        skill_ids=[skill["id"]])
    assert result["allowed"] is False
    assert any("skill_not_scanned" in b for b in result["blockers"])


def test_failed_scan_still_blocks(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA",
                                execution_mode="worktree")
    skill = registry.create_skill(conn, skill_key="runner", display_name="Runner")
    registry.grant_skill(conn, skill_id=skill["id"], role_id=role["id"])
    registry.record_scan(conn, skill["id"], passed=False, detail="发现危险调用")
    result = registry.dispatch_precheck(conn, role_id=role["id"],
                                        skill_ids=[skill["id"]])
    assert result["allowed"] is False


def test_passed_and_granted_skill_allows(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA",
                                execution_mode="worktree")
    skill = registry.create_skill(conn, skill_key="runner", display_name="Runner")
    registry.record_scan(conn, skill["id"], passed=True)
    registry.grant_skill(conn, skill_id=skill["id"], role_id=role["id"])
    result = registry.dispatch_precheck(conn, role_id=role["id"],
                                        skill_ids=[skill["id"]])
    assert result["allowed"] is True, result["blockers"]


def test_ungranted_skill_blocks(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA",
                                execution_mode="worktree")
    skill = registry.create_skill(conn, skill_key="runner", display_name="Runner")
    registry.record_scan(conn, skill["id"], passed=True)
    result = registry.dispatch_precheck(conn, role_id=role["id"],
                                        skill_ids=[skill["id"]])
    assert result["allowed"] is False
    assert any("skill_not_granted" in b for b in result["blockers"])


def test_revoked_grant_blocks(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA",
                                execution_mode="worktree")
    skill = registry.create_skill(conn, skill_key="runner", display_name="Runner")
    registry.record_scan(conn, skill["id"], passed=True)
    grant = registry.grant_skill(conn, skill_id=skill["id"], role_id=role["id"])
    registry.revoke_skill_grant(conn, grant["id"], reason="测试撤销")
    result = registry.dispatch_precheck(conn, role_id=role["id"],
                                        skill_ids=[skill["id"]])
    assert result["allowed"] is False


# ---- 撤销不删行 ----
def test_revoke_keeps_row(conn):
    skill = registry.create_skill(conn, skill_key="runner", display_name="Runner")
    role = registry.create_role(conn, role_key="qa", display_name="QA")
    grant = registry.grant_skill(conn, skill_id=skill["id"], role_id=role["id"])
    registry.revoke_skill_grant(conn, grant["id"], reason="不用了")
    row = conn.execute("SELECT * FROM registry_skill_grants WHERE id = ?",
                       (grant["id"],)).fetchone()
    assert row is not None, "撤销必须保留行，供审计回溯"
    assert row["revoked_at"] is not None


def test_readonly_role_cannot_execute(conn):
    role = registry.create_role(conn, role_key="arch", display_name="架构",
                                execution_mode="readonly")
    result = registry.dispatch_precheck(conn, role_id=role["id"])
    assert result["allowed"] is False
    assert "role_readonly_cannot_execute" in result["blockers"]


def test_revoked_role_blocks(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA",
                                execution_mode="worktree")
    conn.execute("UPDATE registry_agent_roles SET status='revoked' WHERE id=?",
                 (role["id"],))
    conn.commit()
    result = registry.dispatch_precheck(conn, role_id=role["id"])
    assert result["allowed"] is False
    assert "role_revoked" in result["blockers"]


# ---- 模型 ----
def test_create_model(conn):
    m = registry.create_model(conn, model_key="deepseek-v4", provider="deepseek",
                              context_window=128000)
    assert m["context_window"] == 128000
    assert m["supports_tools"] == 1


def test_duplicate_model_rejected(conn):
    registry.create_model(conn, model_key="m1", provider="p")
    with pytest.raises(registry.RegistryError) as exc:
        registry.create_model(conn, model_key="m1", provider="p")
    assert exc.value.code == "model_exists"


# ---- 运行时 ----
def test_runtime_starts_unknown(conn):
    rt = registry.create_runtime(conn, runtime_key="mac-local")
    assert rt["effective_status"] == "unknown"


def test_heartbeat_makes_healthy(conn):
    rt = registry.create_runtime(conn, runtime_key="mac-local")
    rt = registry.heartbeat(conn, rt["id"])
    assert rt["effective_status"] == "healthy"


def test_stale_heartbeat_computes_offline(conn):
    """心跳超时由读取时计算，不靠采集线程改状态——线程挂了也不会骗人。"""
    rt = registry.create_runtime(conn, runtime_key="mac-local", heartbeat_ttl_s=1)
    registry.heartbeat(conn, rt["id"])
    conn.execute("UPDATE registry_runtimes SET heartbeat_at = ? WHERE id = ?",
                 ("2020-01-01T00:00:00+00:00", rt["id"]))
    conn.commit()
    assert registry.get_runtime(conn, rt["id"])["effective_status"] == "offline"


def test_revoked_runtime_blocks_dispatch(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA",
                                execution_mode="worktree")
    rt = registry.create_runtime(conn, runtime_key="mac-local")
    registry.heartbeat(conn, rt["id"])
    registry.revoke_runtime(conn, rt["id"], reason="设备丢失")
    result = registry.dispatch_precheck(conn, role_id=role["id"], runtime_id=rt["id"])
    assert result["allowed"] is False
    assert "runtime_revoked" in result["blockers"]


def test_lease_concurrency_limit(conn):
    rt = registry.create_runtime(conn, runtime_key="mac", max_concurrency=1)
    registry.heartbeat(conn, rt["id"])
    registry.acquire_lease(conn, runtime_id=rt["id"], project_id="p1", holder="a")
    with pytest.raises(registry.RegistryError) as exc:
        registry.acquire_lease(conn, runtime_id=rt["id"], project_id="p1", holder="b")
    assert exc.value.code == "concurrency_exceeded"


def test_release_lease_frees_slot(conn):
    rt = registry.create_runtime(conn, runtime_key="mac", max_concurrency=1)
    registry.heartbeat(conn, rt["id"])
    lease = registry.acquire_lease(conn, runtime_id=rt["id"], project_id="p1",
                                   holder="a")
    registry.release_lease(conn, lease["id"])
    # 释放后应能再租
    registry.acquire_lease(conn, runtime_id=rt["id"], project_id="p1", holder="b")


# ---- 兼容矩阵 ----
def test_no_record_means_compatible(conn):
    """显式宽松决策：没有记录视为兼容，否则新资源无法使用。"""
    ok, why = registry.is_compatible(conn, subject_type="agent_role", subject_id="r1",
                                     target_type="skill", target_id="s1")
    assert ok is True
    assert "no_record" in why


def test_explicit_incompatible(conn):
    registry.set_compatibility(conn, subject_type="agent_role", subject_id="r1",
                               target_type="skill", target_id="s1",
                               compatible=False, reason="技能需要 GUI，服务器无显示")
    ok, why = registry.is_compatible(conn, subject_type="agent_role", subject_id="r1",
                                     target_type="skill", target_id="s1")
    assert ok is False
    assert "GUI" in why


def test_incompatible_blocks_dispatch(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA",
                                execution_mode="worktree")
    skill = registry.create_skill(conn, skill_key="runner", display_name="Runner")
    registry.record_scan(conn, skill["id"], passed=True)
    registry.grant_skill(conn, skill_id=skill["id"], role_id=role["id"])
    registry.set_compatibility(conn, subject_type="agent_role", subject_id=role["id"],
                               target_type="skill", target_id=skill["id"],
                               compatible=False, reason="不兼容")
    result = registry.dispatch_precheck(conn, role_id=role["id"],
                                        skill_ids=[skill["id"]])
    assert result["allowed"] is False
    assert any("incompatible_skill" in b for b in result["blockers"])


# ---- 变更审计 ----
def test_changes_recorded(conn):
    role = registry.create_role(conn, role_key="qa", display_name="QA",
                                created_by="tester")
    rows = conn.execute("SELECT * FROM registry_changes WHERE entity_id = ?",
                        (role["id"],)).fetchall()
    assert len(rows) >= 1
    assert rows[0]["changed_by"] == "tester"
    assert rows[0]["action"] == "create"


def test_scan_recorded_as_change(conn):
    skill = registry.create_skill(conn, skill_key="runner", display_name="Runner")
    registry.record_scan(conn, skill["id"], passed=True, changed_by="scanner")
    rows = conn.execute(
        "SELECT * FROM registry_changes WHERE entity_id = ? AND action='scan'",
        (skill["id"],)).fetchall()
    assert len(rows) == 1
    assert rows[0]["changed_by"] == "scanner"


# ---- 汇总 ----
def test_summary_counts(conn):
    registry.create_role(conn, role_key="qa", display_name="QA",
                         execution_mode="worktree")
    registry.create_skill(conn, skill_key="runner", display_name="Runner")
    s = registry.summary(conn)
    assert s["roles"]["total"] == 1
    assert s["roles"]["executable"] == 1
    assert s["skills"]["unscanned"] == 1
    assert s["attention"]["roles_without_capabilities"] == 1


# ---- 与调度器接线（P3-M2 关键：建了不用等于没建）----
def test_revoked_role_excluded_from_scheduling(conn):
    """Registry 里 revoked 的角色，其任务必须从调度候选中消失。"""
    from agent_platform import scheduler

    conn.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('p1','p1','项目一','/tmp/r','2026-11-15T00:00:00+00:00',"
        " '2026-11-15T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO project_agents (id, project_id, role_key, display_name,"
        " instance_key, agent_type, execution_mode)"
        " VALUES ('a1','p1','qa','QA','a1','claude','worktree')")
    conn.execute(
        "INSERT INTO tasks (id, project_id, task_key, title, status, priority,"
        " assignee_agent_id, source, updated_at, version)"
        " VALUES ('t1','p1','t1','任务','todo','P2','a1','platform',"
        " '2026-11-15T00:00:00+00:00',1)")
    conn.commit()

    ready, _ = scheduler.schedulable(conn, "p1")
    assert [t["task_key"] for t in ready] == ["t1"], "未登记 Registry 时不应拦截"

    registry.create_role(conn, role_key="qa", display_name="QA",
                         execution_mode="worktree")
    ready, _ = scheduler.schedulable(conn, "p1")
    assert [t["task_key"] for t in ready] == ["t1"], "active 角色仍可调度"

    conn.execute("UPDATE registry_agent_roles SET status='revoked' WHERE role_key='qa'")
    conn.commit()
    ready, skipped = scheduler.schedulable(conn, "p1")
    assert ready == [], "撤销后必须从候选剔除"
    assert any("Registry" in s["reason"] for s in skipped)
