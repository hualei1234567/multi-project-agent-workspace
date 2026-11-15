"""能力目录 V1（P4-M8 P1）：上架、申请、授权与撤销。

边界（V6.0 §19）：

* 目录**只存引用**（类型 + 引用 ID + 版本 + 摘要），不复制 Registry 正文；
* 授权**不扩大数据权限**：拿到某个 Workflow 的引用权限，不等于能读源项目数据；
* 授权必须带用途与有效期，可撤销，撤销后立即不再出现在可用列表里。
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .. import store
from . import audited, events

CAPABILITY_TYPES = ("agent", "skill", "workflow", "domain_pack")
GRANT_ACTIONS = ("reference", "clone", "execute")


class CatalogError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def resolve_ref(conn: sqlite3.Connection, capability_type: str, ref_id: str,
                ref_version: str, *, source_project_id: str) -> dict[str, Any]:
    """把 ref 解析到真实资源（R3 / M8）。

    每种能力类型对应一张真实表：
      agent       → project_agents.role_key / id
      skill       → 项目仓库 .claude/skills（无法在库里查，标 unresolved 但给原因）
      workflow    → workflow_versions.id（且版本属于 source_project）
      domain_pack → knowledge_items.id

    **悬空引用不允许上架**：审计指出 ``ref_id`` 原来只是自由字符串，
    上架一个根本不存在的资源也能成功，目录于是变成一张许愿单。
    """
    rid = (ref_id or "").strip()
    ver = (ref_version or "").strip()
    if not rid:
        return {"resolved": False, "reason": "ref_id 为空"}

    if capability_type == "agent":
        row = conn.execute(
            "SELECT id, role_key, project_id FROM project_agents"
            " WHERE project_id = ? AND (id = ? OR role_key = ?)",
            (source_project_id, rid, rid),
        ).fetchone()
        if row is None:
            return {"resolved": False, "reason": f"项目里没有 agent {rid}"}
        return {"resolved": True, "kind": "project_agent", "id": row["id"]}

    if capability_type == "workflow":
        row = conn.execute(
            "SELECT v.id, v.version, d.project_id FROM workflow_versions v"
            " JOIN workflow_definitions d ON d.id = v.definition_id"
            " WHERE v.id = ? OR (v.version = ? AND d.project_id = ? AND d.def_key = ?)",
            (rid, ver, source_project_id, rid),
        ).fetchone()
        if row is None:
            return {"resolved": False, "reason": f"没有匹配的 workflow 版本 {rid}@{ver}"}
        if row["project_id"] != source_project_id:
            return {"resolved": False,
                    "reason": f"workflow 属于项目 {row['project_id']}，不是本项目"}
        return {"resolved": True, "kind": "workflow_version", "id": row["id"]}

    if capability_type == "domain_pack":
        row = conn.execute(
            "SELECT id, project_id FROM knowledge_items WHERE id = ?",
            (rid,),
        ).fetchone()
        if row is None:
            return {"resolved": False, "reason": f"知识条目 {rid} 不存在"}
        if row["project_id"] != source_project_id:
            return {"resolved": False, "reason": "知识条目不属于本项目"}
        return {"resolved": True, "kind": "knowledge_item", "id": row["id"]}

    if capability_type == "skill":
        # Skill 是文件系统里的东西，平台库没有索引。这里如实标「无法在库内解析」，
        # 而不是假设它存在——假装解析成功等于把悬空引用洗白。
        return {"resolved": False,
                "reason": "skill 定义在仓库文件里，平台库无法验证；"
                          "上架需人工确认（不会因此被拒，但会标为未解析）",
                "soft": True}

    return {"resolved": False, "reason": f"未知能力类型 {capability_type}"}


@audited("capability.listing.publish", "capability_listing", id_arg="project_id")
def publish_listing(conn: sqlite3.Connection, project_id: str,
                    payload: dict[str, Any]) -> dict[str, Any]:
    ctype = str(payload.get("capability_type") or "").strip()
    ref_id = str(payload.get("ref_id") or "").strip()
    ref_version = str(payload.get("ref_version") or "").strip()
    name = str(payload.get("name") or "").strip()
    if ctype not in CAPABILITY_TYPES:
        raise CatalogError("invalid_payload", 422, f"capability_type 必须是 {CAPABILITY_TYPES}")
    if not ref_id or not ref_version or not name:
        raise CatalogError("invalid_payload", 422, "ref_id / ref_version / name 必填")

    # R3 / M8：上架前必须能解析到真实资源，否则目录里全是悬空引用。
    ref = resolve_ref(conn, ctype, ref_id, ref_version, source_project_id=project_id)
    if not ref["resolved"] and not ref.get("soft"):
        raise CatalogError("dangling_ref", 422, f"引用无法解析：{ref['reason']}")

    listing_id = _new_id("cap")
    now = store.now_iso()
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO capability_listings (id, source_project_id, capability_type,"
                " ref_id, ref_version, name, summary, status, owner, created_at, updated_at,"
                " ref_resolved, ref_checked_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'published', ?, ?, ?, ?, ?)",
                (listing_id, project_id, ctype, ref_id, ref_version, name,
                 payload.get("summary"), payload.get("owner"), now, now,
                 1 if ref["resolved"] else 0, now),
            )
            events.emit(conn, "capability.listing.published", project_id=project_id,
                        resource_type="capability_listing", resource_id=listing_id)
    except sqlite3.IntegrityError as exc:
        raise CatalogError("conflict", 409, "该能力版本已上架") from exc
    return get_listing(conn, listing_id)


def get_listing(conn: sqlite3.Connection, listing_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM capability_listings WHERE id = ?", (listing_id,)
    ).fetchone()
    if row is None:
        raise CatalogError("not_found", 404, "能力条目不存在")
    return dict(row)


def available_for_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """本项目可用的能力：自己上架的 + 别人授权给本项目的（且未撤销未过期）。"""
    now = store.now_iso()
    rows = conn.execute(
        """
        SELECT l.*, g.id AS grant_id, g.actions_json, g.expires_at
        FROM capability_listings l
        LEFT JOIN capability_grants g
               ON g.listing_id = l.id AND g.target_project_id = ?
              AND g.revoked_at IS NULL
              AND (g.expires_at IS NULL OR g.expires_at > ?)
        WHERE l.status = 'published'
          AND (l.source_project_id = ? OR g.id IS NOT NULL)
        ORDER BY l.created_at DESC
        """,
        (project_id, now, project_id),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["from_own_project"] = item["source_project_id"] == project_id
        item["actions"] = store.loads_json(item.pop("actions_json"), []) or []
        items.append(item)
    return {"items": items, "total": len(items)}


@audited("capability.grant.create", "capability_grant", id_arg="listing_id")
def grant(conn: sqlite3.Connection, listing_id: str, payload: dict[str, Any],
          *, granted_by: str) -> dict[str, Any]:
    target = str(payload.get("target_project_id") or "").strip()
    actions = payload.get("actions") or ["reference"]
    if not target:
        raise CatalogError("invalid_payload", 422, "target_project_id 必填")
    if not isinstance(actions, list) or not actions:
        raise CatalogError("invalid_payload", 422, "actions 不能为空")
    for action in actions:
        if action not in GRANT_ACTIONS:
            raise CatalogError("invalid_payload", 422, f"非法动作：{action}")
    if not str(payload.get("purpose") or "").strip():
        raise CatalogError("purpose_required", 422, "授权必须写清用途")

    listing = get_listing(conn, listing_id)
    if listing["status"] != "published":
        raise CatalogError("invalid_state", 409, "只能授权已上架能力")
    if conn.execute("SELECT 1 FROM agent_projects WHERE id = ?", (target,)).fetchone() is None:
        raise CatalogError("invalid_payload", 422, "目标项目不存在")

    grant_id = _new_id("grant")
    with store.write_tx(conn):
        conn.execute(
            "INSERT INTO capability_grants (id, listing_id, target_project_id,"
            " actions_json, purpose, granted_by, expires_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (grant_id, listing_id, target, store.dumps_json(actions),
             payload.get("purpose"), granted_by, payload.get("expires_at"),
             store.now_iso()),
        )
        events.emit(conn, "capability.listing.granted",
                    project_id=listing["source_project_id"],
                    resource_type="capability_grant", resource_id=grant_id,
                    payload={"target_project_id": target, "actions": actions})
    return get_grant(conn, grant_id)


def get_grant(conn: sqlite3.Connection, grant_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM capability_grants WHERE id = ?", (grant_id,)
    ).fetchone()
    if row is None:
        raise CatalogError("not_found", 404, "授权不存在")
    item = dict(row)
    item["actions"] = store.loads_json(item.pop("actions_json"), []) or []
    return item


@audited("capability.grant.revoke", "capability_grant", id_arg="grant_id")
def revoke_grant(conn: sqlite3.Connection, grant_id: str, *, reason: str,
                 revoked_by: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM capability_grants WHERE id = ?", (grant_id,)
    ).fetchone()
    if row is None:
        raise CatalogError("not_found", 404, "授权不存在")
    if row["revoked_at"]:
        return {"grant_id": grant_id, "status": "already_revoked"}
    with store.write_tx(conn):
        conn.execute(
            "UPDATE capability_grants SET revoked_at = ? WHERE id = ?",
            (store.now_iso(), grant_id),
        )
        events.emit(conn, "capability.listing.revoked",
                    resource_type="capability_grant", resource_id=grant_id,
                    payload={"reason": reason, "revoked_by": revoked_by})
    return {"grant_id": grant_id, "status": "revoked"}


def list_grants(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT g.*, l.name, l.capability_type FROM capability_grants g"
        " JOIN capability_listings l ON l.id = g.listing_id"
        " WHERE l.source_project_id = ? ORDER BY g.created_at DESC", (project_id,),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["actions"] = store.loads_json(item.pop("actions_json"), []) or []
        items.append(item)
    return {"items": items, "total": len(items)}
