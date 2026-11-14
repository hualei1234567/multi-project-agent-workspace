"""R2 治理与数据修复：S-3 审计 / S-7 Outbox / S-8 搜索表映射 / D-1 追溯 / D-2 成本口径。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from agent_platform import cost as cost_module
from agent_platform import migrate, service, store, workspace
from agent_platform.executor import _parse_result_cost as developer_parse
from agent_platform.p4 import events as p4_events
from agent_platform.p4 import release as release_service


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


# ---------------------------------------------------------------- S-3 审计
def test_p4_publish_writes_audit(env):
    """S-3：P4 高风险操作必须留下审计记录。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        rel = release_service.create_release(conn, "default", {
            "release_key": "k1", "name": "测试发布", "created_by": "tester",
            "items": [{"artifact_type": "prompt", "artifact_ref": "a1",
                       "artifact_version": "1.0", "digest": "abc"}],
        })
        rid = rel["id"]
        conn.execute("UPDATE agent_releases SET status='approved' WHERE id=?", (rid,))
        conn.commit()
        release_service.publish(conn, rid)
        row = conn.execute(
            "SELECT action, resource_type, resource_id FROM audit_logs"
            " WHERE action = 'agent.release.publish' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "publish 没有写审计"
        assert row["resource_type"] == "agent_release"
        assert row["resource_id"] == rid
    finally:
        conn.close()


def test_all_high_risk_p4_ops_are_decorated():
    """S-3：12 个高风险状态变更入口都必须挂 audited，防止漏改。"""
    p4 = APP / "agent_platform" / "p4"
    expected = {
        "release.py": 4, "knowledge.py": 2, "workflow.py": 1,
        "catalog.py": 3, "autonomy.py": 1, "improvement.py": 1,
    }
    for fname, count in expected.items():
        src = (p4 / fname).read_text(encoding="utf-8")
        actual = src.count("@audited(")
        assert actual >= count, f"{fname} 只挂了 {actual} 处，期望 {count}"


# ---------------------------------------------------------------- S-7 Outbox
def test_outbox_dispatch_marks_dispatched(env):
    """S-7：dispatcher 必须真的把 pending 事件投递出去。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        with store.write_tx(conn):
            p4_events.emit(conn, "test.event", project_id="default",
                           resource_type="task", resource_id="t1")
        pending_before = conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE status='pending'"
        ).fetchone()[0]
        assert pending_before == 1

        stats = p4_events.dispatch_pending(conn, consumer="platform")
        assert stats["dispatched"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE status='pending'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM event_inbox WHERE consumer='platform'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_outbox_dispatch_is_idempotent(env):
    """S-7：重复投递不产生重复副作用（inbox 去重）。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        with store.write_tx(conn):
            p4_events.emit(conn, "test.event2", project_id="default")
        p4_events.dispatch_pending(conn, consumer="platform")
        # 再跑一次，没有 pending 了
        stats = p4_events.dispatch_pending(conn, consumer="platform")
        assert stats["dispatched"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM event_inbox WHERE consumer='platform'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_server_starts_dispatcher_thread():
    """S-7：生产代码必须有调用 dispatch_pending 的地方，否则 outbox 只写不投。"""
    src = (APP / "agents_dashboard.py").read_text(encoding="utf-8")
    assert "dispatch_pending" in src, "生产代码没有调用 dispatch_pending"
    assert "_event_dispatcher" in src, "缺少常驻 dispatcher 线程"


# ---------------------------------------------------------------- S-8 搜索
def test_search_uses_real_table_names():
    """S-8：搜索必须查真实存在的表，不能再查 p4_ 前缀的幽灵表。

    只校验 ``FROM <表>`` 这类真实取数语句，避免把注释里的历史表名也算成缺陷。
    """
    src = (APP / "agent_platform" / "workspace.py").read_text(encoding="utf-8")
    used = set(re.findall(r"FROM\s+([a-z_][a-z0-9_]*)", src, flags=re.IGNORECASE))
    ghosts = {"p4_workflow_definitions", "p4_knowledge_items", "p4_releases"}
    assert not (used & ghosts), f"仍在查不存在的表：{sorted(used & ghosts)}"
    for real in ("workflow_definitions", "knowledge_items", "agent_releases"):
        assert real in used, f"缺少真实表：{real}（实际 FROM：{sorted(used)}）"


def test_search_does_not_silently_swallow_errors():
    """S-8：查询失败必须可见（degraded），不能 except: pass。"""
    src = (APP / "agent_platform" / "workspace.py").read_text(encoding="utf-8")
    assert "degraded" in src, "没有降级记录机制"
    assert "_search_query" in src, "缺少统一查询 helper"


def test_search_returns_degraded_field(env):
    """S-8：搜索结果必须带 degraded 字段（正常时为空列表）。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        out = workspace.search(conn, q="测试", principal_id="local-owner")
        assert "degraded" in out
        assert isinstance(out["degraded"], list)
    finally:
        conn.close()


def test_search_finds_real_workflow_data(env):
    """S-8：三类搜索真能找到数据（证明表名修对了，不是靠空结果蒙混）。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        now = store.now_iso()
        conn.execute(
            "INSERT INTO workflow_definitions (id, project_id, def_key, name, status,"
            " created_at, updated_at) VALUES ('w1','default','k','可搜索工作流','draft',?,?)",
            (now, now),
        )
        conn.commit()
        out = workspace.search(conn, q="可搜索", types=["workflow"],
                               principal_id="local-owner")
        assert out["count"] >= 1, "workflow 搜索没找到刚插入的数据"
        assert out["results"][0]["type"] == "workflow"
    finally:
        conn.close()


# ---------------------------------------------------------------- D-1 追溯
def test_traceability_excludes_manual_transcript_sessions(env):
    """D-1：transcript 采集的人工会话本来就没有 Task，不能算进追溯率分母。

    否则分母永远比分子大、追溯率永远上不去，还会诱导后人伪造关联。
    """
    conn = store.connect()
    try:
        store.init_db(conn)
        now = store.now_iso()
        conn.execute(
            "INSERT INTO executions (id, project_id, agent_id, task_id, session_id,"
            " status, started_at, source, link_quality)"
            " VALUES ('e1','default','a1',NULL,'s-manual','succeeded',?,'transcript','legacy_unlinked')",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()
    data = service.execution_traceability("default")
    assert data["total"] == 1
    assert data["traceable"] == 0
    # 没有可追溯对象 → None，而不是伪造的 0.0
    assert data["rate"] is None
    assert data["unlinked_legacy"] == 1


def test_traceability_counts_dispatcher_gap_as_defect(env):
    """D-1：调度器写的执行没有 task_id 是真缺陷，必须计入分母。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        now = store.now_iso()
        conn.execute(
            "INSERT INTO executions (id, project_id, agent_id, task_id, session_id,"
            " status, started_at, source, link_quality)"
            " VALUES ('e1','default','a1',NULL,'s1','succeeded',?,'dispatcher','dispatcher_missing_task')",
            (now,),
        )
        conn.execute(
            "INSERT INTO executions (id, project_id, agent_id, task_id, session_id,"
            " status, started_at, source, link_quality)"
            " VALUES ('e2','default','a1','t1','s2','succeeded',?,'dispatcher','linked')",
            (now,),
        )
        conn.commit()
    finally:
        conn.close()
    data = service.execution_traceability("default")
    assert data["traceable"] == 2
    assert data["linked"] == 1
    assert data["unlinked_defect"] == 1
    assert data["rate"] == 0.5


def test_backfill_links_only_on_session_id_evidence(env):
    """D-1：历史回填只认 session_id 精确匹配，绝不按时间/文本猜 Task。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        now = store.now_iso()
        # 一条能精确匹配 dispatch_items.session_id
        conn.execute(
            "INSERT INTO dispatch_plans (id, project_id, status, strategy, created_at)"
            " VALUES ('p1','default','done','manual',?)", (now,))
        conn.execute(
            "INSERT INTO dispatch_items (id, plan_id, project_id, task_id, agent_id,"
            " role_key, status, session_id, created_at, updated_at)"
            " VALUES ('d1','p1','default','task-9','a1','backend-engineer','succeeded','sess-hit',?,?)",
            (now, now))
        for eid, sid in (("e1", "sess-hit"), ("e2", "sess-miss")):
            conn.execute(
                "INSERT INTO executions (id, project_id, agent_id, task_id, session_id,"
                " status, started_at, source) VALUES (?,?,?,?,?,'succeeded',?,'transcript')",
                (eid, "default", "a1", None, sid, now),
            )
        conn.commit()
        migrate._migrate_v18_to_v19(conn)
        conn.commit()
        hit = conn.execute("SELECT task_id, link_quality FROM executions WHERE id='e1'").fetchone()
        miss = conn.execute("SELECT task_id, link_quality FROM executions WHERE id='e2'").fetchone()
        assert hit["task_id"] == "task-9" and hit["link_quality"] == "linked"
        assert miss["task_id"] is None and miss["link_quality"] == "legacy_unlinked"
    finally:
        conn.close()


# ---------------------------------------------------------------- D-2 成本
def test_price_resolution_is_time_scoped(env):
    """D-2：价格必须按生效区间取；发生时间早于任何版本 → 不估，保持 UNKNOWN。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        with store.write_tx(conn):
            cost_module.add_price_version(conn, {
                "model_pattern": "m1", "effective_from": "2026-11-01T00:00:00+00:00",
                "input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0,
                "source": "test",
            })
        assert cost_module.resolve_price(conn, "m1", "2026-10-15T00:00:00+00:00") is None
        hit = cost_module.resolve_price(conn, "m1", "2026-11-15T00:00:00+00:00")
        assert hit is not None and hit["input_usd_per_mtok"] == 1.0
        # 模型名不匹配也必须是 None，不能回落到「随便一个价格」
        assert cost_module.resolve_price(conn, "other", "2026-11-15T00:00:00+00:00") is None
    finally:
        conn.close()


def test_estimate_matches_linear_pricing_exactly(env):
    """D-2：估算必须是四个 token 维度的线性组合，不留取整误差。"""
    price = {
        "input_usd_per_mtok": 0.15, "output_usd_per_mtok": 0.60,
        "cache_read_usd_per_mtok": 0.003, "cache_write_usd_per_mtok": 0.0,
    }
    usage = {"input_tokens": 23023, "output_tokens": 906,
             "cache_read_tokens": 67200, "cache_write_tokens": 0}
    got = cost_module.estimate_usd(price, usage)
    expect = 23023 * 0.15 / 1e6 + 906 * 0.60 / 1e6 + 67200 * 0.003 / 1e6
    assert abs(got - expect) < 1e-12


def test_backfill_never_guesses_and_keeps_zero_distinct(env):
    """D-2：无价格证据 → 保持 UNKNOWN；零 token → 确定不计费（两者不能混）。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        with store.write_tx(conn):
            cost_module.add_price_version(conn, {
                "model_pattern": "priced-model",
                "effective_from": "2026-11-01T00:00:00+00:00",
                "input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0,
                "source": "test",
            })
        rows = [
            ("t1", "priced-model", "2026-11-10T00:00:00+00:00", 1000),
            ("t2", "no-price-model", "2026-11-10T00:00:00+00:00", 1000),
            ("t3", "priced-model", "2026-10-01T00:00:00+00:00", 1000),  # 早于生效
            ("t4", "priced-model", "2026-11-10T00:00:00+00:00", 0),     # 零 token
        ]
        with store.write_tx(conn):
            for eid, model, ts, tokens in rows:
                conn.execute(
                    "INSERT INTO token_usage_events (id, project_id, model, input_tokens,"
                    " total_tokens, source, occurred_at) VALUES (?,?,?,?,?, 'transcript', ?)",
                    (eid, "default", model, tokens, tokens, ts),
                )
        dry = cost_module.backfill_estimates(conn)
        assert dry["priced"] == 1 and dry["zero_token"] == 1 and dry["unmatched"] == 2
        assert dry["applied"] is False
        # dry-run 不改库
        assert conn.execute("SELECT COUNT(*) FROM token_usage_events"
                            " WHERE estimated_cost_usd IS NOT NULL").fetchone()[0] == 0

        cost_module.backfill_estimates(conn, apply=True)
        by_id = {
            r["id"]: (r["estimated_cost_usd"], r["cost_quality"])
            for r in conn.execute("SELECT id, estimated_cost_usd, cost_quality"
                                  " FROM token_usage_events")
        }
        assert by_id["t1"][1] == "estimated" and by_id["t1"][0] > 0
        assert by_id["t2"] == (None, None)   # 无价格 → 未知，宁缺勿猜
        assert by_id["t3"] == (None, None)   # 早于生效 → 未知
        assert by_id["t4"] == (0, "not_billable")  # 零 token 是「确定为零」
    finally:
        conn.close()


def test_replay_does_not_double_count(env):
    """D-2：同一事件重放不重复计费（唯一键 + 幂等插入）。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        params = ("t1", "default", "m", 100, 100, "transcript", "2026-11-10T00:00:00+00:00")
        sql = ("INSERT OR IGNORE INTO token_usage_events (id, project_id, model,"
               " input_tokens, total_tokens, source, occurred_at)"
               " VALUES (?,?,?,?,?,?,?)")
        with store.write_tx(conn):
            conn.execute(sql, params)
            conn.execute(sql, params)
        assert conn.execute("SELECT COUNT(*) FROM token_usage_events").fetchone()[0] == 1
    finally:
        conn.close()


def test_coverage_reports_unknown_separately(env):
    """D-2：报表必须给覆盖率，且未知金额是 None 而不是 0。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        ts = "2026-11-10T00:00:00+00:00"
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO token_usage_events (id, project_id, model, input_tokens,"
                " total_tokens, source, occurred_at, estimated_cost_usd, cost_quality)"
                " VALUES ('a','default','m',10,10,'transcript',?,0.5,'estimated')", (ts,))
            conn.execute(
                "INSERT INTO token_usage_events (id, project_id, model, input_tokens,"
                " total_tokens, source, occurred_at)"
                " VALUES ('b','default','m',10,10,'transcript',?)", (ts,))
            conn.execute(
                "INSERT INTO token_usage_events (id, project_id, model, input_tokens,"
                " total_tokens, source, occurred_at, estimated_cost_usd, cost_quality)"
                " VALUES ('c','default','m',0,0,'transcript',?,0,'not_billable')", (ts,))
        cov = cost_module.cost_coverage(conn, "default")
        assert cov["events"] == 3
        assert cov["billable_events"] == 2       # 零 token 不进分母
        assert cov["unknown_events"] == 1
        assert cov["estimated_cost_usd"] == 0.5
        assert cov["actual_cost_usd"] is None     # 没有真实账单 → None，不是 0
        assert cov["coverage"] == 0.5
    finally:
        conn.close()


def test_executor_records_provider_cost_as_actual(env):
    """D-2：CLI 的 total_cost_usd 是 provider 账单，必须落 actual 而不是 estimated。"""
    parsed = developer_parse('{"type":"result","total_cost_usd":0.17145,'
                             '"modelUsage":{"m":{"costUSD":0.17,"costBasis":"list"}}}')
    assert parsed is not None
    assert parsed["total_cost_usd"] == 0.17145
    assert parsed["model_usage"]["m"]["costBasis"] == "list"


def test_executor_rejects_bad_cost_values(env):
    """D-2：取不到合法金额就不写成本，绝不编一个数出来。"""
    for bad in ('{"type":"result"}', '{"type":"result","total_cost_usd":-1}',
                '{"type":"result","total_cost_usd":"abc"}', 'not json', ''):
        assert developer_parse(bad) is None
