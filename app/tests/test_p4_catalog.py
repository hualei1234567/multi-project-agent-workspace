"""能力目录：上架引用、授权需用途、撤销后立即不可见。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import store
from agent_platform.p4 import catalog


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(tmp_path / "platform.db"))
    conn = store.connect()
    store.init_db(conn)
    for pid in ("p1", "p2"):
        conn.execute(
            "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
            " VALUES (?, ?, ?, '/tmp/r', '2026-11-06T00:00:00+00:00',"
            " '2026-11-06T00:00:00+00:00')", (pid, pid, pid))
    conn.commit()
    yield conn
    conn.close()


def _listing(conn, project="p1"):
    return catalog.publish_listing(conn, project, {
        "capability_type": "workflow", "ref_id": "wf-1", "ref_version": "2",
        "name": "标准上线流程", "summary": "含人工闸门",
    })


def test_publish_and_duplicate_conflict(conn):
    listing = _listing(conn)
    assert listing["status"] == "published"
    with pytest.raises(catalog.CatalogError) as exc:
        _listing(conn)
    assert exc.value.code == "conflict"


def test_own_listing_visible_only_to_owner(conn):
    _listing(conn)
    assert catalog.available_for_project(conn, "p1")["total"] == 1
    assert catalog.available_for_project(conn, "p2")["total"] == 0


def test_grant_requires_purpose(conn):
    listing = _listing(conn)
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.grant(conn, listing["id"], {"target_project_id": "p2"}, granted_by="alice")
    assert exc.value.code == "purpose_required"


def test_grant_makes_listing_visible_and_revoke_hides_it(conn):
    listing = _listing(conn)
    g = catalog.grant(conn, listing["id"], {
        "target_project_id": "p2", "actions": ["reference"], "purpose": "复用流程",
    }, granted_by="alice")
    available = catalog.available_for_project(conn, "p2")
    assert available["total"] == 1
    assert available["items"][0]["grant_id"] == g["id"]
    assert available["items"][0]["actions"] == ["reference"]
    assert available["items"][0]["from_own_project"] is False

    catalog.revoke_grant(conn, g["id"], reason="试点结束", revoked_by="alice")
    assert catalog.available_for_project(conn, "p2")["total"] == 0


def test_grant_rejects_unknown_action_and_project(conn):
    listing = _listing(conn)
    with pytest.raises(catalog.CatalogError) as exc:
        catalog.grant(conn, listing["id"], {
            "target_project_id": "p2", "actions": ["delete"], "purpose": "x",
        }, granted_by="alice")
    assert exc.value.code == "invalid_payload"

    with pytest.raises(catalog.CatalogError) as exc2:
        catalog.grant(conn, listing["id"], {
            "target_project_id": "nope", "actions": ["reference"], "purpose": "x",
        }, granted_by="alice")
    assert exc2.value.code == "invalid_payload"
