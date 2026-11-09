"""跨机器窗口会话：把各台机器上「已经打开的 Claude CLI 窗口」登记到平台，并向指定窗口下发指令。

背景与边界（务必保持诚实）：

* 平台运行在服务器上，用户的 VSCode 窗口通常开在另一台机器（例如 Mac）。
  ``claude agents --json`` 只能看到本机会话，所以需要一个本机桥接进程
  （``scripts/session-bridge.py``）上报会话、领取指令并执行。
* Claude Code 没有「往正在运行的 TUI 里注入文字」的能力。下发走
  ``claude --bg --resume <session-id>``：同一会话继续时沿用原 id；会话正在运行时
  CLI 会另起一个副本并打印新 id——这不是同一个窗口，必须如实回传给用户。
* 因此「平台下发」与「窗口里自己输入」共享同一份会话记录（同一 sessionId 或同源副本），
  但不会实时出现在已打开的窗口里；这一点在 UI 上要写清楚，不能假装是实时注入。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import store

#: 桥接超过这个时间没上报，就认为该机器的会话信息已过期。
ONLINE_SECONDS = 90
COMMAND_STATUSES = ("pending", "dispatched", "done", "failed")


class WindowError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM agent_projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise WindowError("not_found", 404, "项目不存在")
    return dict(row)


# ---------------------------------------------------------------------------
# 桥接上报
# ---------------------------------------------------------------------------
def report_sessions(device_id: str, sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """桥接定期上报本机打开/运行的 Claude 会话（交互式与后台都会列出）。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        raise WindowError("invalid_payload", 422, "缺少 device_id")
    if not isinstance(sessions, list):
        raise WindowError("invalid_payload", 422, "sessions 必须是数组")
    now = _now()
    accepted = 0
    with store.session() as conn:
        for item in sessions:
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("sessionId") or item.get("session_id") or "").strip()
            if not session_id:
                continue
            project_id = str(item.get("project_id") or "").strip() or None
            conn.execute(
                """
                INSERT INTO device_sessions
                    (id, device_id, session_id, name, cwd, kind, status, pid, project_id, reported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, session_id) DO UPDATE SET
                    name = excluded.name, cwd = excluded.cwd, kind = excluded.kind,
                    status = excluded.status, pid = excluded.pid,
                    project_id = excluded.project_id, reported_at = excluded.reported_at
                """,
                (
                    store.stable_id("ds", device_id, session_id),
                    device_id,
                    session_id,
                    str(item.get("name") or "") or None,
                    str(item.get("cwd") or "") or None,
                    str(item.get("kind") or "") or None,
                    str(item.get("status") or "") or None,
                    int(item["pid"]) if str(item.get("pid") or "").isdigit() else None,
                    project_id,
                    now,
                ),
            )
            accepted += 1
        # 桥接没再上报的会话标记为离线：不删除历史，只让页面显示「窗口已关闭」
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ONLINE_SECONDS)).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE device_sessions SET status = 'offline' WHERE device_id = ? AND reported_at < ?",
            (device_id, cutoff),
        )
    return {"device_id": device_id, "accepted": accepted, "reported_at": now}


# ---------------------------------------------------------------------------
# 远端会话 trace（桥接上报）
# ---------------------------------------------------------------------------
def report_traces(device_id: str, traces: list[dict[str, Any]]) -> dict[str, Any]:
    """桥接上报远端窗口的会话 trace，平台才能「进去看」别的机器上的会话。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        raise WindowError("invalid_payload", 422, "缺少 device_id")
    if not isinstance(traces, list):
        raise WindowError("invalid_payload", 422, "traces 必须是数组")
    now = _now()
    accepted = 0
    with store.session() as conn:
        for item in traces:
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("session_id") or "").strip()
            payload = item.get("payload")
            if not session_id or not isinstance(payload, dict):
                continue
            conn.execute(
                """
                INSERT INTO device_traces
                    (id, device_id, session_id, project_id, payload_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, session_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    store.stable_id("dt", device_id, session_id),
                    device_id, session_id,
                    str(item.get("project_id") or "") or None,
                    store.dumps_json(payload), now,
                ),
            )
            accepted += 1
    return {"device_id": device_id, "accepted": accepted, "reported_at": now}


