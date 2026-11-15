"""Global Task 统一任务视图（P3-M5）。

锁定四件事（都对应真实会出错的场景，不是形式覆盖）：

1. **link 必须校验归属**——任务存在但属于别的项目时不能挂上去；
2. **rollup 状态正确**——全 done / 部分 done / 无 link 各自的结果；
3. **无 link 不能显示 done**——空壳任务最容易被"看起来完成"骗过去；
4. **同一 task 不能重复挂到同一 Global Task**。

另加一条地基约束：**`tasks.project_id` 仍是 NOT NULL**，
Global Task 是独立建模，不是放宽它（破坏了 35 项隔离语义）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import global_task, store

NOW = "2026-11-15T00:00:00+00:00"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    c = store.connect()
    store.init_db(c)
    for project_id, name in (("p1", "项目一"), ("p2", "项目二")):
        c.execute(
            "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
            " VALUES (?, ?, ?, '/tmp/r', ?, ?)",
            (project_id, project_id, name, NOW, NOW),
        )
    yield c
    c.close()


def _task(conn, project_id: str, key: str, status: str = "todo") -> str:
    task_id = f"{project_id}-{key}"
    conn.execute(
        "INSERT INTO tasks (id, project_id, task_key, title, status, priority,"
        " source, updated_at) VALUES (?, ?, ?, ?, ?, 'P2', 'platform', ?)",
        (task_id, project_id, key, f"{key} 标题", status, NOW),
    )
    conn.commit()
    return task_id


def _set_status(conn, task_id: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    conn.commit()


def _global(conn, key: str = "GT-1") -> str:
    return global_task.create_global_task(
        conn, global_key=key, title="跨项目统一任务")["id"]


# ---------------------------------------------------------------------------
# 地基约束
# ---------------------------------------------------------------------------
def test_tasks_project_id_stays_not_null(conn):
    """不能为了 Global Task 放宽 tasks.project_id——这是项目隔离的地基。"""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tasks (id, project_id, task_key, title, status, source,"
            " updated_at) VALUES ('x', NULL, 'K', 'T', 'todo', 'platform', ?)", (NOW,))


def test_global_task_has_no_project_column(conn):
    """Global Task 自身不挂项目；项目维度只存在于 link 表。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(global_tasks)")}
    assert "project_id" not in cols
    link_cols = {row[1] for row in conn.execute("PRAGMA table_info(global_task_links)")}
    assert {"project_id", "task_id", "link_role"} <= link_cols


# ---------------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------------
def test_create_and_get(conn):
    item = global_task.create_global_task(
        conn, global_key="GT-INFRA", title="基础设施统一", priority="P1",
        owner_principal_id="local-owner")
    assert item["global_key"] == "GT-INFRA"
    assert item["status"] == "open"
    assert item["no_links"] is True
    assert item["rollup_status"] == "open"
    fetched = global_task.get_global_task(conn, item["id"])
    assert fetched["title"] == "基础设施统一"
    assert fetched["links"] == []


def test_get_by_global_key(conn):
    gid = global_task.create_global_task(conn, global_key="GT-KEY", title="按 key 查")["id"]
    assert global_task.get_global_task(conn, "GT-KEY")["id"] == gid


def test_duplicate_global_key_rejected(conn):
    _global(conn, "GT-DUP")
    with pytest.raises(global_task.GlobalTaskError) as exc:
        _global(conn, "GT-DUP")
    assert exc.value.code == "conflict"
    assert exc.value.status == 409


def test_invalid_payloads(conn):
    cases = [
        ({"global_key": "", "title": "x"}, "invalid_payload"),
        ({"global_key": "GT OK", "title": "x"}, "invalid_payload"),
        ({"global_key": "GT-1", "title": ""}, "invalid_payload"),
        ({"global_key": "GT-1", "title": "x", "priority": "P9"}, "invalid_payload"),
    ]
    for kwargs, code in cases:
        with pytest.raises(global_task.GlobalTaskError) as exc:
            global_task.create_global_task(conn, **kwargs)
        assert exc.value.code == code


def test_get_missing_raises_404(conn):
    with pytest.raises(global_task.GlobalTaskError) as exc:
        global_task.get_global_task(conn, "gt_nope")
    assert exc.value.status == 404


# ---------------------------------------------------------------------------
# link 归属校验（最重要）
# ---------------------------------------------------------------------------
def test_link_accepts_task_of_same_project(conn):
    gid = _global(conn)
    tid = _task(conn, "p1", "T-1")
    item = global_task.link_task(conn, gid, "p1", tid)
    assert item["link_count"] == 1
    assert item["links"][0]["task_id"] == tid
    assert item["projects"] == ["p1"]


