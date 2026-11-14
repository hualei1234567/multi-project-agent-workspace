"""知识域 V1：来源 / 条目 / 版本 / 分块 / 项目内检索（P4-M2）。

三条硬约束（对 V6.0 §13/§22）：

1. **Secret 不入库**：候选内容先过扫描，命中即拒绝写入，不打码后入库；
2. **检索引擎里做权限过滤**：FTS 查询自带 ``project_id`` 过滤条件，
   不接受"先全局召回、再由调用方筛"的用法；
3. **撤销与索引删除同事务**：撤销后新请求不能再命中（含缓存与导出）。
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from typing import Any

from .. import store
from . import audited, events

VALID_CLASSIFICATIONS = ("Public", "Internal", "Confidential")
VALID_SOURCE_KINDS = ("note", "file", "url", "transcript")

#: Secret 检测：宁可误杀，不可漏放。命中即拒绝入库。
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE)),
    ("password_assign", re.compile(r"(?i)\bpassword\s*[:=]\s*\S{6,}")),
    ("ssh_private", re.compile(r"-----BEGIN OPENSSH PRIVATE KEY-----")),
)

CHUNK_MAX_CHARS = 600


class KnowledgeError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def scan_secrets(text: str) -> list[str]:
    """返回命中的敏感模式名列表。空列表表示干净。"""
    hits: list[str] = []
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text or ""):
            hits.append(name)
    return hits


def chunk_text(text: str, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """按段落聚合成块，块不超过 max_chars。空文本返回空列表。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(para) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(para), max_chars):
                chunks.append(para[i:i + max_chars])
            continue
        if len(buf) + len(para) + 2 > max_chars:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


# --------------------------------------------------------------------------
# 来源与条目
# --------------------------------------------------------------------------
def create_source(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    if not title:
        raise KnowledgeError("invalid_payload", 422, "title 必填")
    kind = str(payload.get("kind") or "note")
    if kind not in VALID_SOURCE_KINDS:
        raise KnowledgeError("invalid_payload", 422, f"kind 必须是 {VALID_SOURCE_KINDS}")
    classification = str(payload.get("classification") or "Internal")
    if classification not in VALID_CLASSIFICATIONS:
        raise KnowledgeError("invalid_payload", 422, "classification 非法")
    license_text = payload.get("license")
    if classification == "Confidential" and not license_text:
        raise KnowledgeError("license_required", 422, "Confidential 来源必须登记许可")

    source_id = _new_id("ksrc")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO knowledge_sources (id, project_id, kind, uri, title,"
            " classification, license, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, project_id, kind, payload.get("uri"), title, classification,
             license_text, str(payload.get("created_by") or "local"), store.now_iso()),
        )
    return get_source(conn, source_id)


