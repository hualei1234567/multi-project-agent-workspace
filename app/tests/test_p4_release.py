"""发布治理：评估门禁、职责分离、审批随 Manifest 失效、Canary 止损、回滚留痕。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import store
from agent_platform.p4 import evaluation, release


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


ITEMS = [
    {"artifact_type": "prompt", "artifact_ref": "planner", "artifact_version": "3",
     "digest": "sha-p3"},
    {"artifact_type": "model", "artifact_ref": "sonnet-5-5", "artifact_version": "2026-10",
     "digest": "sha-m1"},
]


def _release(conn, items=None):
    return release.create_release(conn, "p1", {
        "release_key": "rel-1", "name": "首个发布", "items": items or ITEMS,
    })


def _passing_eval_run(conn, decision="pass"):
    suite = evaluation.create_suite(conn, "p1", {"name": "S"})
    dataset = evaluation.create_dataset(conn, "p1", {"name": "D", "suite_id": suite["id"]})
    version = evaluation.freeze_version(conn, dataset["id"], {
        "cases": [{"case_key": "c1", "expected": {"answer": "ok"}}],
    })
    ev = evaluation.create_evaluator(conn, "p1", {
        "name": "E", "rules": {"checks": [{"type": "contains", "field": "answer"}]},
    })
    outputs = {"c1": "ok"} if decision == "pass" else {"c1": "wrong"}
    return evaluation.run_evaluation(conn, "p1", {
        "dataset_version_id": version["id"],
        "evaluator_version_id": ev["version_id"],
        "candidate_outputs": outputs,
    })


def test_create_and_validate(conn):
    created = _release(conn)
    assert created["status"] == "assembled"
    assert created["manifest_digest"]
    result = release.validate_release(conn, created["id"])
    assert result["valid"] is True
    assert release.get_release(conn, created["id"])["status"] == "validated"


def test_manifest_requires_prompt(conn):
    created = _release(conn, [ITEMS[1]])
    result = release.validate_release(conn, created["id"])
    assert result["valid"] is False
    assert any("prompt" in e for e in result["errors"])


def test_evaluation_gate_blocks_failed_run(conn):
    created = _release(conn)
    run = _passing_eval_run(conn, decision="fail")
    with pytest.raises(release.ReleaseError) as exc:
        release.attach_evaluation(conn, created["id"], run["id"])
    assert exc.value.code == "evaluation_gate_failed"


def test_evaluation_gate_accepts_pass(conn):
    created = _release(conn)
    run = _passing_eval_run(conn)
    result = release.attach_evaluation(conn, created["id"], run["id"])
    assert result["status"] == "evaluated"


def test_request_approval_requires_evaluation(conn):
    created = _release(conn)
    with pytest.raises(release.ReleaseError) as exc:
        release.request_approval(conn, created["id"], requested_by="alice")
    assert exc.value.code == "evaluation_required"


def test_separation_of_duties(conn):
    created = _release(conn)
    run = _passing_eval_run(conn)
    release.attach_evaluation(conn, created["id"], run["id"])
    approval = release.request_approval(conn, created["id"], requested_by="alice")
    with pytest.raises(release.ReleaseError) as exc:
        release.approve(conn, approval["approval_id"], approved_by="alice")
    assert exc.value.code == "separation_of_duties"


def test_manifest_change_invalidates_approval(conn):
    created = _release(conn)
    run = _passing_eval_run(conn)
    release.attach_evaluation(conn, created["id"], run["id"])
    approval = release.request_approval(conn, created["id"], requested_by="alice")

    # 审批后偷偷换掉模型版本
    conn.execute(
        "UPDATE release_manifest_items SET artifact_version = '2026-11', digest = 'sha-m2'"
        " WHERE release_id = ? AND artifact_type = 'model'", (created["id"],),
    )
    conn.commit()
    with pytest.raises(release.ReleaseError) as exc:
        release.approve(conn, approval["approval_id"], approved_by="bob")
    assert exc.value.code == "approval_invalidated"
    row = conn.execute("SELECT status FROM approvals WHERE id = ?",
                       (approval["approval_id"],)).fetchone()
    assert row["status"] == "expired"


def test_full_happy_path_to_published(conn):
    created = _release(conn)
    release.validate_release(conn, created["id"])
    run = _passing_eval_run(conn)
    release.attach_evaluation(conn, created["id"], run["id"])
    approval = release.request_approval(conn, created["id"], requested_by="alice")
    release.approve(conn, approval["approval_id"], approved_by="bob")
    result = release.publish(conn, created["id"])
    assert result["status"] == "published"


def test_canary_stops_on_threshold_breach(conn):
    created = _release(conn)
    release.validate_release(conn, created["id"])
    run = _passing_eval_run(conn)
    release.attach_evaluation(conn, created["id"], run["id"])
    approval = release.request_approval(conn, created["id"], requested_by="alice")
    release.approve(conn, approval["approval_id"], approved_by="bob")
    release.publish(conn, created["id"])

    canary = release.start_canary(
        conn, created["id"], traffic_pct=10, stop_threshold={"error_rate": 0.05}
    )
    ok = release.observe_canary(conn, canary["id"], {"error_rate": 0.01})
    assert ok["status"] == "canary" and ok["breaches"] == []

    breach = release.observe_canary(conn, canary["id"], {"error_rate": 0.20})
    assert breach["status"] == "stopped"
    assert breach["breaches"]


def test_rollback_keeps_history(conn):
    created = _release(conn)
    release.validate_release(conn, created["id"])
    run = _passing_eval_run(conn)
    release.attach_evaluation(conn, created["id"], run["id"])
    approval = release.request_approval(conn, created["id"], requested_by="alice")
    release.approve(conn, approval["approval_id"], approved_by="bob")
    release.publish(conn, created["id"])
    canary = release.start_canary(conn, created["id"], traffic_pct=5)

    result = release.rollback(conn, created["id"], reason="线上指标劣化",
                              deployment_id=canary["id"])
    assert result["status"] == "rolled_back"
    assert result["restored_digest"] == created["manifest_digest"]

    after = release.get_release(conn, created["id"])
    assert after["status"] == "deprecated"
    assert after["manifest_digest"] == created["manifest_digest"]  # 历史未被改写
    assert release.get_canary(conn, canary["id"])["status"] == "rolled_back"
