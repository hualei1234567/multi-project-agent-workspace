#!/usr/bin/env python3
"""本机会话桥接：把本机打开的 Claude Code 窗口登记到平台，并执行平台下发的指令。

为什么需要它
------------
平台跑在服务器上，你的 VSCode 窗口开在另一台机器；`claude agents --json` 只能看到本机会话。
这个小脚本在那台机器上运行，负责两件事：

1. 每 N 秒上报本机会话（交互式窗口、后台会话）以及它们所属项目；
2. 领取平台下发的指令，用 `claude --bg --resume <session-id> "<指令>"` 执行并回报结果。

边界（不要夸大）
----------------
Claude Code 没有「往正在运行的 TUI 里注入文字」的能力。`--bg --resume` 在同一会话未运行时
会用原 id 继续；会话正在运行时 CLI 会另起一个副本并打印新 id。桥接会把实际使用的 id 如实回报，
平台上能看到「是否为副本」。

用法
----
    python3 scripts/session-bridge.py --platform http://<LAN_HOST>:8099 --device <device-id>

可选：--token <令牌>（平台开了鉴权时必需）、--interval 5、--once（跑一轮就退出，便于自检）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SHORT_ID_RE = re.compile(r"backgrounded\s*·\s*([0-9a-zA-Z-]{6,64})")

#: 后台会话高危工具拒绝规则（R1 / S-4）。
#:
#: 必须与 app/agent_platform/executor_policy.py 的 ``BG_DISALLOWED_TOOLS`` 保持一致：
#: 桥接跑在窗口所在机器上，不能 import 平台模块，只能内置一份。
#: 改一处必须同步改另一处——两边不一致就等于开了旁路。
BG_DISALLOWED_TOOLS = (
    "Bash(git push:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(git checkout --:*)",
    "Bash(rm:*)",
    "Bash(rmdir:*)",
    "Bash(mv:*)",
    "Bash(dd:*)",
    "Bash(docker:*)",
    "Bash(kubectl:*)",
    "Bash(ssh:*)",
    "Bash(scp:*)",
    "Bash(rsync:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(sudo:*)",
    "Bash(su:*)",
    "Bash(chmod:*)",
    "Bash(chown:*)",
    "Bash(migrate:*)",
    "Bash(psql:*)",
    "Bash(mysql:*)",
    "Bash(systemctl:*)",
    "Bash(launchctl:*)",
    "Bash(crontab:*)",
    "WebFetch",
)


def _http(method: str, url: str, payload: dict | None = None, token: str = "", timeout: int = 20) -> dict:
    """走 curl，不用 urllib。

    原因（实测）：这台 Mac 上 python 进程访问局域网会直接 EHOSTUNREACH，
    而 curl / nc / ssh 都能连通——属于系统层面对 python 二进制的网络限制。
    桥接必须稳定连上平台，所以统一用 curl 传输，python 只负责流程。
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-X", method,
           "-H", "Content-Type: application/json"]
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    cmd.append(url)
    proc = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout + 10, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        raise RuntimeError(f"{method} {url} → curl 退出码 {proc.returncode}：{err}")
    text = (proc.stdout or b"").decode("utf-8", "replace")
    try:
        payload_out = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{method} {url} → 响应不是 JSON：{text[:200]}") from exc
    if isinstance(payload_out, dict) and payload_out.get("ok") is False:
        raise RuntimeError(f"{method} {url} → {payload_out.get('error')}: {payload_out.get('message')}")
    return payload_out


def claude_bin() -> str:
    found = shutil.which("claude")
    if not found:
        raise SystemExit("找不到 claude 命令，请确认本机已安装 Claude Code CLI")
    return found