def test_link_rejects_task_from_other_project(conn):
    """任务真实存在但属于 p2，却声称挂到 p1 —— 必须 422，不能静默成功。"""
    gid = _global(conn)
    tid = _task(conn, "p2", "T-X")
    with pytest.raises(global_task.GlobalTaskError) as exc:
        global_task.link_task(conn, gid, "p1", tid)
    assert exc.value.code == "task_project_mismatch"
    assert exc.value.status == 422
    # 失败不能留下半截 link
    assert global_task.get_global_task(conn, gid)["links"] == []


def test_link_unknown_task(conn):
    gid = _global(conn)
    with pytest.raises(global_task.GlobalTaskError) as exc:
        global_task.link_task(conn, gid, "p1", "no-such-task")
    assert exc.value.code == "task_not_found"


def test_link_unknown_project(conn):
    gid = _global(conn)
    tid = _task(conn, "p1", "T-2")
    with pytest.raises(global_task.GlobalTaskError) as exc:
        global_task.link_task(conn, gid, "nope", tid)
    assert exc.value.code == "project_not_found"


def test_link_unknown_global_task(conn):
    tid = _task(conn, "p1", "T-3")
    with pytest.raises(global_task.GlobalTaskError) as exc:
        global_task.link_task(conn, "gt_missing", "p1", tid)
    assert exc.value.status == 404


def test_link_invalid_role(conn):
    gid = _global(conn)
    tid = _task(conn, "p1", "T-4")
    with pytest.raises(global_task.GlobalTaskError) as exc:
        global_task.link_task(conn, gid, "p1", tid, "owns")
    assert exc.value.code == "invalid_payload"


def test_link_by_task_key(conn):
    gid = _global(conn)
    _task(conn, "p1", "T-KEY")
    item = global_task.link_task(conn, gid, "p1", "T-KEY")
    assert item["links"][0]["task_key"] == "T-KEY"


def test_duplicate_link_rejected(conn):
    """同一 task 不能重复挂到同一 Global Task（UNIQUE 约束 + 409）。"""
    gid = _global(conn)
    tid = _task(conn, "p1", "T-DUP")
    global_task.link_task(conn, gid, "p1", tid)
    with pytest.raises(global_task.GlobalTaskError) as exc:
        global_task.link_task(conn, gid, "p1", tid, "verifies")
    assert exc.value.code == "conflict"
    assert exc.value.status == 409
    assert global_task.get_global_task(conn, gid)["link_count"] == 1


def test_same_task_can_link_to_two_global_tasks(conn):
    """约束是 (global_task_id, task_id)，不是 task_id 全局唯一。"""
    g1 = _global(conn, "GT-A")
    g2 = _global(conn, "GT-B")
    tid = _task(conn, "p1", "T-SHARED")
    global_task.link_task(conn, g1, "p1", tid)
    global_task.link_task(conn, g2, "p1", tid)
    assert global_task.get_global_task(conn, g1)["link_count"] == 1
    assert global_task.get_global_task(conn, g2)["link_count"] == 1


def test_cross_project_links(conn):
    gid = _global(conn)
    global_task.link_task(conn, gid, "p1", _task(conn, "p1", "T-1"))
    global_task.link_task(conn, gid, "p2", _task(conn, "p2", "T-1"))
    item = global_task.get_global_task(conn, gid)
    assert item["link_count"] == 2
    assert item["projects"] == ["p1", "p2"]


# ---------------------------------------------------------------------------
# rollup 状态
# ---------------------------------------------------------------------------
def test_rollup_no_links_is_open_and_flagged(conn):
    """空壳任务：必须 open + no_links，绝不能显示 done。"""
    gid = _global(conn)
    rollup = global_task.rollup_status(conn, gid)
    assert rollup["status"] == "open"
    assert rollup["no_links"] is True
    assert rollup["link_count"] == 0
    assert rollup["status"] != "done"


def test_rollup_all_done(conn):
    gid = _global(conn)
    for project_id in ("p1", "p2"):
        tid = _task(conn, project_id, "T-D", status="done")
        global_task.link_task(conn, gid, project_id, tid)
    rollup = global_task.rollup_status(conn, gid)
    assert rollup["status"] == "done"
    assert rollup["no_links"] is False
    assert rollup["task_counts"] == {"done": 2}


