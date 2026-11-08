"""价值口径与自主化资格：unknown 不填 0、verified/estimated 分开、资格不等于授权。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import store
from agent_platform.p4 import autonomy, governance, outcome, policy


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


def _metric(conn, key="cycle_time"):
    return outcome.define_metric(conn, "p1", {
        "metric_key": key, "name": "任务周期", "formula": "done_at - created_at",
        "unit": "hour",
    })


def test_metric_definition_and_version(conn):
    metric = _metric(conn)
    assert metric["metric_key"] == "cycle_time"
    row = conn.execute("SELECT * FROM outcome_metric_versions").fetchone()
    assert row["version"] == 1
    assert row["formula"] == "done_at - created_at"


def test_unknown_quality_cannot_carry_value(conn):
    _metric(conn)
    with pytest.raises(outcome.OutcomeError) as exc:
        outcome.record_value(conn, "p1", {
            "metric_key": "cycle_time", "quality": "unknown", "value": 0,
        })
    assert exc.value.code == "invalid_payload"


def test_summary_keeps_verified_and_estimated_apart(conn):
    _metric(conn)
    outcome.record_value(conn, "p1", {"metric_key": "cycle_time", "quality": "verified",
                                      "value": 10})
    outcome.record_value(conn, "p1", {"metric_key": "cycle_time", "quality": "verified",
                                      "value": 20})
    outcome.record_value(conn, "p1", {"metric_key": "cycle_time", "quality": "estimated",
                                      "value": 99})
    outcome.record_value(conn, "p1", {"metric_key": "cycle_time", "quality": "unknown"})

    summary = outcome.summarize(conn, "p1", "cycle_time")
    assert summary["verified_avg"] == 15
    assert summary["estimated_avg"] == 99
    assert summary["unknown_count"] == 1


def test_cost_summary_reads_usage_ledger(conn):
    conn.execute(
        "INSERT INTO token_usage_events (id, project_id, total_tokens, source, occurred_at)"
        " VALUES ('t1', 'p1', 100, 'transcript', '2026-11-06T00:00:00+00:00')")
    conn.commit()
    summary = outcome.cost_summary(conn, "p1")
    assert summary["events"] == 1
    assert summary["tokens"] == 100
    # 没有 cost 列数据 → quality 不能是 verified，也不能把成本写成 0
    assert summary["quality"] == "unknown"
    assert summary["known_cost_usd"] is None


def test_eligibility_starts_at_a0(conn):
    record = autonomy.get_or_create(conn, "p1", task_type="code_edit")
    assert record["level"] == "A0"
    assert record["status"] == "not_assessed"


def test_approve_requires_evidence(conn):
    record = autonomy.get_or_create(conn, "p1", task_type="code_edit")
    with pytest.raises(autonomy.AutonomyError) as exc:
        autonomy.approve_level(conn, record["id"], level="A2", approved_by="bob",
                               reason="看起来不错")
    assert exc.value.code == "invalid_state"


def test_production_capped_at_a2(conn):
    record = autonomy.get_or_create(conn, "p1", task_type="code_edit",
                                    environment="production")
    autonomy.submit_evidence(conn, record["id"], {"quality": 0.95, "safety": "clean"})
    with pytest.raises(autonomy.AutonomyError) as exc:
        autonomy.approve_level(conn, record["id"], level="A3", approved_by="bob",
                               reason="想开 A3")
    assert exc.value.code == "p0_level_cap"

    ok = autonomy.approve_level(conn, record["id"], level="A2", approved_by="bob",
                                reason="证据充分")
    assert ok["level"] == "A2" and ok["status"] == "active"


def test_policy_blocks_when_automation_insufficient(conn):
    owner = governance.get_principal(conn, governance.LOCAL_OWNER)
    decision = policy.decide(
        conn, principal=owner, project_id="p1", resource_type="execution", action="run",
        task_type="code_edit", required_automation="A2",
    )
    assert decision.allowed is False
    assert decision.blocked_by == "automation"

    record = autonomy.get_or_create(conn, "p1", task_type="code_edit")
    autonomy.submit_evidence(conn, record["id"], {"quality": 0.95})
    autonomy.approve_level(conn, record["id"], level="A2", approved_by="bob", reason="ok")

    decision2 = policy.decide(
        conn, principal=owner, project_id="p1", resource_type="execution", action="run",
        task_type="code_edit", required_automation="A2",
    )
    assert decision2.allowed is True


def test_suspend_drops_level_to_a0(conn):
    record = autonomy.get_or_create(conn, "p1", task_type="code_edit")
    autonomy.submit_evidence(conn, record["id"], {"quality": 0.9})
    autonomy.approve_level(conn, record["id"], level="A2", approved_by="bob", reason="ok")
    autonomy.suspend(conn, record["id"], reason="出现安全事件")
    assert autonomy.current_level(conn, "p1", "code_edit") == "A0"
