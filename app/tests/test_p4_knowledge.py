"""知识域 V1：Secret 阻断、项目内检索、撤销后不可命中。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_platform import store
from agent_platform.p4 import knowledge


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(tmp_path / "platform.db"))
    conn = store.connect()
    store.init_db(conn)
    for pid in ("p1", "p2"):
        conn.execute(
            "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
            " VALUES (?, ?, ?, '/tmp/repo', '2026-11-06T00:00:00+00:00',"
            " '2026-11-06T00:00:00+00:00')", (pid, pid, pid),
        )
    conn.commit()
    yield conn
    conn.close()


def _source(conn, project_id="p1", title="手册", classification="Internal"):
    return knowledge.create_source(conn, project_id, {
        "title": title, "kind": "note", "classification": classification,
        "license": "internal-only" if classification == "Confidential" else None,
    })


def _published_item(conn, project_id="p1", content="部署流程：先跑 pytest 再上线。\n\n回滚：git revert。"):
    source = _source(conn, project_id)
    item = knowledge.create_item(conn, project_id, {
        "title": "部署手册", "content": content, "source_id": source["id"],
    })
    version_id = item["versions"][0]["id"]
    knowledge.publish_version(conn, version_id, reviewed_by="reviewer")
    return item, version_id


def test_confidential_source_requires_license(conn):
    with pytest.raises(knowledge.KnowledgeError) as exc:
        knowledge.create_source(conn, "p1", {
            "title": "机密", "classification": "Confidential",
        })
    assert exc.value.code == "license_required"


def test_secret_content_is_blocked_before_storage(conn):
    source = _source(conn)
    with pytest.raises(knowledge.KnowledgeError) as exc:
        knowledge.create_item(conn, "p1", {
            "title": "泄露", "source_id": source["id"],
            "content": "key: sk-ABCDEFGHIJKLMNOPQRSTUVWX1234",
        })
    assert exc.value.code == "secret_detected"
    # 内容不得落库
    assert conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0] == 0


def test_publish_indexes_and_search_hits(conn):
    _, version_id = _published_item(conn)
    result = knowledge.search(conn, "p1", "pytest", principal_id="u1")
    assert result["total"] == 1
    assert result["hits"][0]["version_id"] == version_id
    assert "pytest" in result["hits"][0]["snippet"]
    # 检索留痕
    trace = conn.execute("SELECT * FROM retrieval_traces").fetchone()
    assert trace["project_id"] == "p1"


def test_unpublished_item_not_searchable(conn):
    source = _source(conn)
    knowledge.create_item(conn, "p1", {
        "title": "草稿", "content": "这是一段还没发布的独特内容 zeta", "source_id": source["id"],
    })
    result = knowledge.search(conn, "p1", "zeta")
    assert result["total"] == 0


def test_search_is_project_scoped(conn):
    _published_item(conn, "p1")
    assert knowledge.search(conn, "p2", "pytest")["total"] == 0
    assert knowledge.search(conn, "p1", "pytest")["total"] == 1


def test_revoke_removes_index_immediately(conn):
    _, version_id = _published_item(conn)
    assert knowledge.search(conn, "p1", "pytest")["total"] == 1

    result = knowledge.revoke_version(
        conn, version_id, reason="内容过期", revoked_by="owner"
    )
    assert result["chunks_removed"] >= 1
    assert knowledge.search(conn, "p1", "pytest")["total"] == 0
    assert conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0] == 0


def test_item_classification_cannot_be_lower_than_source(conn):
    source = _source(conn, classification="Confidential")
    with pytest.raises(knowledge.KnowledgeError) as exc:
        knowledge.create_item(conn, "p1", {
            "title": "降级", "source_id": source["id"],
            "content": "普通内容", "classification": "Public",
        })
    assert exc.value.code == "classification_too_low"


def test_source_from_other_project_rejected(conn):
    source = _source(conn, "p2")
    with pytest.raises(knowledge.KnowledgeError) as exc:
        knowledge.create_item(conn, "p1", {
            "title": "越界", "source_id": source["id"], "content": "内容",
        })
    assert exc.value.code == "forbidden"


def test_chunking_splits_long_text(conn):
    long_text = "\n\n".join(f"段落{i}" + "内容" * 200 for i in range(3))
    chunks = knowledge.chunk_text(long_text, max_chars=600)
    assert len(chunks) >= 3
    assert all(len(c) <= 600 for c in chunks)
