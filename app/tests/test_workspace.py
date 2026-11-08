"""工作空间体验层：项目组合、我的工作台、消息中心、全局搜索。

验证重点（对应产品补充规范 §3/§4/§5）：
* 组合视图的风险等级来自显式规则，不是黑箱打分；
* 消息已读只是个人偏好，不改变业务状态；
* 检索与聚合必须先按可见项目过滤，无权主体拿不到任何数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import store, workspace

NOW = "2026-11-08T00:00:00+00:00"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    conn = store.connect()
    store.init_db(conn)
    for pid, name in (("p1", "项目一"), ("p2", "项目二")):
        conn.execute(
            "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
            " VALUES (?, ?, ?, '/tmp/repo', ?, ?)", (pid, pid, name, NOW, NOW),
        )
    conn.execute(
        "INSERT INTO tasks (id, project_id, task_key, title, status, priority, source,"
        " updated_at) VALUES ('t1','p1','K-1','阻塞任务','blocked','P0','platform',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO tasks (id, project_id, task_key, title, status, priority, source,"
        " updated_at) VALUES ('t2','p1','K-2','已完成任务','done','P1','platform',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO approvals (id, project_id, resource_type, resource_id, action,"
        " requested_by, status, created_at) VALUES ('a1','p1','release','r1','publish',"
        " 'agent','pending',?)", (NOW,),
    )
    conn.execute(
        "INSERT INTO deliveries (id, project_id, title, kind, created_at, status, source)"
        " VALUES ('d1','p1','交付说明','doc',?,'submitted','platform')", (NOW,),
    )
    conn.commit()
    yield conn
    conn.close()


def test_portfolio_risk_rules(conn):
    data = workspace.portfolio(conn, principal_id="local-owner")
    assert data["project_count"] == 2
    by_id = {p["id"]: p for p in data["projects"]}
    p1 = by_id["p1"]
    assert p1["tasks"]["total"] == 2
    assert p1["tasks"]["done"] == 1
    assert p1["tasks"]["completion"] == 0.5
    assert p1["pending_deliveries"] == 1
    assert p1["pending_approvals"] == 1
    # 有阻塞任务 + 待审批 → high，且原因可读
    assert p1["risk"] == "high"
    assert any("阻塞" in r for r in p1["risk_reasons"])
    # p2 没有任何活动，不伪造成功率
    assert by_id["p2"]["tasks"]["completion"] is None
    assert by_id["p2"]["risk"] == "low"
    assert data["summary"]["blocked_tasks"] == 1


def test_my_work_aggregates_only_actionable(conn):
    data = workspace.my_work(conn, principal_id="local-owner")
    assert [t["id"] for t in data["tasks"]] == ["t1"]  # done 不进待办
    assert [a["id"] for a in data["approvals"]] == ["a1"]
    assert [d["id"] for d in data["deliveries"]] == ["d1"]
    assert data["counts"]["blocked_tasks"] == 1


def test_notifications_and_read_state(conn):
    data = workspace.notifications(conn, principal_id="local-owner")
    keys = {i["key"] for i in data["items"]}
    assert {"approval:a1", "delivery:d1", "task:t1"} <= keys
    assert data["unread"] == len(data["items"])

    workspace.mark_notifications(conn, "local-owner", ["approval:a1"], "read")
    after = workspace.notifications(conn, principal_id="local-owner")
    approval = next(i for i in after["items"] if i["key"] == "approval:a1")
    assert approval["read"] is True
    assert after["unread"] == len(after["items"]) - 1

    # 已读只是个人偏好：审批本身仍然是 pending
    status = conn.execute("SELECT status FROM approvals WHERE id='a1'").fetchone()[0]
    assert status == "pending"

    # 视图过滤
    only_unread = workspace.notifications(conn, principal_id="local-owner", status="unread")
    assert all(not i["read"] for i in only_unread["items"])

    # 其他主体看不到本人的已读状态
    other = workspace.notifications(conn, principal_id="local-owner")
    assert other["unread"] == after["unread"]


def test_mark_notifications_rejects_bad_state(conn):
    with pytest.raises(ValueError):
        workspace.mark_notifications(conn, "local-owner", ["x"], "deleted")


def test_search_filters_by_type_and_project(conn):
    all_hits = workspace.search(conn, q="阻塞", principal_id="local-owner")
    assert {r["type"] for r in all_hits["results"]} == {"task"}

    none_hits = workspace.search(conn, q="阻塞", types=["delivery"], principal_id="local-owner")
    assert none_hits["count"] == 0

    scoped = workspace.search(conn, q="阻塞", project_id="p2", principal_id="local-owner")
    assert scoped["count"] == 0

    empty = workspace.search(conn, q="   ", principal_id="local-owner")
    assert empty["count"] == 0


def test_unprivileged_principal_sees_nothing(conn):
    conn.execute(
        "INSERT INTO principals (id, principal_type, display_name, is_platform_owner,"
        " status, created_at, updated_at)"
        " VALUES ('outsider','user','外部用户',0,'active',?,?)", (NOW, NOW),
    )
    conn.commit()
    assert workspace.visible_project_ids(conn, "outsider") == []
    assert workspace.portfolio(conn, principal_id="outsider")["project_count"] == 0
    assert workspace.search(conn, q="阻塞", principal_id="outsider")["count"] == 0
    assert workspace.notifications(conn, principal_id="outsider")["count"] == 0
    assert workspace.my_work(conn, principal_id="outsider")["counts"] == {}


def test_suspended_principal_denied(conn):
    conn.execute(
        "INSERT INTO principals (id, principal_type, display_name, is_platform_owner,"
        " status, created_at, updated_at)"
        " VALUES ('frozen','user','停用用户',1,'suspended',?,?)", (NOW, NOW),
    )
    conn.commit()
    assert workspace.visible_project_ids(conn, "frozen") == []


def test_saved_views_are_per_principal_and_upsert(conn):
    saved = workspace.save_view(conn, "local-owner", {
        "scope": "portfolio", "name": "高风险项目",
        "filters": {"risk": "high", "empty": "", "nested": {"x": 1}},
    })["view"]
    assert saved["filters"] == {"risk": "high"}          # 只留扁平标量
    assert workspace.list_views(conn, "local-owner", "portfolio")["total"] == 1

    # 同名再存是更新，不产生第二条
    workspace.save_view(conn, "local-owner", {
        "scope": "portfolio", "name": "高风险项目", "filters": {"risk": "medium"},
    })
    items = workspace.list_views(conn, "local-owner", "portfolio")["items"]
    assert len(items) == 1 and items[0]["filters"] == {"risk": "medium"}

    # 别人看不到、也删不掉
    assert workspace.list_views(conn, "other", "portfolio")["total"] == 0
    with pytest.raises(workspace.ViewError) as err:
        workspace.delete_view(conn, "other", saved["id"])
    assert err.value.code == "not_found" and err.value.status == 404

    assert workspace.delete_view(conn, "local-owner", saved["id"])["ok"] is True
    assert workspace.list_views(conn, "local-owner", "portfolio")["total"] == 0


def test_saved_view_rejects_bad_input(conn):
    for payload in (
        {"scope": "nope", "name": "x", "filters": {}},
        {"scope": "portfolio", "name": "", "filters": {}},
        {"scope": "portfolio", "name": "x", "filters": "not-a-dict"},
    ):
        with pytest.raises(workspace.ViewError):
            workspace.save_view(conn, "local-owner", payload)
    with pytest.raises(workspace.ViewError):
        workspace.list_views(conn, "local-owner", "nope")


def test_portfolio_risk_includes_execution_failures(conn):
    # 近 24 小时有执行失败 → 高风险，且原因可读
    conn.execute(
        "INSERT INTO agent_events (id, project_id, event_type, message, tool, ok, occurred_at)"
        " VALUES ('ev-fail', 'p2', 'tool_use', '构建失败', 'Bash', 0, ?)",
        (NOW,),
    )
    data = workspace.portfolio(conn, "local-owner")
    p2 = next(x for x in data["projects"] if x["id"] == "p2")
    assert p2["execution"]["total"] == 1
    assert p2["execution"]["counts"]["failed"] == 1
    assert p2["risk"] == "high"
    assert any("执行失败" in r for r in p2["risk_reasons"])
    assert data["summary"]["execution_failed"] == 1


def test_portfolio_flags_mostly_unverified_execution(conn):
    for i in range(3):
        conn.execute(
            "INSERT INTO agent_events (id, project_id, event_type, message, tool, ok, occurred_at)"
            " VALUES (?, 'p2', 'status_report', '有动静', NULL, NULL, ?)",
            (f"ev-unv-{i}", NOW),
        )
    p2 = next(x for x in workspace.portfolio(conn, "local-owner")["projects"] if x["id"] == "p2")
    assert p2["execution"]["counts"]["unverified"] == 3
    assert p2["risk"] in {"medium", "high"}
    assert any("未验证" in r for r in p2["risk_reasons"])