def get_source(conn: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM knowledge_sources WHERE id = ?", (source_id,)).fetchone()
    if row is None:
        raise KnowledgeError("not_found", 404, "知识来源不存在")
    return dict(row)


def list_sources(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM knowledge_sources WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


def create_item(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """建条目 + 第一版候选内容。内容必须通过 Secret 扫描。"""
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content") or "").strip()
    source_id = str(payload.get("source_id") or "").strip()
    if not title or not content or not source_id:
        raise KnowledgeError("invalid_payload", 422, "title / content / source_id 必填")

    source = get_source(conn, source_id)
    if source["project_id"] != project_id:
        raise KnowledgeError("forbidden", 403, "来源不属于该项目")
    if source["status"] != "active":
        raise KnowledgeError("source_inactive", 409, "来源已停用")

    hits = scan_secrets(content)
    if hits:
        raise KnowledgeError(
            "secret_detected", 422, f"内容命中敏感信息，已阻断入库：{', '.join(hits)}"
        )

    # 条目密级不得低于来源密级
    order = {name: i for i, name in enumerate(VALID_CLASSIFICATIONS)}
    classification = str(payload.get("classification") or source["classification"])
    if order.get(classification, 0) < order.get(source["classification"], 0):
        raise KnowledgeError("classification_too_low", 422, "条目密级不能低于来源密级")

    now = store.now_iso()
    item_id = _new_id("kitem")
    version_id = _new_id("kver")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO knowledge_items (id, project_id, source_id, title, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, 'draft', ?, ?)",
            (item_id, project_id, source_id, title, now, now),
        )
        conn.execute(
            "INSERT INTO knowledge_versions (id, item_id, version, content, content_digest,"
            " classification, status, created_at) VALUES (?, ?, 1, ?, ?, ?, 'candidate', ?)",
            (version_id, item_id, content, store.stable_id(item_id, content),
             classification, now),
        )
    return get_item(conn, item_id)


def get_item(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise KnowledgeError("not_found", 404, "知识条目不存在")
    item = dict(row)
    item["versions"] = [
        dict(v) for v in conn.execute(
            "SELECT id, version, status, classification, content_digest, created_at,"
            " published_at, revoked_at FROM knowledge_versions WHERE item_id = ?"
            " ORDER BY version DESC", (item_id,),
        )
    ]
    return item


def list_items(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id, title, status, source_id, current_version_id, created_at"
        " FROM knowledge_items WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return {"items": [dict(row) for row in rows], "total": len(rows)}


# --------------------------------------------------------------------------
# 发布 / 撤销
# --------------------------------------------------------------------------
@audited("knowledge.version.publish", "knowledge_version", id_arg="version_id")
def publish_version(conn: sqlite3.Connection, version_id: str, *, reviewed_by: str) -> dict[str, Any]:
    """审核通过并发布：建块 + 建索引。"""
    row = conn.execute(
        "SELECT v.*, i.project_id, i.id AS item_id FROM knowledge_versions v"
        " JOIN knowledge_items i ON i.id = v.item_id WHERE v.id = ?", (version_id,)
    ).fetchone()
    if row is None:
        raise KnowledgeError("not_found", 404, "知识版本不存在")
    if row["status"] not in {"candidate", "approved"}:
        raise KnowledgeError("invalid_state", 409, f"当前状态 {row['status']} 不能发布")

    hits = scan_secrets(row["content"])
    if hits:
        raise KnowledgeError("secret_detected", 422, f"发布前复检命中：{', '.join(hits)}")

    now = store.now_iso()
    chunks = chunk_text(row["content"])
    with store.write_tx(conn):
        conn.execute(
            "UPDATE knowledge_versions SET status = 'published', reviewed_by = ?,"
            " published_at = ? WHERE id = ?", (reviewed_by, now, version_id),
        )
        conn.execute(
            "UPDATE knowledge_items SET status = 'published', current_version_id = ?,"
            " updated_at = ? WHERE id = ?", (version_id, now, row["item_id"]),
        )
        for seq, text in enumerate(chunks):
            chunk_id = _new_id("kchunk")
            conn.execute(
                "INSERT INTO knowledge_chunks (id, version_id, seq, content) VALUES (?, ?, ?, ?)",
                (chunk_id, version_id, seq, text),
            )
            conn.execute(
                "INSERT INTO knowledge_fts (chunk_id, project_id, version_id, content)"
                " VALUES (?, ?, ?, ?)",
                (chunk_id, row["project_id"], version_id, text),
            )
        events.emit(conn, "knowledge.version.published", project_id=row["project_id"],
                    resource_type="knowledge_version", resource_id=version_id,
                    payload={"chunks": len(chunks)})
    return {"version_id": version_id, "status": "published", "chunks": len(chunks)}


@audited("knowledge.version.revoke", "knowledge_version", id_arg="version_id")
def revoke_version(conn: sqlite3.Connection, version_id: str, *, reason: str,
                   revoked_by: str) -> dict[str, Any]:
    """撤销：状态置 revoked 并**在同一事务内**删除分块与索引。"""
    row = conn.execute(
        "SELECT v.*, i.project_id, i.id AS item_id FROM knowledge_versions v"
        " JOIN knowledge_items i ON i.id = v.item_id WHERE v.id = ?", (version_id,)
    ).fetchone()
    if row is None:
        raise KnowledgeError("not_found", 404, "知识版本不存在")
    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "UPDATE knowledge_versions SET status = 'revoked', revoked_at = ?,"
            " revoke_reason = ? WHERE id = ?", (now, reason, version_id),
        )
        deleted = conn.execute(
            "DELETE FROM knowledge_chunks WHERE version_id = ?", (version_id,)
        ).rowcount
        conn.execute("DELETE FROM knowledge_fts WHERE version_id = ?", (version_id,))
        conn.execute(
            "UPDATE knowledge_items SET status = 'revoked', updated_at = ?"
            " WHERE id = ? AND current_version_id = ?", (now, row["item_id"], version_id),
        )
        events.emit(conn, "knowledge.version.revoked", project_id=row["project_id"],
                    resource_type="knowledge_version", resource_id=version_id,
                    payload={"reason": reason, "revoked_by": revoked_by,
                             "chunks_removed": max(deleted, 0)})
    return {"version_id": version_id, "status": "revoked", "chunks_removed": max(deleted, 0)}


# --------------------------------------------------------------------------
# 检索
# --------------------------------------------------------------------------
def _fts_query(text: str) -> str:
    """把用户输入转成安全的 FTS5 查询：整体加引号，避免语法注入。"""
    cleaned = re.sub(r'["\']', " ", text or "").strip()
    tokens = [tok for tok in cleaned.split() if tok]
    if not tokens:
        return ""
    return " ".join(f'"{tok}"' for tok in tokens)


def search(conn: sqlite3.Connection, project_id: str, query: str, *,
           limit: int = 10, principal_id: str | None = None,
           correlation_id: str | None = None) -> dict[str, Any]:
    """项目内检索。SQL 自带 project_id 过滤，不存在"先全局召回"。"""
    match = _fts_query(query)
    if not match:
        raise KnowledgeError("invalid_payload", 422, "query 不能为空")
    limit = max(1, min(50, int(limit)))

    rows = conn.execute(
        "SELECT f.chunk_id, f.version_id, f.content,"
        "       v.item_id, v.version, v.classification, i.title"
        " FROM knowledge_fts f"
        " JOIN knowledge_versions v ON v.id = f.version_id"
        " JOIN knowledge_items i ON i.id = v.item_id"
        " WHERE knowledge_fts MATCH ? AND f.project_id = ?"
        "   AND v.status = 'published'"
        " ORDER BY rank LIMIT ?",
        (match, project_id, limit),
    ).fetchall()

    hits = [
        {
            "chunk_id": row["chunk_id"],
            "item_id": row["item_id"],
            "version_id": row["version_id"],
            "version": row["version"],
            "title": row["title"],
            "classification": row["classification"],
            "snippet": row["content"][:300],
        }
        for row in rows
    ]
    trace_id = _new_id("rtrace")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO retrieval_traces (id, project_id, principal_id, query,"
            " hits_json, correlation_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trace_id, project_id, principal_id, query, store.dumps_json(hits),
             correlation_id, store.now_iso()),
        )
    return {"query": query, "trace_id": trace_id, "hits": hits, "total": len(hits)}