def list_sessions() -> list[dict]:
    out = subprocess.run([claude_bin(), "agents", "--json"], capture_output=True, text=True,
                   timeout=60, check=False)
    try:
        data = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def project_for(cwd: str, projects: list[dict]) -> str | None:
    """会话 cwd → 项目 id。

    平台上的 repo_root 是服务器路径，桥接在本机看到的 cwd 是另一套路径，
    所以不能只做前缀匹配，按下面顺序判定：

    1. 以 repo_root 结尾（同仓库在不同机器的挂载路径）；
    2. 路径末段与 repo_root 末段同名（本机与服务器目录名一致的情况）；
    3. 位于 ~/agent-team/<id>-team/ 下（项目 worktree）。
    """
    cwd = str(cwd or "").rstrip("/")
    if not cwd:
        return None
    for project in projects:
        repo = str(project.get("repo_root") or "").rstrip("/")
        if not repo:
            continue
        if cwd == repo or cwd.endswith(repo):
            parent = cwd[: -len(repo)] or "/"
            if cwd.endswith(repo) and (parent.endswith("/") or parent == ""):
                return str(project.get("id"))
    by_name = {
        repo.rstrip("/").rsplit("/", 1)[-1]: str(project.get("id"))
        for project in projects
        if (repo := str(project.get("repo_root") or ""))
    }
    name = cwd.rsplit("/", 1)[-1]
    if name in by_name:
        return by_name[name]
    for project in projects:
        pid = str(project.get("id") or "")
        if f"/agent-team/{pid}-team/" in cwd:
            return pid
    return None


def report(base: str, device: str, token: str, sessions: list[dict], projects: list[dict]) -> int:
    payload = []
    for item in sessions:
        cwd = str(item.get("cwd") or "")
        payload.append({
            "sessionId": item.get("sessionId"),
            "name": item.get("name"),
            "cwd": cwd,
            "kind": item.get("kind"),
            "status": item.get("status"),
            "pid": item.get("pid"),
            "project_id": project_for(cwd, projects),
        })
    res = _http("POST", f"{base}/api/devices/{device}/sessions", {"sessions": payload}, token)
    return int(res.get("accepted") or 0)


def _short(value, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\n", " ").strip()
    return text[:limit]


def transcript_path(session: dict) -> Path | None:
    cwd = str(session.get("cwd") or "")
    sid = str(session.get("sessionId") or "")
    if not cwd or not sid:
        return None
    slug = cwd.rstrip("/").replace("/", "-")
    candidate = Path.home() / ".claude" / "projects" / slug / f"{sid}.jsonl"
    return candidate if candidate.is_file() else None


def build_trace(session: dict, project_id: str | None, limit: int = 160) -> dict | None:
    """把本机会话的 transcript 解析成平台能直接渲染的 trace（只取最近 limit 条）。"""
    path = transcript_path(session)
    if path is None:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    events: list[dict] = []
    usage_window: list[int] = []   # 只统计最近若干轮，整份文件的累计会大到没有意义
    model = None
    started_at = None
    last_at = None
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            at = row.get("timestamp") or row.get("ts")
            if at:
                started_at = started_at or at
                last_at = at
            message = row.get("message") or {}
            if isinstance(message, dict):
                model = message.get("model") or model
                usage = message.get("usage") or {}
                if isinstance(usage, dict):
                    usage_window.append(sum(int(usage.get(k) or 0) for k in (
                        "input_tokens", "output_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens")))
                    usage_window = usage_window[-200:]
                content = message.get("content")
                blocks = content if isinstance(content, list) else []
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "thinking":
                        events.append({"kind": "thinking", "text": _short(block.get("thinking")), "at": at})
                    elif btype == "text":
                        events.append({"kind": "text", "text": _short(block.get("text"), 900), "at": at})
                    elif btype == "tool_use":
                        events.append({"kind": "tool", "tool": block.get("name"),
                                       "detail": _short(block.get("input") or {}, 200), "at": at})
                    elif btype == "tool_result":
                        events.append({"kind": "tool_result", "text": _short(block.get("content"), 900), "at": at})
            if len(events) > limit * 3:
                events = events[-limit * 2:]
    events = events[-limit:]
    status = "running" if session.get("status") in ("busy", "running") else (
        "idle" if session.get("status") in ("idle", None) else "stalled")
    return {
        "session_id": session.get("sessionId"),
        "project_id": project_id,
        "payload": {
            "ok": True,
            "session_id": session.get("sessionId"),
            "kind": "window",
            "agent_id": None,
            "description": session.get("name") or f"窗口 {str(session.get('sessionId'))[:8]}",
            "agent_type": "window",
            "status": status,
            "status_text": {"running": "运行中", "idle": "空闲", "stalled": "疑似停滞"}.get(status, status),
            "started_at": started_at,
            "last_event_at": last_at,
            "idle_seconds": int(max(0, time.time() - stat.st_mtime)),
            "tokens": sum(usage_window),
            "last_step": (events[-1].get("tool") or events[-1].get("kind") or "") if events else "",
            "last_detail": (events[-1].get("detail") or events[-1].get("text") or "")[:120] if events else "",
            "events_total": len(events),
            "truncated": len(events) >= limit,
            "events": events,
            "model": model,
        },
    }


def run_command(base: str, device: str, token: str, cmd: dict) -> None:
    session_id = str(cmd.get("session_id") or "")
    instruction = str(cmd.get("instruction") or "")
    cwd = None
    for item in list_sessions():
        if str(item.get("sessionId")) == session_id:
            cwd = str(item.get("cwd") or "")
            break
    if not cwd:
        _http("POST", f"{base}/api/devices/{device}/commands/{cmd['id']}/result",
              {"ok": False, "error": "会话已不存在（窗口可能已关闭）"}, token)
        return
    # R1 / S-4：带统一 deny 策略再启动，避免窗口派发成为绕过工具管控的旁路
    args = [claude_bin(), "--bg", "--resume", session_id,
            "--disallowedTools", *BG_DISALLOWED_TOOLS, instruction]
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=900, check=False)
        text = f"{out.stdout}\n{out.stderr}".strip()
        ok = out.returncode == 0
        match = SHORT_ID_RE.search(text)
        used = session_id
        if match:
            used = match.group(1)
        note = text[:2000]
        _http("POST", f"{base}/api/devices/{device}/commands/{cmd['id']}/result",
              {"ok": ok, "session_id": used, "result": note,
               "error": "" if ok else text[-800:]}, token)
        print(f"[桥接] 指令 {cmd['id'][:8]} → {'成功' if ok else '失败'}（会话 {used[:8]}）")
    except subprocess.TimeoutExpired:
        _http("POST", f"{base}/api/devices/{device}/commands/{cmd['id']}/result",
              {"ok": False, "error": "执行超时（15 分钟）"}, token)
        print(f"[桥接] 指令 {cmd['id'][:8]} → 超时")