def test_rollup_all_todo_is_open(conn):
    gid = _global(conn)
    global_task.link_task(conn, gid, "p1", _task(conn, "p1", "T-1"))
    global_task.link_task(conn, gid, "p1", _task(conn, "p1", "T-2"))
    rollup = global_task.rollup_status(conn, gid)
    assert rollup["status"] == "open"
    assert rollup["no_links"] is False


def test_rollup_partial_done_is_in_progress(conn):
    """部分完成 = 进行中。把「3/5 完成」显示成 done 是最典型的撒谎。"""
    gid = _global(conn)
    global_task.link_task(conn, gid, "p1", _task(conn, "p1", "T-1", "done"))
    global_task.link_task(conn, gid, "p2", _task(conn, "p2", "T-2", "todo"))
    rollup = global_task.rollup_status(conn, gid)
    assert rollup["status"] == "in_progress"
    assert rollup["task_counts"] == {"done": 1, "todo": 1}


@pytest.mark.parametrize("status", ["in_progress", "review", "blocked"])
def test_rollup_any_started_task_is_in_progress(conn, status):
    gid = _global(conn)
    global_task.link_task(conn, gid, "p1", _task(conn, "p1", "T-1", status))
    global_task.link_task(conn, gid, "p1", _task(conn, "p1", "T-2", "done"))
    assert global_task.rollup_status(conn, gid)["status"] == "in_progress"


def test_rollup_unknown_status_not_treated_as_not_started(conn):
    """未知状态按「已开始」处理：宁可显示进行中，不能显示成未开始。"""
    gid = _global(conn)
    global_task.link_task(conn, gid, "p1", _task(conn, "p1", "T-1", "weird_state"))
    assert global_task.rollup_status(conn, gid)["status"] == "in_progress"


def test_rollup_follows_live_task_status(conn):
    """聚合状态现算，不缓存：项目里状态变了，全局立刻跟上。"""
    gid = _global(conn)
    tid = _task(conn, "p1", "T-1")
    global_task.link_task(conn, gid, "p1", tid)
    assert global_task.rollup_status(conn, gid)["status"] == "open"
    _set_status(conn, tid, "in_progress")
    assert global_task.rollup_status(conn, gid)["status"] == "in_progress"
    _set_status(conn, tid, "done")
    assert global_task.rollup_status(conn, gid)["status"] == "done"


def test_cancelled_overrides_rollup(conn):
    """人工 cancelled 覆盖派生：停掉的任务不该因为子任务做完就自动变 done。"""
    gid = _global(conn)
    global_task.link_task(conn, gid, "p1", _task(conn, "p1", "T-1", "done"))
    conn.execute("UPDATE global_tasks SET status = 'cancelled' WHERE id = ?", (gid,))
    conn.commit()
    rollup = global_task.rollup_status(conn, gid)
    assert rollup["status"] == "cancelled"
    assert rollup["derived_status"] == "done"


def test_unlink_restores_open_when_last_link_removed(conn):
    gid = _global(conn)
    tid = _task(conn, "p1", "T-1", "done")
    item = global_task.link_task(conn, gid, "p1", tid)
    link_id = item["links"][0]["link_id"]
    assert global_task.rollup_status(conn, gid)["status"] == "done"

    result = global_task.unlink_task(conn, link_id)
    assert result["task_id"] == tid
    rollup = global_task.rollup_status(conn, gid)
    assert rollup["status"] == "open"
    assert rollup["no_links"] is True


def test_unlink_with_wrong_global_task_is_404(conn):
    """指定了 global_task_id 时，link 必须真的属于它，防止跨任务误删。"""
    g1 = _global(conn, "GT-A")
    g2 = _global(conn, "GT-B")
    item = global_task.link_task(conn, g1, "p1", _task(conn, "p1", "T-1"))
    link_id = item["links"][0]["link_id"]
    with pytest.raises(global_task.GlobalTaskError) as exc:
        global_task.unlink_task(conn, link_id, global_task_id=g2)
    assert exc.value.status == 404
    assert global_task.get_global_task(conn, g1)["link_count"] == 1


def test_unlink_unknown_link(conn):
    with pytest.raises(global_task.GlobalTaskError) as exc:
        global_task.unlink_task(conn, "gtl_nope")
    assert exc.value.status == 404


def test_link_project_helper(conn):
    """HTTP 层靠它做项目级授权：不能凭 link_id 删别的项目的关联。"""
    gid = _global(conn)
    item = global_task.link_task(conn, gid, "p2", _task(conn, "p2", "T-1"))
    link_id = item["links"][0]["link_id"]
    assert global_task.link_project(conn, link_id) == "p2"
    assert global_task.link_project(conn, link_id, global_task_id="gt_other") is None
    assert global_task.link_project(conn, "gtl_nope") is None


