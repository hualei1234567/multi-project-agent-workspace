"""改进闭环：失败聚合、根因需证据、沙箱不通过不得审批、职责分离。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import store
from agent_platform.p4 import evaluation, improvement


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(tmp_path / "platform.db"))
    conn = store.connect()
    store.init_db(conn)
    conn.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('p1', 'p1', '项目一', '/tmp/repo', '2026-11-06T00:00:00+00:00',"
        " '2026-11-06T00:00:00+00:00')"
    )
    conn.commit()
    yield conn
    conn.close()


def _eval_run(conn, decision="pass"):
    suite = evaluation.create_suite(conn, "p1", {"name": "S"})
    ds = evaluation.create_dataset(conn, "p1", {"name": "D", "suite_id": suite["id"]})
    ver = evaluation.freeze_version(conn, ds["id"], {
        "cases": [{"case_key": "c1", "expected": {"answer": "ok"}}]})
    ev = evaluation.create_evaluator(conn, "p1", {
        "name": "E", "rules": {"checks": [{"type": "contains", "field": "answer"}]}})
    return evaluation.run_evaluation(conn, "p1", {
        "dataset_version_id": ver["id"], "evaluator_version_id": ev["version_id"],
        "candidate_outputs": {"c1": "ok" if decision == "pass" else "bad"}})


def _case(conn):
    return improvement.open_case(conn, "p1", {"title": "回答不稳定"})


def test_failure_patterns_aggregate(conn):
    a = improvement.record_failure(conn, "p1", {"pattern_key": "empty-answer",
                                               "summary": "空回答"})
    b = improvement.record_failure(conn, "p1", {"pattern_key": "empty-answer",
                                               "summary": "空回答"})
    assert a["id"] == b["id"]
    assert b["occurrences"] == 2


def test_root_cause_requires_evidence(conn):
    case = _case(conn)
    with pytest.raises(improvement.ImprovementError) as exc:
        improvement.assess_root_cause(conn, case["id"], {"root_cause": "提示词缺约束"})
    assert exc.value.code == "evidence_required"

    ok = improvement.assess_root_cause(conn, case["id"], {
        "root_cause": "提示词缺少输出格式约束",
        "evidence": {"eval_run": "run_x", "failed_cases": ["c1"]},
    })
    assert ok["status"] == "proposal_ready"


def test_proposal_requires_root_cause(conn):
    case = _case(conn)
    with pytest.raises(improvement.ImprovementError) as exc:
        improvement.create_proposal(conn, "p1", {
            "case_id": case["id"], "title": "改提示词",
            "base_version": "v1", "candidate_version": "v2", "rollback_plan": "回滚到 v1",
        })
    assert exc.value.code == "root_cause_required"


def _proposal(conn):
    case = _case(conn)
    improvement.assess_root_cause(conn, case["id"], {
        "root_cause": "缺输出约束", "evidence": {"case": "c1"}})
    return improvement.create_proposal(conn, "p1", {
        "case_id": case["id"], "title": "加输出约束",
        "base_version": "p1", "candidate_version": "p2", "rollback_plan": "回滚到 p1",
    })


def test_failed_sandbox_rejects_proposal(conn):
    proposal = _proposal(conn)
    run = _eval_run(conn, decision="fail")
    result = improvement.run_sandbox(conn, proposal["id"], run["id"])
    assert result["status"] == "failed"
    assert improvement.get_proposal(conn, proposal["id"])["status"] == "rejected"
    with pytest.raises(improvement.ImprovementError) as exc:
        improvement.request_approval(conn, proposal["id"], requested_by="alice")
    assert exc.value.code == "sandbox_required"


def test_full_improvement_loop(conn):
    proposal = _proposal(conn)
    run = _eval_run(conn)
    assert improvement.run_sandbox(conn, proposal["id"], run["id"])["status"] == "passed"

    approval = improvement.request_approval(conn, proposal["id"], requested_by="alice")
    with pytest.raises(improvement.ImprovementError) as exc:
        improvement.approve(conn, approval["approval_id"], approved_by="alice")
    assert exc.value.code == "separation_of_duties"

    improvement.approve(conn, approval["approval_id"], approved_by="bob")
    assert improvement.get_proposal(conn, proposal["id"])["status"] == "approved"

    case_id = improvement.get_proposal(conn, proposal["id"])["case_id"]
    assert improvement.get_case(conn, case_id)["status"] == "verified"


def test_cross_project_run_rejected(conn):
    conn.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('p2', 'p2', 'p2', '/tmp/r2', '2026-11-06T00:00:00+00:00',"
        " '2026-11-06T00:00:00+00:00')")
    conn.commit()
    proposal = _proposal(conn)
    run = _eval_run(conn)
    conn.execute("UPDATE eval_runs SET project_id = 'p2' WHERE id = ?", (run["id"],))
    conn.commit()
    with pytest.raises(improvement.ImprovementError) as exc:
        improvement.run_sandbox(conn, proposal["id"], run["id"])
    assert exc.value.code == "forbidden"
