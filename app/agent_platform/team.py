"""项目 Agent Team：管理真实的 Claude Code 后台会话。

设计要点（对应用户要求）：

* 团队不是平台里模拟出来的角色，而是真实的 ``claude`` 进程：
  ``claude agents --json`` 读取真实会话，``claude --bg`` 启动，``claude --bg --resume``
  继续下达指令，``claude stop`` 停止。
* 平台只做三件事：启动、下达指令、观测。任务怎么拆、Subagent 怎么派、worktree
  怎么开，由项目里的 General Owner 会话自己决定。
* 默认关闭（``AGWS_PLATFORM_EXECUTOR=on`` 才允许启动），与执行器共用同一个总开关。
* 预检失败时返回可执行的修复提示，不假装成功。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from . import store

EXECUTOR_ENV = "AGWS_PLATFORM_EXECUTOR"
SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: `claude --bg` 打印的是短 id（backgrounded · 94eaba41 / claude attach 94eaba41）
SHORT_ID_RE = re.compile(r"backgrounded\s*·\s*([0-9a-zA-Z-]{6,64})")
ATTACH_ID_RE = re.compile(r"claude (?:attach|stop)\s+([0-9a-zA-Z-]{6,64})")
LAUNCH_TIMEOUT_S = 120
QUERY_TIMEOUT_S = 20
LOG_TIMEOUT_S = 25
#: 项目可以指定由哪个角色担任总负责人（settings.team_owner_agent）
OWNER_SETTING = "team_owner_agent"
DEFAULT_OWNER = "general-owner"
#: 平台托管的总负责人后台会话（claude --bg）。平台与用户输入的是同一个会话。
OWNER_SESSION_SETTING = "team_owner_session"


class TeamError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def executor_enabled() -> bool:
    import os

    return os.environ.get(EXECUTOR_ENV, "off").strip().lower() in {"1", "on", "true", "yes"}


def _claude() -> str:
    return shutil.which("claude") or ""


def _run(args: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False,
    )


def _agents(cwd: Path, *, include_finished: bool = False) -> tuple[list[dict[str, Any]], str | None]:
    """读取本机真实 Claude Code 会话（含交互式与后台）。

    ``include_finished`` 时带上已结束的后台会话：托管会话跑完一轮就会退出，
    但会话本身可继续，不能因此显示「不存在」。
    """
    binary = _claude()
    if not binary:
        return [], "未找到 claude CLI"
    args = [binary, "agents", "--json"]
    if include_finished:
        args.append("--all")
    try:
        proc = _run(args, cwd, QUERY_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"读取会话失败：{exc}"
    if proc.returncode != 0:
        return [], (proc.stderr or proc.stdout or "claude agents 失败").strip()[:300]
    try:
        data = json.loads(proc.stdout or "[]")
    except ValueError:
        return [], "claude agents 返回非 JSON"
    return data if isinstance(data, list) else [], None


def owner_agent(project: dict[str, Any]) -> str:
    settings = project.get("settings_json") or {}
    if isinstance(settings, str):
        settings = store.loads_json(settings, {})
    value = str((settings or {}).get(OWNER_SETTING) or "").strip()
    return value or DEFAULT_OWNER


def _trusted(repo: Path) -> bool | None:
    """读取 ``~/.claude.json`` 里的工作区信任标记；读不到返回 None（未知）。"""
    config = Path.home() / ".claude.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = (data.get("projects") or {}).get(str(repo)) or {}
    return bool(entry.get("hasTrustDialogAccepted"))


def _cli_session_id(repo: Path, session_id: str) -> str:
    """CLI 子命令（logs/stop）认的是短 id；从 agents 列表里换回短 id。"""
    sessions, _err = _agents(repo)
    for item in sessions:
        short = str(item.get("id") or "")
        full = str(item.get("sessionId") or "")
        if short == session_id or full == session_id or (full and full.startswith(session_id)):
            return short or session_id
    return session_id


def _resolve_session(repo: Path, session_id: str) -> str | None:
    """短 id → 全 id。CLI 打印短 id，agents --json 里才有完整 sessionId。"""
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", session_id or ""):
        return session_id
    sessions, _err = _agents(repo, include_finished=True)
    for item in sessions:
        full = str(item.get("sessionId") or "")
        if str(item.get("id") or "") == session_id or (full and full.startswith(session_id)):
            return full or None
    return None


def preflight(project: dict[str, Any]) -> dict[str, Any]:
    """启动前检查：CLI、仓库、开关、信任状态。每条都给出可执行的处理方式。"""
    repo = Path(str(project.get("repo_root") or "")).expanduser()
    checks: list[dict[str, Any]] = []

    binary = _claude()
    checks.append({
        "key": "cli", "label": "claude CLI", "ok": bool(binary),
        "detail": binary or "未找到 claude，请先安装 Claude Code",
    })
    repo_ok = repo.is_dir()
    checks.append({
        "key": "repo", "label": "项目仓库", "ok": repo_ok,
        "detail": str(repo) if repo_ok else f"{repo} 不存在",
    })
    enabled = executor_enabled()
    checks.append({
        "key": "switch", "label": "执行器开关", "ok": enabled,
        "detail": "已开启" if enabled else f"未开启：在服务端设置 {EXECUTOR_ENV}=on 后重启",
    })
    trusted = _trusted(repo) if repo_ok else None
    checks.append({
        "key": "trust", "label": "工作区信任",
        # 未知不拦（不同版本可能不写这个字段），但一定要提示
        "ok": trusted is not False,
        "detail": (
            "已信任" if trusted is True else
            f"未信任：在服务器终端执行 `cd {repo} && claude` 并接受一次提示" if trusted is False else
            "未读取到信任记录；若启动被拒，按 CLI 提示在该目录接受一次"
        ),
    })
    sessions, error = _agents(repo) if repo_ok and binary else ([], None)
    mine = [s for s in sessions if str(s.get("cwd") or "") == str(repo)]
    checks.append({
        "key": "sessions", "label": "会话读取", "ok": error is None,
        "detail": error or f"当前该项目有 {len(mine)} 个真实会话",
    })
    return {
        # 注意：不要用 ``ok`` 作为业务字段，HTTP 层的 ``ok`` 是错误包封口
        "ready": all(c["ok"] for c in checks),
        "project_id": project.get("id"),
        "repo_root": str(repo),
        "checks": checks,
        "sessions": mine,
        "hint": (
            None if repo_ok and binary
            else "首次在该目录后台运行前，需要先在终端里 cd 到仓库跑一次 claude 并接受信任提示"
        ),
    }


def owner_session_info(project: dict[str, Any]) -> dict[str, Any]:
    """平台托管的总负责人后台会话：是否还在跑、状态如何。

    这是「平台下发 = 同一个会话」的真正形态：会话由平台用 claude --bg 拉起，
    平台内可查看输出（trace）、继续对话（--bg --resume 同一 id）、结束。
    """
    settings = project.get("settings_json")
    settings = settings if isinstance(settings, dict) else store.loads_json(settings, {})
    saved = (settings or {}).get(OWNER_SESSION_SETTING)
    if not isinstance(saved, dict) or not saved.get("session_id"):
        return {"exists": False, "session_id": None, "status": None,
                "note": "还没有平台托管的会话；启动后平台与你的输入就是同一个会话。"}
    session_id = str(saved["session_id"])
    repo = Path(str(project.get("repo_root") or "")).expanduser()
    sessions, error = _agents(repo, include_finished=True) if repo.is_dir() else ([], "仓库不存在")

    def _matches(item: dict[str, Any]) -> bool:
        short = str(item.get("id") or "")
        full = str(item.get("sessionId") or "")
        return session_id in (short, full) or (full and full.startswith(session_id))

    found = next((s for s in sessions if _matches(s)), None)
    state = str((found or {}).get("status") or (found or {}).get("state") or "")
    live = state in ("busy", "idle", "running")
    if not found:
        note = "会话当前不在运行；「继续对话」会以 --bg --resume 在同一 id 上继续。"
    elif live:
        note = "会话在线：平台里「继续对话」与你在窗口里输入的是同一个会话。"
    else:
        note = "会话已结束一轮（可继续）：平台里「继续对话」会在同一 id 上接着聊。"
    return {
        "exists": True,
        "session_id": session_id,
        "agent": saved.get("agent"),
        "launched_at": saved.get("launched_at"),
        "running": live,
        "status": state or None,
        "kind": (found or {}).get("kind"),
        "pid": (found or {}).get("pid"),
        "error": error,
        "note": note,
    }


def team_status(project: dict[str, Any], conn) -> dict[str, Any]:
    """真实会话 + 平台记录的启动历史。"""
    repo = Path(str(project.get("repo_root") or "")).expanduser()
    sessions, error = _agents(repo) if repo.is_dir() else ([], "仓库不存在")
    mine = [s for s in sessions if str(s.get("cwd") or "") == str(repo)]
    rows = conn.execute(
        "SELECT id, project_id, session_id, agent, status, instruction, created_at, updated_at"
        " FROM team_sessions WHERE project_id = ? ORDER BY created_at DESC LIMIT 20",
        (project["id"],),
    ).fetchall()
    live_ids = {str(s.get("sessionId")) for s in mine}
    history = []
    for row in rows:
        item = dict(row)
        item["live"] = str(item.get("session_id") or "") in live_ids
        history.append(item)
    return {
        "project_id": project["id"],
        "repo_root": str(repo),
        "executor_enabled": executor_enabled(),
        "error": error,
        "total": len(mine),
        "running": sum(1 for s in mine
                       if (s.get("status") or s.get("state")) == "busy"),
        "sessions": mine,
        "history": history,
    }


def _team_prompt(project: dict[str, Any], instruction: str) -> str:
    """给 General Owner 的团队协议：明确分工与边界。"""
    return (
        f"你是项目「{project.get('name') or project.get('id')}」的 General Owner（总负责人）。\n"
        "工作方式（必须遵守）：\n"
        "1. 你是真实 Claude Code 会话的负责人，负责接收平台指令、拆解任务、分派与验收。\n"
        "2. 复杂任务用 Agent Team 协作推进；专项分析、代码审查用 Subagent 完成。\n"
        "3. 并行开发必须用 Git Worktree 隔离，一个角色一个 worktree，不共享工作区。\n"
        "4. 每个任务的产出要可验证：写清改了什么、怎么验证、失败原因。\n"
        "5. 不要执行 push、部署、删数据或修改 .claude 配置。\n\n"
        f"本次平台指令：\n{instruction}"
    )


def launch_team(project: dict[str, Any], instruction: str, *, actor: str = "local") -> dict[str, Any]:
    pre = preflight(project)
    if not pre["ready"]:
        failed = [c for c in pre["checks"] if not c["ok"]]
        raise TeamError(
            "team_preflight_failed", 409,
            "；".join(f"{c['label']}：{c['detail']}" for c in failed),
        )
    text = (instruction or "").strip()
    if not text:
        raise TeamError("invalid_payload", 422, "指令不能为空")
    binary = _claude()
    repo = Path(str(project["repo_root"])).expanduser()
    agent = owner_agent(project)
    # 项目没有 .claude/agents/<agent>.md 时，--agent 只会报警并退回默认模板；
    # 与其发一条永远失败的会话，不如显式去掉 --agent，并在结果里说明。
    agent_defined = (repo / ".claude" / "agents" / f"{agent}.md").is_file()
    args = [binary, "--bg", _team_prompt(project, text)]
    if agent_defined:
        args = [binary, "--agent", agent, "--bg", _team_prompt(project, text)]
    try:
        # --agent 决定这个真实会话以哪个角色运行；--bg 让它作为后台会话存在
        proc = _run(args, repo, LAUNCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise TeamError("team_timeout", 504, "启动后台会话超时")
    except OSError as exc:
        raise TeamError("team_launch_failed", 500, str(exc))
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    match = SESSION_ID_RE.search(output)
    session_id = match.group(0) if match else None
    if session_id is None:
        short = SHORT_ID_RE.search(output) or ATTACH_ID_RE.search(output)
        if short:
            # 短 id 解析成全 id：后续 --resume / stop 与 agents --json 对得上
            session_id = _resolve_session(repo, short.group(1)) or short.group(1)
    if proc.returncode != 0 and not session_id:
        raise TeamError(
            "team_launch_failed", 409,
            output[:400] or "启动失败",
        )
    conn = store.connect()
    try:
        store.init_db(conn)
        now = store.now_iso()
        record_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO team_sessions (id, project_id, session_id, agent, status, instruction,"
            " output, created_at, updated_at) VALUES (?, ?, ?, ?, 'launched', ?, ?, ?, ?)",
            (record_id, project["id"], session_id, agent, text[:2000], output[:2000], now, now),
        )
        if session_id:
            settings = project.get("settings_json")
            settings = settings if isinstance(settings, dict) else store.loads_json(settings, {})
            settings = dict(settings or {})
            settings[OWNER_SESSION_SETTING] = {
                "session_id": session_id, "agent": agent, "launched_at": now,
            }
            conn.execute(
                "UPDATE agent_projects SET settings_json = ?, updated_at = ? WHERE id = ?",
                (store.dumps_json(settings), now, project["id"]),
            )
        conn.execute(
            "INSERT INTO audit_logs (id, project_id, actor, action, resource_type, resource_id,"
            " result, detail_json, created_at) VALUES (?, ?, ?, 'team.launch', 'team_session', ?,"
            " 'ok', ?, ?)",
            (uuid.uuid4().hex, project["id"], actor, record_id,
             store.dumps_json({"session_id": session_id, "instruction": text[:200]}), now),
        )
    finally:
        conn.close()
    conn = store.connect()
    try:
        store.init_db(conn)
        status = team_status(project, conn)
    finally:
        conn.close()
    return {
        "ok": True,
        "record_id": record_id,
        "session_id": session_id,
        "agent": agent,
        "agent_defined": agent_defined,
        "note": None if agent_defined else (
            f"项目未定义 .claude/agents/{agent}.md，本次用默认模板启动；"
            "角色约束已写进指令正文，行为不受影响。"
        ),
        "output": output[:2000],
        "status": status,
    }


def instruct_team(project: dict[str, Any], session_id: str, instruction: str,
                  *, actor: str = "local") -> dict[str, Any]:
    if not executor_enabled():
        raise TeamError("executor_disabled", 409, f"执行器未开启：设置 {EXECUTOR_ENV}=on")
    text = (instruction or "").strip()
    if not text:
        raise TeamError("invalid_payload", 422, "指令不能为空")
    if not re.fullmatch(r"[0-9a-zA-Z-]{6,64}", session_id or ""):
        raise TeamError("invalid_payload", 422, "session_id 非法")
    repo = Path(str(project["repo_root"])).expanduser()
    resolved = _resolve_session(repo, session_id)
    if resolved is None:
        raise TeamError("not_found", 404, "找不到该会话；请刷新会话列表后重试")
    session_id = resolved
    binary = _claude()
    try:
        proc = _run([binary, "--bg", "--resume", session_id, text], repo, LAUNCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise TeamError("team_timeout", 504, "下达指令超时")
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    conn = store.connect()
    try:
        store.init_db(conn)
        now = store.now_iso()
        conn.execute(
            "UPDATE team_sessions SET instruction = ?, output = ?, updated_at = ?,"
            " status = 'instructed' WHERE project_id = ? AND session_id = ?",
            (text[:2000], output[:2000], now, project["id"], session_id),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, project_id, actor, action, resource_type, resource_id,"
            " result, detail_json, created_at) VALUES (?, ?, ?, 'team.instruct', 'team_session', ?,"
            " 'ok', ?, ?)",
            (uuid.uuid4().hex, project["id"], actor, session_id,
             store.dumps_json({"instruction": text[:200]}), now),
        )
    finally:
        conn.close()
    if proc.returncode != 0:
        raise TeamError("team_instruct_failed", 409, output[:400] or "指令失败")
    return {"ok": True, "session_id": session_id, "output": output[:2000]}


def stop_team(project: dict[str, Any], session_id: str, *, actor: str = "local") -> dict[str, Any]:
    if not executor_enabled():
        raise TeamError("executor_disabled", 409, f"执行器未开启：设置 {EXECUTOR_ENV}=on")
    binary = _claude()
    repo = Path(str(project["repo_root"])).expanduser()
    try:
        proc = _run([binary, "stop", session_id], repo, QUERY_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise TeamError("team_stop_failed", 500, str(exc))
    conn = store.connect()
    try:
        store.init_db(conn)
        now = store.now_iso()
        conn.execute(
            "UPDATE team_sessions SET status = 'stopped', updated_at = ?"
            " WHERE project_id = ? AND session_id = ?",
            (now, project["id"], session_id),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, project_id, actor, action, resource_type, resource_id,"
            " result, detail_json, created_at) VALUES (?, ?, ?, 'team.stop', 'team_session', ?,"
            " 'ok', '{}', ?)",
            (uuid.uuid4().hex, project["id"], actor, session_id, now),
        )
    finally:
        conn.close()
    return {"ok": True, "session_id": session_id, "output": ((proc.stdout or "") + (proc.stderr or ""))[:1000]}


def _transcript_log(repo: Path, session_id: str, lines: int) -> str:
    """把会话 transcript 的最后若干条渲染成可读文本（交互式会话的「日志」替代）。"""
    slug = str(repo.resolve()).replace("/", "-")
    path = Path.home() / ".claude" / "projects" / slug / f"{session_id}.jsonl"
    if not path.is_file():
        return f"找不到会话记录：{path}（该会话可能属于其他工作目录）"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"读取会话记录失败：{exc}"
    out: list[str] = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or row.get("type") or "")
        content = message.get("content")
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    parts.append(str(block.get("text") or ""))
                elif kind == "thinking":
                    parts.append("[思考] " + str(block.get("thinking") or ""))
                elif kind == "tool_use":
                    parts.append(f"[工具 {block.get('name')}] " + json.dumps(
                        block.get("input") or {}, ensure_ascii=False, default=str)[:200])
                elif kind == "tool_result":
                    parts.append("[结果] " + str(block.get("content") or "")[:300])
        body = " ".join(p.strip() for p in parts if str(p).strip())
        if not body:
            continue
        stamp = str(row.get("timestamp") or "")[:19].replace("T", " ")
        out.append(f"{stamp} {role}: {body}"[:1200])
    tail = out[-max(1, min(int(lines or 200), 1000)):]
    return "\n".join(tail) if tail else "该会话还没有可显示的消息"


def attach_session(project: dict[str, Any], session_id: str, instruction: str = "",
                   *, actor: str = "local") -> dict[str, Any]:
    """从平台接管一个正在运行的会话：把它的后续工作接到平台托管的后台会话上。

    为什么必须这样：交互式 TUI 会话没有可注入的输入通道，平台无法往别人的终端里
    打字。可行且诚实的做法是 `claude --bg --resume <session>`——同一份会话记录
    继续往下跑，之后这个会话就由平台托管，可在平台里看日志、继续对话、结束。

    返回里明确告诉用户实际落在哪个会话上：
    * ``taken_over`` 为真：原会话当时空闲，已用同一 id 在后台继续；
    * 否则 ``copied`` 为真：原会话正在跑，CLI 起了同源副本，返回新 id。
    """
    if not executor_enabled():
        raise TeamError("executor_disabled", 409, f"执行器未开启：设置 {EXECUTOR_ENV}=on")
    binary = _claude()
    if not binary:
        raise TeamError("team_unavailable", 409, "未找到 claude CLI")
    repo = Path(str(project["repo_root"])).expanduser()
    resolved = _resolve_session(repo, session_id)
    if resolved is None:
        raise TeamError("not_found", 404, "找不到该会话；请刷新会话列表后重试")

    sessions, err = _agents(repo, include_finished=True)
    matched = next(
        (it for it in sessions
         if str(it.get("sessionId") or "") == resolved or str(it.get("id") or "") == session_id),
        None,
    )
    if matched is None:
        raise TeamError("not_found", 404, err or "会话不在运行列表中")

    busy = str(matched.get("status") or matched.get("state") or "") == "busy"
    text = (instruction or "").strip() or (
        "接手这个会话：先说明你当前正在做什么、有哪些未完成的改动，"
        "然后等待平台指令。不要重复已经完成的工作。"
    )
    try:
        proc = _run([binary, "--bg", "--resume", resolved, text], repo, LAUNCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise TeamError("team_timeout", 504, "接管会话超时")
    except OSError as exc:
        raise TeamError("team_attach_failed", 500, str(exc))
    output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    new_id = None
    match = SESSION_ID_RE.search(output)
    if match:
        new_id = match.group(0)
    else:
        short = SHORT_ID_RE.search(output) or ATTACH_ID_RE.search(output)
        if short:
            new_id = _resolve_session(repo, short.group(1)) or short.group(1)
    if proc.returncode != 0 and not new_id:
        raise TeamError("team_attach_failed", 409, output[:400] or "接管失败")
    final_id = new_id or resolved
    copied = final_id != resolved

    conn = store.connect()
    try:
        store.init_db(conn)
        now = store.now_iso()
        settings = project.get("settings_json")
        settings = settings if isinstance(settings, dict) else store.loads_json(settings, {})
        settings = dict(settings or {})
        settings[OWNER_SESSION_SETTING] = {
            "session_id": final_id, "agent": owner_agent(project),
            "launched_at": now, "adopted_from": resolved,
        }
        conn.execute(
            "UPDATE agent_projects SET settings_json = ?, updated_at = ? WHERE id = ?",
            (store.dumps_json(settings), now, project["id"]),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, project_id, actor, action, resource_type, resource_id,"
            " result, detail_json, created_at) VALUES (?, ?, ?, 'team.attach', 'team_session', ?,"
            " 'ok', ?, ?)",
            (uuid.uuid4().hex, project["id"], actor, final_id,
             store.dumps_json({"adopted_from": resolved, "copied": copied, "was_busy": busy}), now),
        )
    finally:
        conn.close()
    return {
        "ok": True,
        "session_id": final_id,
        "adopted_from": resolved,
        "copied": copied,
        "was_busy": busy,
        "taken_over": not copied,
        "output": output[:2000],
        "note": (
            "原会话正在运行，CLI 起了同源副本（上下文一致，但不是同一个进程）；"
            "已把副本登记为平台托管会话。"
            if copied else
            "已用同一个 session id 在后台继续，后续在平台里看日志/继续对话即可。"
        ),
    }


def session_logs(project: dict[str, Any], session_id: str, *, lines: int = 200) -> dict[str, Any]:
    """读取真实会话最近输出（``claude logs``）。只读，不需要执行器开关。"""
    binary = _claude()
    if not binary:
        raise TeamError("team_unavailable", 409, "未找到 claude CLI")
    repo = Path(str(project["repo_root"])).expanduser()
    resolved = _resolve_session(repo, session_id) or session_id
    # claude logs 只对后台会话有效；交互式会话在终端里才有输出
    sessions, _err = _agents(repo)
    matched = next(
        (it for it in sessions
         if str(it.get("sessionId") or "") == resolved
         or str(it.get("id") or "") == session_id),
        None,
    )
    if matched is not None and str(matched.get("kind") or "") != "background":
        # 交互式会话没有 claude logs，但 transcript 是同一份事实源：
        # 直接把最近事件渲染成文本，用户至少能看到它到底在跑什么。
        text = _transcript_log(repo, resolved, lines)
        return {
            "session_id": resolved,
            "ok": True,
            "supported": True,
            "source": "transcript",
            "kind": str(matched.get("kind") or "interactive"),
            "line_count": len(text.splitlines()),
            "log": text,
            "note": "交互式会话没有 claude logs，这里读的是它自己的对话记录（只读）。",
        }
    cli_id = _cli_session_id(repo, resolved)
    try:
        proc = _run([binary, "logs", cli_id], repo, LOG_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise TeamError("team_timeout", 504, "读取会话日志超时")
    except OSError as exc:
        raise TeamError("team_logs_failed", 500, str(exc))
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    tail = "\n".join(text.splitlines()[-max(1, min(int(lines or 200), 1000)):])
    return {
        "session_id": resolved,
        "ok": proc.returncode == 0,
        "supported": True,
        "line_count": len(tail.splitlines()),
        "log": tail[-20000:],
    }
