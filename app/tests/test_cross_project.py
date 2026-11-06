"""跨项目协作（三期 M3-2）：跨项目依赖、协作图、共享资源。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import resources, scheduler, service, store


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(db))
    monkeypatch.setenv("DDP_AGENT_PLATFORM_REPO", str(repo))
    monkeypatch.setenv("DDP_AGENT_PLATFORM_PROJECTS_ROOT", str(tmp_path / "projects"))
    cfg = tmp_path / "projects.json"
    cfg.write_text('{"projects": []}', encoding="utf-8")
    monkeypatch.setenv("DDP_AGENT_PLATFORM_CONFIG", str(cfg))
    service.bootstrap(repo_root=repo)
    service.create_project({
        "code": "proj-b", "name": "Project B", "repo_root": str(other),
        "session_slugs": [],
    })
    return SimpleNamespace(db=db, repo=repo, other=other)


def _agent(project="default", role="frontend-engineer"):
    with store.session() as conn:
        row = conn.execute(
            "SELECT id FROM project_agents WHERE project_id=? AND role_key=?",
            (project, role),
        ).fetchone()
        return row["id"]


def _task(project, key, **extra):
    payload = {"title": f"任务 {key}", "task_key": key}
    payload.update(extra)
    return service.create_task(project, payload)["task"]


# ---------------------------------------------------------------------------
# 跨项目依赖
# ---------------------------------------------------------------------------
def test_same_project_dependency_still_works(env):
    a = _task("default", "X-001")
    b = _task("default", "X-002")
    service.add_dependency("default", b["id"], {"depends_on_task_id": a["id"]})
    detail = service.task_cross_dependencies("default", b["id"])
    assert detail["outgoing"] == []  # 同项目不算跨项目


def test_cross_project_dependency_requires_flag(env):
    a = _task("proj-b", "B-001")
    b = _task("default", "X-003")
    with pytest.raises(service.PlatformError) as exc:
        service.add_dependency("default", b["id"], {"depends_on_task_id": a["id"]})
    assert exc.value.code == "cross_project_reference"


def test_cross_project_dependency_created(env):
    a = _task("proj-b", "B-002")
    b = _task("default", "X-004")
    result = service.add_dependency(
        "default", b["id"], {"depends_on_task_id": a["id"], "cross_project": True}
    )
    assert result["dependency_id"]

    out = service.task_cross_dependencies("default", b["id"])["outgoing"]
    assert len(out) == 1
    assert out[0]["target_project_id"] == "proj-b"
    assert out[0]["target_task_key"] == "B-002"

    # 反向可见：B 项目能看到谁在等它
    inc = service.task_cross_dependencies("proj-b", a["id"])["incoming"]
    assert len(inc) == 1
    assert inc[0]["source_project_id"] == "default"


def test_cross_project_requires_id_not_task_key(env):
    _task("proj-b", "B-003")
    b = _task("default", "X-005")
    with pytest.raises(service.PlatformError) as exc:
        service.add_dependency(
            "default", b["id"], {"depends_on_task_id": "B-003", "cross_project": True}
        )
    assert exc.value.code == "ambiguous_reference"


def test_cross_project_only_blocks_type(env):
    a = _task("proj-b", "B-004")
    b = _task("default", "X-006")
    with pytest.raises(service.PlatformError) as exc:
        service.add_dependency(
            "default", b["id"],
            {"depends_on_task_id": a["id"], "cross_project": True, "dependency_type": "related"},
        )
    assert "只支持 blocks" in exc.value.message


def test_cross_project_cycle_detected(env):
    """A项目任务 → B项目任务 → A项目任务 必须被全局图检测拦住。"""
    a1 = _task("default", "X-010")
    b1 = _task("proj-b", "B-010")
    service.add_dependency(
        "default", a1["id"], {"depends_on_task_id": b1["id"], "cross_project": True}
    )
    with pytest.raises(service.PlatformError) as exc:
        service.add_dependency(
            "proj-b", b1["id"], {"depends_on_task_id": a1["id"], "cross_project": True}
        )
    assert exc.value.code == "dependency_cycle"


def test_cross_project_graph(env):
    a = _task("proj-b", "B-020")
    b = _task("default", "X-020")
    service.add_dependency(
        "default", b["id"], {"depends_on_task_id": a["id"], "cross_project": True}
    )
    graph = service.cross_project_graph()
    assert {p["id"] for p in graph["projects"]} >= {"default", "proj-b"}
    assert len(graph["edges"]) == 1
    assert graph["blocked_edges"] == 1  # B-020 未完成
    assert graph["blocked_by_project"]["default"] == 1


def test_cross_project_unblocks_when_done(env):
    a = _task("proj-b", "B-030")
    b = _task("default", "X-030")
    service.add_dependency(
        "default", b["id"], {"depends_on_task_id": a["id"], "cross_project": True}
    )
    for status in ("in_progress", "review", "done"):
        v = service.get_task("proj-b", a["id"])["task"]["version"]
        service.transition_task("proj-b", a["id"], {"to_status": status, "expected_version": v})
    assert service.cross_project_graph()["blocked_edges"] == 0


def test_scheduler_respects_cross_project_dependency(env):
    a = _task("proj-b", "B-040")
    b = _task("default", "X-040", assignee_agent_id=_agent())
    service.add_dependency(
        "default", b["id"], {"depends_on_task_id": a["id"], "cross_project": True}
    )
    with store.session() as conn:
        ready, skipped = scheduler.schedulable(conn, "default")
    assert b["id"] not in {t["id"] for t in ready}
    assert any(s["task_key"] == "X-040" and "B-040" in s["reason"] for s in skipped)


# ---------------------------------------------------------------------------
# 共享资源
# ---------------------------------------------------------------------------
def _resource(name="4090 站点运行时", kind="host", capacity=1, exclusive=True):
    return resources.create_resource({
        "name": name, "kind": kind, "capacity": capacity, "exclusive": exclusive,
    })


def test_create_and_get_resource(env):
    res = _resource()
    assert res["name"] == "4090 站点运行时"
    assert res["claims"] == []
    assert resources.list_resources()["total"] == 1


def test_duplicate_resource_name_rejected(env):
    _resource()
    with pytest.raises(resources.ResourceError) as exc:
        _resource()
    assert exc.value.code == "conflict"


def test_exclusive_resource_blocks_second_project(env):
    res = _resource()
    first = resources.claim_resource(res["id"], {"project_id": "default"})
    assert first["claim_id"]
    with pytest.raises(resources.ResourceError) as exc:
        resources.claim_resource(res["id"], {"project_id": "proj-b"})
    assert exc.value.code == "resource_busy"
    assert "default" in exc.value.message


def test_release_allows_other_project(env):
    res = _resource()
    claim = resources.claim_resource(res["id"], {"project_id": "default"})
    resources.release_claim(res["id"], claim["claim_id"])
    second = resources.claim_resource(res["id"], {"project_id": "proj-b"})
    assert second["claim_id"]


def test_shared_resource_capacity(env):
    res = _resource(name="GPU 池", kind="gpu", capacity=2, exclusive=False)
    resources.claim_resource(res["id"], {"project_id": "default", "amount": 2})
    with pytest.raises(resources.ResourceError) as exc:
        resources.claim_resource(res["id"], {"project_id": "proj-b", "amount": 1})
    assert exc.value.code == "capacity_exceeded"


def test_expired_claim_frees_resource(env):
    conn = store.connect(env.db)
    try:
        store.init_db(conn)
        res = _resource()
        # 直接插一条已过期的占用
        conn.execute(
            "INSERT INTO resource_claims (id, resource_id, project_id, amount, status,"
            " expires_at, created_at) VALUES ('c1', ?, 'default', 1, 'active',"
            " '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')",
            (res["id"],),
        )
        assert resources.expire_stale_claims(conn) == 1
    finally:
        conn.close()
    # 过期后另一个项目可以占用
    claim = resources.claim_resource(res["id"], {"project_id": "proj-b"})
    assert claim["claim_id"]


def test_release_is_idempotent(env):
    res = _resource()
    claim = resources.claim_resource(res["id"], {"project_id": "default"})
    first = resources.release_claim(res["id"], claim["claim_id"])
    second = resources.release_claim(res["id"], claim["claim_id"])
    assert first["changed"] is True
    assert second["changed"] is False


def test_unknown_resource(env):
    with pytest.raises(resources.ResourceError) as exc:
        resources.get_resource("nope")
    assert exc.value.code == "not_found"