def device_trace(conn: sqlite3.Connection, project_id: str, session_id: str) -> dict[str, Any] | None:
    """某个远端窗口会话的最后一份 trace（没有就返回 None）。"""
    row = conn.execute(
        "SELECT * FROM device_traces WHERE project_id = ? AND session_id = ?"
        " ORDER BY updated_at DESC LIMIT 1",
        (project_id, session_id),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM device_traces WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    payload = store.loads_json(row["payload_json"], {}) or {}
    payload["_device_id"] = row["device_id"]
    payload["_updated_at"] = row["updated_at"]
    return payload


def window_sessions(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    """项目下已登记的远端窗口会话（用于并入「实时会话」列表）。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ONLINE_SECONDS)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM device_sessions WHERE project_id = ? ORDER BY reported_at DESC",
        (project_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["online"] = str(item.get("reported_at") or "") >= cutoff
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
#: 绑定存在项目 settings_json 里：哪个窗口是该项目的「总负责人主会话」。
BIND_SETTING = "team_window"


def bind_window(project_id: str, device_id: str, session_id: str, *,
                actor: str = "local") -> dict[str, Any]:
    """把某个窗口绑定为项目的总负责人主会话（General Owner 就是它）。"""
    with store.session() as conn:
        project = _require_project(conn, project_id)
        target = conn.execute(
            "SELECT * FROM device_sessions WHERE device_id = ? AND session_id = ?",
            (device_id, session_id),
        ).fetchone()
        if target is None:
            raise WindowError("not_found", 404, "该窗口未登记；请先在对应机器启动桥接")
        settings = dict(_settings(project))
        settings[BIND_SETTING] = {
            "device_id": device_id,
            "session_id": session_id,
            "bound_at": _now(),
            "actor": actor,
        }
        conn.execute(
            "UPDATE agent_projects SET settings_json = ?, updated_at = ? WHERE id = ?",
            (store.dumps_json(settings), _now(), project_id),
        )
    return {"project_id": project_id, "bound": settings[BIND_SETTING]}


def bound_window(conn: sqlite3.Connection, project: dict[str, Any]) -> dict[str, Any] | None:
    """项目绑定的总负责人窗口 + 该窗口的实时状态（离线也返回，便于页面显示「已离线」）。"""
    bound = _bound(project)
    if not bound:
        return None
    row = conn.execute(
        "SELECT * FROM device_sessions WHERE device_id = ? AND session_id = ?",
        (bound.get("device_id"), bound.get("session_id")),
    ).fetchone()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ONLINE_SECONDS)).isoformat(timespec="seconds")
    live = dict(row) if row else {}
    return {
        **bound,
        "name": live.get("name"),
        "status": live.get("status"),
        "pid": live.get("pid"),
        "cwd": live.get("cwd"),
        "online": bool(live) and str(live.get("reported_at") or "") >= cutoff,
    }


def _settings(project: dict[str, Any]) -> dict[str, Any]:
    """settings_json 可能是字符串，也可能已经被上层解析成 dict。"""
    raw = project.get("settings_json")
    if isinstance(raw, dict):
        return raw
    return store.loads_json(raw, {}) or {}


def _bound(project: dict[str, Any]) -> dict[str, Any] | None:
    settings = _settings(project)
    value = settings.get(BIND_SETTING)
    return value if isinstance(value, dict) else None


def list_windows(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    """某项目可见的所有窗口会话 + 待执行/最近指令。"""
    project = _require_project(conn, project_id)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ONLINE_SECONDS)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM device_sessions WHERE project_id = ? OR (project_id IS NULL AND cwd IS NOT NULL)"
        " ORDER BY reported_at DESC",
        (project_id,),
    ).fetchall()
    repo = str(project.get("repo_root") or "")
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        cwd = str(item.get("cwd") or "")
        # 桥接没带 project_id 时，用 cwd 前缀兜底
        owned = item.get("project_id") == project_id or (repo and cwd.startswith(repo))
        if not owned:
            continue
        item["online"] = str(item.get("reported_at") or "") >= cutoff
        item["label"] = item.get("name") or f"会话 {str(item.get('session_id'))[:8]}"
        items.append(item)
    commands = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM device_commands WHERE project_id = ? ORDER BY created_at DESC LIMIT 20",
            (project_id,),
        )
    ]
    bound = _bound(project)
    if bound:
        live = next(
            (it for it in items
             if it.get("device_id") == bound.get("device_id")
             and it.get("session_id") == bound.get("session_id")),
            None,
        )
        bound = {**bound, "online": bool(live and live.get("online")),
                 "name": (live or {}).get("name"), "status": (live or {}).get("status"),
                 "pid": (live or {}).get("pid"), "cwd": (live or {}).get("cwd")}
    return {
        "project_id": project_id,
        "repo_root": repo,
        "online_seconds": ONLINE_SECONDS,
        "bound": bound,
        "windows": items,
        "commands": commands,
        "note": (
            "平台下发走 claude --bg --resume：同一会话继续沿用原 id；"
            "若该窗口正在运行，CLI 会另起一个副本（同源上下文，但不是同一个窗口）。"
            "窗口里自己输入的内容与平台下发共享同一会话记录。"
        ),
    }


# ---------------------------------------------------------------------------
# 指令队列
# ---------------------------------------------------------------------------
def enqueue_command(
    project_id: str,
    device_id: str,
    session_id: str,
    instruction: str,
    *,
    actor: str = "local",
) -> dict[str, Any]:
    """平台侧下发：写一条指令给桥接，由桥接在那台机器上执行。"""
    text = str(instruction or "").strip()
    if not text:
        raise WindowError("invalid_payload", 422, "指令不能为空")
    if len(text) > 8000:
        raise WindowError("invalid_payload", 422, "指令过长（上限 8000 字符）")
    with store.session() as conn:
        _require_project(conn, project_id)
        target = conn.execute(
            "SELECT * FROM device_sessions WHERE device_id = ? AND session_id = ?",
            (device_id, session_id),
        ).fetchone()
        if target is None:
            raise WindowError("not_found", 404, "该窗口未登记，请先在对应机器上启动桥接")
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ONLINE_SECONDS)).isoformat(timespec="seconds")
        if str(target["reported_at"] or "") < cutoff:
            raise WindowError("window_offline", 409, "该窗口已离线（桥接超过 90 秒未上报）")
        command_id = store.stable_id("cmd", project_id, device_id, session_id, text, _now())
        conn.execute(
            "INSERT INTO device_commands"
            " (id, device_id, project_id, session_id, instruction, status, created_at, actor)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (command_id, device_id, project_id, session_id, text, _now(), actor),
        )
    return {"id": command_id, "status": "pending", "device_id": device_id, "session_id": session_id}


def pull_commands(device_id: str, limit: int = 10) -> dict[str, Any]:
    """桥接领取待执行指令；领取即置为 dispatched，避免重复执行。"""
    device_id = str(device_id or "").strip()
    if not device_id:
        raise WindowError("invalid_payload", 422, "缺少 device_id")
    limit = max(1, min(int(limit or 10), 50))
    taken: list[dict[str, Any]] = []
    with store.session() as conn:
        rows = conn.execute(
            "SELECT * FROM device_commands WHERE device_id = ? AND status = 'pending'"
            " ORDER BY created_at ASC LIMIT ?",
            (device_id, limit),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE device_commands SET status = 'dispatched', started_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
            taken.append(dict(row))
    return {"device_id": device_id, "commands": taken}


def command_result(
    device_id: str,
    command_id: str,
    *,
    ok: bool,
    session_id: str | None = None,
    result: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """桥接回报执行结果；``session_id`` 是实际被续跑的会话（可能是副本）。"""
    with store.session() as conn:
        row = conn.execute(
            "SELECT * FROM device_commands WHERE id = ? AND device_id = ?",
            (command_id, device_id),
        ).fetchone()
        if row is None:
            raise WindowError("not_found", 404, "指令不存在")
        status = "done" if ok else "failed"
        conn.execute(
            "UPDATE device_commands SET status = ?, finished_at = ?, result = ?, error = ?,"
            " session_id = COALESCE(?, session_id) WHERE id = ?",
            (status, _now(), (result or "")[:4000], (error or "")[:2000], session_id, command_id),
        )
    return {"id": command_id, "status": status}
