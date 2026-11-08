"""评估域 V1：版本不可变、安全不被平均分掩盖、未执行样本不计通过。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import store
from agent_platform.p4 import evaluation


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
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


def _suite_and_version(conn, cases):
    suite = evaluation.create_suite(conn, "p1", {"name": "回归"})
    dataset = evaluation.create_dataset(
        conn, "p1", {"name": "核心集", "suite_id": suite["id"], "split": "regression"}
    )
    version = evaluation.freeze_version(conn, dataset["id"], {"cases": cases})
    return suite, dataset, version


def _evaluator(conn, rules):
    return evaluation.create_evaluator(conn, "p1", {"name": "基础", "rules": rules})


def test_suite_dataset_version_immutable(conn):
    _suite, dataset, version = _suite_and_version(conn, [
        {"case_key": "c1", "expected": {"answer": "ok"}},
    ])
    assert version["version"] == 1
    assert version["case_count"] == 1
    assert version["status"] == "frozen"
    assert version["content_digest"]

    # 同一数据集再冻结一次 → 版本号递增，旧版本不受影响
    v2 = evaluation.freeze_version(conn, dataset["id"], {
        "cases": [{"case_key": "c1", "expected": {"answer": "ok"}},
                  {"case_key": "c2", "expected": {"answer": "fine"}}],
    })
    assert v2["version"] == 2
    assert evaluation.get_version(conn, version["id"])["case_count"] == 1


def test_duplicate_case_key_rejected(conn):
    suite = evaluation.create_suite(conn, "p1", {"name": "S"})
    dataset = evaluation.create_dataset(
        conn, "p1", {"name": "D", "suite_id": suite["id"]}
    )
    with pytest.raises(evaluation.EvaluationError):
        evaluation.freeze_version(conn, dataset["id"], {"cases": [
            {"case_key": "x"}, {"case_key": "x"},
        ]})


def test_run_all_pass(conn):
    _, _, version = _suite_and_version(conn, [
        {"case_key": "c1", "expected": {"answer": "hello"}},
        {"case_key": "c2", "expected": {"answer": "world"}},
    ])
    ev = _evaluator(conn, {"checks": [{"type": "contains", "field": "answer"}]})
    run = evaluation.run_evaluation(conn, "p1", {
        "dataset_version_id": version["id"],
        "evaluator_version_id": ev["version_id"],
        "candidate_outputs": {"c1": "say hello", "c2": "the world"},
    })
    assert run["status"] == "completed"
    assert run["decision"] == "pass"
    assert run["executed_cases"] == 2 and run["passed_cases"] == 2
    assert len(run["results"]) == 2


def test_missing_outputs_make_run_partial(conn):
    _, _, version = _suite_and_version(conn, [
        {"case_key": "c1", "expected": {"answer": "a"}},
        {"case_key": "c2", "expected": {"answer": "b"}},
    ])
    ev = _evaluator(conn, {"checks": [{"type": "contains", "field": "answer"}]})
    run = evaluation.run_evaluation(conn, "p1", {
        "dataset_version_id": version["id"],
        "evaluator_version_id": ev["version_id"],
        "candidate_outputs": {"c1": "a"},
    })
    assert run["status"] == "partial"
    assert run["decision"] == "needs_review"
    assert run["executed_cases"] == 1
    assert run["skipped_cases"] == 1
    # 跳过的不算通过
    assert run["passed_cases"] == 1


def test_safety_failure_cannot_be_masked_by_average(conn):
    _, _, version = _suite_and_version(conn, [
        {"case_key": "c1", "expected": {"answer": "ok"}},
    ])
    ev = _evaluator(conn, {
        "min_score": 0.5,
        "checks": [
            {"type": "contains", "field": "answer", "weight": 9},
            {"type": "not_contains", "value": "SECRET", "weight": 1, "safety": True},
        ],
    })
    run = evaluation.run_evaluation(conn, "p1", {
        "dataset_version_id": version["id"],
        "evaluator_version_id": ev["version_id"],
        "candidate_outputs": {"c1": "ok but leaked SECRET"},
    })
    # 平均分 0.9 > 0.5，但安全项失败 → 整轮 fail
    assert run["score"] == pytest.approx(0.9)
    assert run["safety_violations"] == 1
    assert run["decision"] == "fail"


def test_no_outputs_at_all_is_undetermined(conn):
    _, _, version = _suite_and_version(conn, [{"case_key": "c1"}])
    ev = _evaluator(conn, {"checks": [{"type": "contains", "value": "x"}]})
    run = evaluation.run_evaluation(conn, "p1", {
        "dataset_version_id": version["id"],
        "evaluator_version_id": ev["version_id"],
        "candidate_outputs": {},
    })
    assert run["status"] == "failed"
    assert run["decision"] == "undetermined"
    assert run["executed_cases"] == 0


def test_run_emits_event(conn):
    _, _, version = _suite_and_version(conn, [{"case_key": "c1"}])
    ev = _evaluator(conn, {"checks": [{"type": "contains", "value": "x"}]})
    run = evaluation.run_evaluation(conn, "p1", {
        "dataset_version_id": version["id"],
        "evaluator_version_id": ev["version_id"],
        "candidate_outputs": {"c1": "x"},
    })
    rows = conn.execute(
        "SELECT event_type, resource_id, correlation_id FROM event_outbox"
        " WHERE event_type = 'evaluation.run.completed'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["resource_id"] == run["id"]
    assert rows[0]["correlation_id"] == run["correlation_id"]


def test_dataset_of_other_project_rejected(conn):
    conn.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('p2', 'p2', '项目二', '/tmp/r2', '2026-11-06T00:00:00+00:00',"
        " '2026-11-06T00:00:00+00:00')"
    )
    conn.commit()
    _, _, version = _suite_and_version(conn, [{"case_key": "c1"}])
    ev = _evaluator(conn, {"checks": [{"type": "contains", "value": "x"}]})
    with pytest.raises(evaluation.EvaluationError) as exc:
        evaluation.run_evaluation(conn, "p2", {
            "dataset_version_id": version["id"],
            "evaluator_version_id": ev["version_id"],
            "candidate_outputs": {"c1": "x"},
        })
    assert exc.value.status == 403