# ---------------------------------------------------------------------------
# 列表 / 概览
# ---------------------------------------------------------------------------
def test_list_includes_link_count_and_rollup(conn):
    g1 = _global(conn, "GT-A")
    _global(conn, "GT-B")
    global_task.link_task(conn, g1, "p1", _task(conn, "p1", "T-1", "done"))
    global_task.link_task(conn, g1, "p2", _task(conn, "p2", "T-2", "in_progress"))

    listed = global_task.list_global_tasks(conn)
    assert listed["total"] == 2
    by_key = {item["global_key"]: item for item in listed["items"]}
    assert by_key["GT-A"]["link_count"] == 2
    assert by_key["GT-A"]["rollup_status"] == "in_progress"
    assert by_key["GT-B"]["link_count"] == 0
    assert by_key["GT-B"]["no_links"] is True
    assert by_key["GT-B"]["rollup_status"] != "done"


def test_list_status_filter_uses_effective_status(conn):
    """按生效状态过滤：列里写着 open 但子任务都做完了，应能查得到。"""
    gid = _global(conn, "GT-DONE")
    global_task.link_task(conn, gid, "p1", _task(conn, "p1", "T-1", "done"))
    empty = _global(conn, "GT-EMPTY")

    done_items = global_task.list_global_tasks(conn, status="done")["items"]
    assert [item["global_key"] for item in done_items] == ["GT-DONE"]
    open_items = global_task.list_global_tasks(conn, status="open")["items"]
    # 空壳任务生效状态就是 open；GT-DONE 已派生为 done，不能出现在 open 里。
    assert [item["global_key"] for item in open_items] == ["GT-EMPTY"]
    assert global_task.get_global_task(conn, empty)["status"] == "open"


def test_list_filters_by_project(conn):
    g1 = _global(conn, "GT-A")
    _global(conn, "GT-B")
    global_task.link_task(conn, g1, "p2", _task(conn, "p2", "T-1"))
    items = global_task.list_global_tasks(conn, project_id="p2")["items"]
    assert [item["global_key"] for item in items] == ["GT-A"]
    assert global_task.list_global_tasks(conn, project_id="p1")["items"] == []


def test_list_rejects_bad_status(conn):
    with pytest.raises(global_task.GlobalTaskError) as exc:
        global_task.list_global_tasks(conn, status="finished")
    assert exc.value.code == "invalid_payload"


def test_global_overview_groups_by_project(conn):
    gid = _global(conn, "GT-1")
    global_task.link_task(conn, gid, "p1", _task(conn, "p1", "T-1", "in_progress"))
    global_task.link_task(conn, gid, "p2", _task(conn, "p2", "T-2", "done"))
    _global(conn, "GT-2")

    overview = global_task.global_overview(conn)
    totals = overview["totals"]
    assert totals["global_tasks"] == 2
    assert totals["links"] == 2
    assert totals["global_tasks_without_links"] == 1
    assert totals["projects_with_links"] == 2
    by_project = {p["project_id"]: p for p in overview["projects"]}
    assert by_project["p1"]["link_count"] == 1
    assert by_project["p1"]["global_task_count"] == 1
    assert by_project["p2"]["task_status"] == {"done": 1}


def test_global_overview_empty_is_honest(conn):
    """没有任何数据时如实返回 0，不编造完成度。"""
    overview = global_task.global_overview(conn)
    assert overview["totals"]["global_tasks"] == 0
    assert overview["totals"]["by_status"] == {}
    assert overview["global_tasks"] == []
    assert overview["totals"]["projects_with_links"] == 0


def test_link_writes_audit(conn):
    """挂接/解挂必须留痕，否则「谁把哪条任务挂上来的」无从追溯。"""
    gid = _global(conn)
    item = global_task.link_task(conn, gid, "p1", _task(conn, "p1", "T-1"),
                                 linked_by="local-owner")
    link_id = item["links"][0]["link_id"]
    global_task.unlink_task(conn, link_id, unlinked_by="local-owner")
    actions = [
        row[0] for row in conn.execute(
            "SELECT action FROM audit_logs WHERE resource_type = 'global_task'"
            " ORDER BY created_at, rowid")
    ]
    assert "global_task.create" in actions
    assert "global_task.link" in actions
    assert "global_task.unlink" in actions