def main() -> int:
    ap = argparse.ArgumentParser(description="把本机 Claude Code 窗口登记到平台并执行下发指令")
    ap.add_argument("--platform", default=os.environ.get("AGWS_PLATFORM_URL", "http://<LAN_HOST>:8099"))
    ap.add_argument("--device", default=os.environ.get("AGWS_DEVICE", os.uname().nodename.split(".")[0].lower()))
    ap.add_argument("--token", default=os.environ.get("AGWS_TOKEN", ""))
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true", help="只跑一轮，便于自检")
    args = ap.parse_args()

    base = args.platform.rstrip("/")
    print(f"[桥接] 设备 {args.device} → {base}（每 {args.interval:g} 秒一轮）")
    projects: list[dict] = []
    projects_at = 0.0
    while True:
        try:
            now = time.time()
            if now - projects_at > 60:
                projects = list(_http("GET", f"{base}/api/projects", token=args.token).get("items") or [])
                projects_at = now
            sessions = list_sessions()
            accepted = report(base, args.device, args.token, sessions, projects)
            # 把本机会话的 trace 推上去：平台在服务器上，读不到这台机器的 transcript
            traces = []
            for item in sessions[:10]:
                payload = build_trace(item, project_for(str(item.get("cwd") or ""), projects))
                if payload:
                    traces.append(payload)
            if traces:
                _http("POST", f"{base}/api/devices/{args.device}/traces", {"traces": traces}, args.token)
            commands = _http("GET", f"{base}/api/devices/{args.device}/commands", token=args.token).get("commands") or []
            print(f"[桥接] {time.strftime('%H:%M:%S')} 上报 {accepted} 个会话 / {len(traces)} 条 trace，领取 {len(commands)} 条指令")
            for cmd in commands:
                run_command(base, args.device, args.token, cmd)
            if args.once:
                return 0
        except Exception as exc:  # noqa: BLE001 - 桥接要长期运行，单轮失败不能退出
            print(f"[桥接] 本轮失败：{exc}", file=sys.stderr)
            if args.once:
                return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
