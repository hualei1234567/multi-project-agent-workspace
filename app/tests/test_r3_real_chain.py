"""R3：P4 八大域真实链路——闸门真阻断、授权真隔离、发布口径真区分。

这些用例锁定的是审计指出的具体缺陷：
- human_gate 原来只是标签（`human_gates: [...]` 统计），不阻断流程；
- `ref_id` 原来是自由字符串，悬空引用也能上架；
- `publish()` 只改状态，无法区分「有真实流量」与「只是改了个状态」；
- 评估输出既可以来自运行时，也可以由调用方直接塞，但两者混在一起看不出来。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from agent_platform import service, store
from agent_platform.p4 import autonomy, catalog, evaluation, knowledge, release, workflow


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    config = tmp_path / "projects.json"
    config.write_text(json.dumps({"projects": []}), encoding="utf-8")
    monkeypatch.setenv("AGWS_PLATFORM_CONFIG", str(config))
    monkeypatch.setenv("AGWS_PLATFORM_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setenv("AGWS_PLATFORM_REPO", str(repo))
    service.bootstrap(repo_root=repo)
    conn = store.connect()
    store.init_db(conn)
    now = store.now_iso()
    for pid in ("p1", "p2"):
        conn.execute(
            "INSERT OR IGNORE INTO agent_projects (id, code, name, repo_root,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (pid, pid, pid, str(repo), now, now),
        )
    conn.commit()
    yield SimpleNamespace(conn=conn, repo=repo, tmp=tmp_path)
    conn.close()


def _gate_workflow(conn, project="p1"):
    """带一个人工闸门的 workflow，已发布并绑定。"""
    definition = workflow.create_definition(conn, project, {
        "key": "chain", "name": "受控链路",
    })
    version = workflow.create_version(conn, definition["id"], {
        "dsl": {"nodes": [
            {"key": "dev", "type": "task", "title": "开发"},
            {"key": "gate", "type": "human_gate", "title": "人审", "depends_on": ["dev"]},
            {"key": "ship", "type": "task", "title": "上线", "depends_on": ["gate"]},
        ]},
    })
    workflow.test_version(conn, version["id"])
    workflow.publish_version(conn, version["id"], published_by="alice")
    # 物化出真实 tasks：workflow 域自己不执行任务，节点完成与否只能由 task 状态回推。
    binding = workflow.bind(conn, project, version["id"], materialize=True)
    return version, binding


def _finish_task(conn, project, def_key, node_key):
    """把物化出来的节点任务置为 done，模拟上游真实完成。"""
    task_key = f"wf:{def_key}:{node_key}"
    conn.execute(
        "UPDATE tasks SET status = 'done', updated_at = ?"
        " WHERE project_id = ? AND task_key = ?",
        (store.now_iso(), project, task_key),
    )
    conn.commit()


# ------------------------------------------------------------------ M3 闸门
def test_human_gate_blocks_downstream(env):
    """M3 核心：没有审批，流程必须停在闸门，下游节点不可达。"""
    conn = env.conn
    _version, binding = _gate_workflow(conn)
    run = workflow.start_run(conn, "p1", binding["binding_id"])
    # 上游还没完成时，流程停在 running —— 闸门不该被「越过上游」触发
    assert run["status"] == "running"

    _finish_task(conn, "p1", "chain", "dev")
    run = workflow.advance_run(conn, run["id"])

    assert run["status"] == "waiting_approval", "上游完成后闸门没有阻断流程"
    assert run["blocked"] is True
    assert run["waiting_on"] == ["gate"]
    # 下游节点必须还是 pending —— 这才叫「阻断」，而不是「顺便标了一下」
    nodes = {n["node_key"]: n["status"] for n in run["nodes"]}
    assert nodes["ship"] == "pending"
    assert nodes["gate"] == "waiting_approval"


def test_approval_releases_the_gate(env):
    """审批之后流程才能继续，且闸门变成 completed。"""
    conn = env.conn
    _version, binding = _gate_workflow(conn)
    run = workflow.start_run(conn, "p1", binding["binding_id"])
    _finish_task(conn, "p1", "chain", "dev")
    run = workflow.advance_run(conn, run["id"])
    after = workflow.approve_node(
        conn, run["id"], "gate", approver="alice", reason="已核对交付物",
    )
    nodes = {n["node_key"]: n["status"] for n in after["nodes"]}
    assert nodes["gate"] == "completed"
    assert after["status"] != "waiting_approval"


def test_approval_requires_a_real_approver(env):
    """审批必须绑定可信身份：空审批人直接拒绝，不允许匿名通过闸门。"""
    conn = env.conn
    _version, binding = _gate_workflow(conn)
    run = workflow.start_run(conn, "p1", binding["binding_id"])
    _finish_task(conn, "p1", "chain", "dev")
    run = workflow.advance_run(conn, run["id"])
    with pytest.raises(workflow.WorkflowError) as exc:
        workflow.approve_node(conn, run["id"], "gate", approver="  ")
    assert exc.value.code == "invalid_payload"


def test_revoked_approval_closes_the_gate_again(env):
    """撤销审批要立刻重新阻断——否则「撤销」只是个装饰。"""
    conn = env.conn
    _version, binding = _gate_workflow(conn)
    run = workflow.start_run(conn, "p1", binding["binding_id"])
    _finish_task(conn, "p1", "chain", "dev")
    run = workflow.advance_run(conn, run["id"])
    after = workflow.approve_node(conn, run["id"], "gate", approver="alice")
    approval_id = after["approvals"][0]["id"]

    again = workflow.revoke_approval(
        conn, approval_id, reason="交付物有误", revoked_by="alice",
    )
    nodes = {n["node_key"]: n["status"] for n in again["nodes"]}
    assert nodes["gate"] == "waiting_approval"
    assert again["status"] == "waiting_approval"


def test_input_change_invalidates_approval(env):
    """审批绑定输入摘要：输入一变，旧审批自动失效（M3 验收项）。"""
    conn = env.conn
    _version, binding = _gate_workflow(conn)
    run = workflow.start_run(conn, "p1", binding["binding_id"], inputs={"v": 1})
    _finish_task(conn, "p1", "chain", "dev")
    run = workflow.advance_run(conn, run["id"])
    workflow.approve_node(conn, run["id"], "gate", approver="alice")

    # 模拟输入变化：改掉 run 的 digest，等于换了一份内容再跑
    conn.execute(
        "UPDATE workflow_runs SET input_digest = ? WHERE id = ?",
        ("different-digest", run["id"]),
    )
    conn.execute(
        "UPDATE workflow_node_runs SET status = 'pending', input_digest = ?"
        " WHERE run_id = ? AND node_key = 'gate'", ("different-digest", run["id"]),
    )
    conn.commit()
    advanced = workflow.advance_run(conn, run["id"])
    assert advanced["status"] == "waiting_approval", "输入变了审批却没失效"


def test_non_gate_node_needs_no_approval(env):
    """普通节点不能被当成闸门：审批对非闸门节点无意义，应当拒绝。"""
    conn = env.conn
    _version, binding = _gate_workflow(conn)
    run = workflow.start_run(conn, "p1", binding["binding_id"])
    with pytest.raises(workflow.WorkflowError) as exc:
        workflow.approve_node(conn, run["id"], "dev", approver="alice")
    assert exc.value.code == "invalid_state"


# ------------------------------------------------------------------ M2 授权
def _published_knowledge(conn, project="p1"):
    source = knowledge.create_source(conn, project, {
        "title": "来源", "kind": "note",
    })
    item = knowledge.create_item(conn, project, {
        "source_id": source["id"], "title": "部署手册",
        "content": "deploy guide run smoketest first",
    })
    version_id = item["versions"][0]["id"] if item.get("versions") else item["current_version_id"]
    knowledge.publish_version(conn, version_id, reviewed_by="alice")
    conn.execute("UPDATE knowledge_items SET status = 'published' WHERE id = ?", (item["id"],))
    conn.commit()
    return knowledge.get_item(conn, item["id"]), version_id


def test_cross_project_search_requires_grant(env):
    """M2 核心：未授权项目检索不到别人的知识。"""
    conn = env.conn
    _published_knowledge(conn, "p1")
    assert knowledge.search(conn, "p1", "smoketest")["total"] >= 1
    other = knowledge.search(conn, "p2", "smoketest")
    assert other["total"] == 0, "未授权却检索到了别的项目的知识"


def test_grant_enables_and_revoke_blocks_immediately(env):
    """授权后可见；撤销后立刻不可见。"""
    conn = env.conn
    item, vid = _published_knowledge(conn, "p1")
    grant = knowledge.request_grant(
        conn, item["id"], target_project_id="p2", granted_by="alice",
        purpose="联调参考", version_id=vid,
    )
    assert knowledge.search(conn, "p2", "smoketest")["total"] == 0  # 待审批不算

    knowledge.approve_grant(conn, grant["id"], approved_by="bob")
    hits = knowledge.search(conn, "p2", "smoketest")
    assert hits["total"] >= 1, "授权后仍检索不到"
    assert hits["hits"][0]["via_grant"] is True
    assert hits["hits"][0]["grant_id"] == grant["id"]

    knowledge.revoke_grant(conn, grant["id"], reason="联调结束", revoked_by="bob")
    assert knowledge.search(conn, "p2", "smoketest")["total"] == 0, "撤销没有立即生效"


def test_expired_grant_is_not_effective(env):
    """过期授权不能用：到期即恢复封闭。"""
    conn = env.conn
    item, vid = _published_knowledge(conn, "p1")
    grant = knowledge.request_grant(
        conn, item["id"], target_project_id="p2", granted_by="alice",
        version_id=vid, expires_at="2000-01-01T00:00:00+00:00",
    )
    knowledge.approve_grant(conn, grant["id"], approved_by="bob")
    assert knowledge.get_grant(conn, grant["id"])["effective"] is False
    assert knowledge.search(conn, "p2", "smoketest")["total"] == 0


# ------------------------------------------------------------------ M4 发布
def _approved_release(conn, project="p1"):
    created = release.create_release(conn, project, {
        "release_key": "r1", "name": "发布 1", "created_by": "alice",
        "items": [{"artifact_type": "prompt", "artifact_ref": "p1",
                   "artifact_version": "1", "digest": "d1"}],
    })
    release.validate_release(conn, created["id"])
    conn.execute(
        "UPDATE agent_releases SET status = 'approved' WHERE id = ?", (created["id"],)
    )
    conn.commit()
    return release.get_release(conn, created["id"])


def test_publish_defaults_to_simulated(env):
    """M4：默认 SIMULATED，且明确说明没有真实流量。"""
    conn = env.conn
    rel = _approved_release(conn)
    out = release.publish(conn, rel["id"])
    assert out["release_mode"] == "SIMULATED"
    assert out["has_live_traffic"] is False
    assert out["traffic_entry"] is None


def test_live_requires_traffic_evidence(env):
    """声称 LIVE 必须给流量入口证据，否则拒绝。"""
    conn = env.conn
    rel = _approved_release(conn)
    with pytest.raises(release.ReleaseError) as exc:
        release.publish(conn, rel["id"], release_mode="LIVE")
    assert exc.value.code == "invalid_payload"


def test_simulated_release_cannot_start_real_canary(env):
    """M4 核心：没流量的发布不许开「真实 Canary」，只能用演练接口。"""
    conn = env.conn
    rel = _approved_release(conn)
    release.publish(conn, rel["id"])
    with pytest.raises(release.ReleaseError) as exc:
        release.start_canary(conn, rel["id"])
    assert exc.value.code == "simulated_release"

    drill = release.simulate_canary(conn, rel["id"], stop_threshold={"error_rate": 0.1})
    assert drill["status"] == "simulated"
    assert drill["traffic_pct"] == 0


# ------------------------------------------------------------------ M8 目录
def test_dangling_ref_cannot_be_listed(env):
    """M8：引用不存在的资源不能上架。"""
    conn = env.conn
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.publish_listing(conn, "p1", {
            "capability_type": "workflow", "ref_id": "不存在",
            "ref_version": "1", "name": "幽灵能力",
        })
    assert exc.value.code == "dangling_ref"


def test_resolvable_ref_records_resolution(env):
    """能解析的 ref 上架后被标记为已解析。"""
    conn = env.conn
    version, _b = _gate_workflow(conn)
    listing = catalog.publish_listing(conn, "p1", {
        "capability_type": "workflow", "ref_id": version["id"],
        "ref_version": str(version["version"]), "name": "受控链路",
    })
    assert listing["ref_resolved"] == 1


# ------------------------------------------------------------------ M1 评估
def test_evaluation_can_read_runtime_execution(env):
    """M1：评估可以从真实执行读产出，并标明来源。"""
    conn = env.conn
    now = store.now_iso()
    conn.execute(
        "INSERT INTO executions (id, project_id, agent_id, session_id, status,"
        " started_at, summary, source, link_quality)"
        " VALUES ('ex1','p1','a1','s1','succeeded',?,?,'dispatcher','linked')",
        (now, json.dumps({"outputs": {"case-1": "冒烟测试通过"}})),
    )
    suite = evaluation.create_suite(conn, "p1", {"key": "s", "name": "套件"})
    dataset = evaluation.create_dataset(conn, "p1", {
        "suite_id": suite["id"], "key": "d", "name": "数据集",
    })
    version = evaluation.freeze_version(conn, dataset["id"], {"cases": [
        {"case_key": "case-1", "input": {"q": "跑冒烟"},
         "expected": {"want": "冒烟测试通过"}},
    ]})
    ev = evaluation.create_evaluator(conn, "p1", {
        "name": "含关键串",
        "rules": {"min_score": 0.5,
                  "checks": [{"type": "contains", "field": "want"}]},
    })
    run = evaluation.run_evaluation(conn, "p1", {
        "dataset_version_id": version["id"],
        "evaluator_version_id": ev["version_id"],
        "runtime_execution_id": "ex1",
    })
    assert run["output_source"] == "runtime"
    assert run["runtime_execution_id"] == "ex1"
    assert run["decision"] == "pass"


def test_evaluation_marks_caller_supplied_outputs(env):
    """M1：调用方塞入的输出来源必须标出来，不能冒充运行时产出。"""
    conn = env.conn
    suite = evaluation.create_suite(conn, "p1", {"key": "s2", "name": "套件2"})
    dataset = evaluation.create_dataset(conn, "p1", {
        "suite_id": suite["id"], "key": "d2", "name": "数据集2",
    })
    version = evaluation.freeze_version(conn, dataset["id"], {"cases": [
        {"case_key": "c1", "input": {"q": "x"}, "expected": {}},
    ]})
    ev = evaluation.create_evaluator(conn, "p1", {
        "name": "评测2",
        "rules": {"min_score": 0.1,
                  "checks": [{"type": "contains", "field": "must_include"}]},
    })
    run = evaluation.run_evaluation(conn, "p1", {
        "dataset_version_id": version["id"],
        "evaluator_version_id": ev["version_id"],
        "candidate_outputs": {"c1": "手写答案"},
    })
    assert run["output_source"] == "caller_supplied"


# ------------------------------------------------------------------ M7 降级
def _active_eligibility(conn, project="p1"):
    record = autonomy.get_or_create(conn, project, task_type="code_edit")
    conn.execute(
        "UPDATE autonomy_eligibilities SET level = 'A3', status = 'active',"
        " updated_at = ? WHERE id = ?", (store.now_iso(), record["id"]),
    )
    conn.commit()
    return autonomy.get(conn, record["id"])


def test_auto_downgrade_drops_one_level_on_failures(env):
    """M7：连续失败触发降级，且**只降不升**。"""
    conn = env.conn
    _active_eligibility(conn)
    now = store.now_iso()
    for index in range(3):
        conn.execute(
            "INSERT INTO executions (id, project_id, agent_id, session_id, status,"
            " started_at, source, link_quality)"
            " VALUES (?, 'p1', 'a1', ?, 'failed', ?, 'dispatcher', 'unknown')",
            (f"f{index}", f"s{index}", now),
        )
    conn.commit()
    out = autonomy.evaluate_downgrade(conn, "p1", task_type="code_edit")
    assert out["downgraded"] is True
    assert out["from_level"] == "A3" and out["to_level"] == "A2"
    assert "consecutive_failures" in out["triggers"]


def test_auto_downgrade_never_raises_level(env):
    """M7：自动动作不能提升权限，A0 再降还是 A0。"""
    conn = env.conn
    record = autonomy.get_or_create(conn, "p1", task_type="other")
    conn.execute(
        "UPDATE autonomy_eligibilities SET level = 'A0', status = 'active' WHERE id = ?",
        (record["id"],),
    )
    conn.commit()
    out = autonomy.evaluate_downgrade(conn, "p1", task_type="other",
                                      budget_exhausted=True)
    assert out["to_level"] == "A0"
    assert autonomy.LEVEL_ORDER[out["to_level"]] <= autonomy.LEVEL_ORDER["A0"]


# ------------------------------------------------------------ 审计覆盖
def test_gate_approval_is_audited(env):
    """闸门审批必须留审计：谁批准了哪个节点、批准的是哪份输入。"""
    conn = env.conn
    _version, binding = _gate_workflow(conn)
    run = workflow.start_run(conn, "p1", binding["binding_id"])
    _finish_task(conn, "p1", "chain", "dev")
    run = workflow.advance_run(conn, run["id"])
    workflow.approve_node(conn, run["id"], "gate", approver="alice")

    rows = conn.execute(
        "SELECT action, actor FROM audit_logs WHERE action = 'workflow.gate.approved'"
    ).fetchall()
    assert rows, "闸门审批没有写审计"
    assert rows[0]["actor"] == "alice"


def test_gate_revocation_is_audited(env):
    """撤销审批同样留痕，否则「谁放的、谁收的」都查不到。"""
    conn = env.conn
    _version, binding = _gate_workflow(conn)
    run = workflow.start_run(conn, "p1", binding["binding_id"])
    _finish_task(conn, "p1", "chain", "dev")
    run = workflow.advance_run(conn, run["id"])
    after = workflow.approve_node(conn, run["id"], "gate", approver="alice")
    workflow.revoke_approval(conn, after["approvals"][0]["id"],
                             reason="交付物有误", revoked_by="bob")
    rows = conn.execute(
        "SELECT actor FROM audit_logs WHERE action = 'workflow.gate.revoke'"
    ).fetchall()
    assert rows and rows[0]["actor"] == "bob"
