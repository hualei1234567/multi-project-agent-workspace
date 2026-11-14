"""文件采集适配器：把本机现有事实（任务板 / 自报状态 / worktree / 报告 / transcript）
适配成平台领域数据。只读源文件，只写平台独立 SQLite；全部幂等。

数据源（与 scripts/agents_dashboard.py 一致）：
| 数据 | 位置 |
|---|---|
| 任务 | ``<repo>/ai-team/TASK_BOARD.md`` |
| 自报状态 | ``<repo>/ai-team/status/*.json`` |
| worktree | ``git -C <repo> worktree list --porcelain`` |
| 交付 | ``<repo>/ai-team/reports/``、``<repo>/docs/reports/`` |
| 事件 / Token | ``~/.claude/projects/<slug>/*.jsonl`` 及 ``subagents/*.jsonl`` |
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import cost, store

KNOWN_BOARD_STATUSES = frozenset(
    {"BACKLOG", "READY", "IN_PROGRESS", "REVIEW", "TESTING", "DONE", "BLOCKED", "CANCELLED"}
)

BOARD_STATUS_MAP = {
    "BACKLOG": "todo",
    "READY": "todo",
    "IN_PROGRESS": "in_progress",
    "REVIEW": "review",
    "TESTING": "review",
    "DONE": "done",
    "BLOCKED": "blocked",
    "CANCELLED": "blocked",
}

SELF_REPORT_STATUS_MAP = {
    "running": "working",
    "working": "working",
    "done": "idle",
    "idle": "idle",
    "blocked": "blocked",
    "offline": "offline",
}

WORKTREE_ROLE_BY_DIR = {
    "内部平台": "general-owner",
    "fe": "frontend-engineer",
    "be": "backend-engineer",
    "qa-review": "qa-devops",
}

ROLE_ALIASES = {
    "qa-engineer": "qa-devops",
    "qa-devops": "qa-devops",
    "general-owner": "general-owner",
    "product-manager": "product-manager",
    "system-architect": "system-architect",
    "ux-designer": "ux-designer",
    "frontend-engineer": "frontend-engineer",
    "backend-engineer": "backend-engineer",
    "fullstack-engineer": "fullstack-engineer",
    "product-architecture": "product-architecture",
    "general-purpose": "unknown",
}

ROLE_KEY_BY_KEYWORD = (
    ("prd", "product-manager"),
    ("需求", "product-manager"),
    ("契约", "system-architect"),
    ("adr", "system-architect"),
    ("信息架构", "ux-designer"),
    ("页面规范", "ux-designer"),
    ("复验", "qa-devops"),
    ("复核", "qa-devops"),
    ("审查", "qa-devops"),
    ("general", "general-owner"),
    ("owner", "general-owner"),
    ("product", "product-manager"),
    ("pm", "product-manager"),
    ("architect", "system-architect"),
    ("架构", "system-architect"),
    ("ux", "ux-designer"),
    ("ui", "ux-designer"),
    ("frontend", "frontend-engineer"),
    ("前端", "frontend-engineer"),
    ("backend", "backend-engineer"),
    ("后端", "backend-engineer"),
    ("fullstack", "fullstack-engineer"),
    ("qa", "qa-devops"),
    ("test", "qa-devops"),
    ("测试", "qa-devops"),
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def projects_root() -> Path:
    override = os.environ.get("AGWS_PLATFORM_PROJECTS_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude" / "projects"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _norm_role(value: str | None) -> str:
    text = (value or "").strip().lower()
    if not text:
        return "unknown"
    if text in ROLE_ALIASES:
        return ROLE_ALIASES[text]
    for keyword, role in ROLE_KEY_BY_KEYWORD:
        if keyword in text:
            return role
    return "unknown"


def _priority(value: str | None) -> str:
    text = (value or "").strip().upper()
    return text if text in {"P0", "P1", "P2", "P3"} else "P2"


def _delivery_kind(name: str) -> str:
    low = name.lower()
    if "release" in low or "发布" in low or "deploy" in low:
        return "release"
    if "test" in low or "qa" in low or "验收" in low:
        return "test_report"
    if low.endswith((".md", ".html", ".pdf", ".txt")):
        return "document"
    return "other"


def _git(*args: str, cwd: Path) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


# ---------------------------------------------------------------------------
# ccusage 会话成本（只做机器级对账，不反推 Token 明细）
# ---------------------------------------------------------------------------
CCUSAGE_CACHE_SECONDS = 60
_ccusage_cache: dict[str, Any] = {"at": 0.0, "value": []}


def _find_ccusage() -> str | None:
    found = shutil.which("ccusage")
    if found:
        return found
    nvm_bins = sorted((Path.home() / ".nvm" / "versions" / "node").glob("*/bin/ccusage"), reverse=True)
    if nvm_bins:
        return str(nvm_bins[0])
    for candidate in ("/opt/homebrew/bin/ccusage", "/usr/local/bin/ccusage"):
        if Path(candidate).exists():
            return candidate
    return None


def clear_ccusage_cache() -> None:
    _ccusage_cache["at"] = 0.0
    _ccusage_cache["value"] = []


def _normalize_sessions(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("sessions") or data.get("data") or []
    else:
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cost = row.get("totalCost")
        try:
            total_cost = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            total_cost = None
        try:
            total_tokens = int(row.get("totalTokens") or 0)
        except (TypeError, ValueError):
            total_tokens = 0
        out.append({
            "project_path": str(row.get("projectPath") or ""),
            "session_id": str(row.get("sessionId") or ""),
            "last_activity": store.iso_utc(row.get("lastActivity")),
            "total_cost": total_cost,
            "total_tokens": total_tokens,
        })
    return out


def ccusage_session_costs() -> list[dict[str, Any]]:
    """调用 ``ccusage claude session --json``；未安装/失败/超时一律返回空列表。

    返回项：``project_path``（通常是 slug 形式的项目路径）、``session_id``、
    ``last_activity``（UTC ISO）、``total_cost``（float/None）、``total_tokens``。
    这是“会话总成本/总 Token”，用于按 token 占比把跨天会话分摊到近 24h，不做精确日切分。
    """
    now = time.time()
    if (now - float(_ccusage_cache["at"])) < CCUSAGE_CACHE_SECONDS:
        return list(_ccusage_cache["value"])
    binary = _find_ccusage()
    if binary is None:
        return []
    parsed: list[dict[str, Any]] = []
    for args in (["claude", "session", "--json"], ["session", "--json"]):
        try:
            proc = subprocess.run(
                [binary, *args], capture_output=True, text=True, timeout=25, check=False
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        try:
            parsed = _normalize_sessions(json.loads(proc.stdout))
        except ValueError:
            continue
        break
    # 失败结果也做短缓存，避免轮询时反复拉起子进程。
    _ccusage_cache["at"] = now
    _ccusage_cache["value"] = parsed
    return list(parsed)


# ---------------------------------------------------------------------------
# 任务板
# ---------------------------------------------------------------------------
def parse_task_board(repo_root: Path) -> list[dict[str, str]]:
    """解析 ``ai-team/TASK_BOARD.md`` 的 TEAM-xxx 行；保留 task_key。"""
    path = repo_root / "ai-team" / "TASK_BOARD.md"
    tasks: list[dict[str, str]] = []
    for line in _read_text(path).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 6 or not re.match(r"^TEAM-\d+$", cells[0]):
            continue
        status_at = next((i for i, cell in enumerate(cells) if cell in KNOWN_BOARD_STATUSES), None)
        if status_at is None or status_at < 3:
            continue
        title = " ".join(part for part in cells[1:max(status_at - 1, 2)] if part)
        rest = cells[status_at:]
        raw_status = rest[0]
        tasks.append(
            {
                "task_key": cells[0],
                "title": title,
                "status": BOARD_STATUS_MAP[raw_status],
                "raw_status": raw_status,
                "owner": rest[1] if len(rest) > 1 else "",
                "priority": _priority(rest[2] if len(rest) > 2 else ""),
                "depends": rest[3] if len(rest) > 3 else "",
                "ref": rest[4] if len(rest) > 4 else "",
                "qa": rest[5] if len(rest) > 5 else "",
            }
        )
    return tasks


def backfill_tasks(conn: sqlite3.Connection, project: dict[str, Any]) -> int:
    repo = Path(project["repo_root"])
    now = store.now_iso()
    count = 0
    for task in parse_task_board(repo):
        conn.execute(
            """
            INSERT INTO tasks
                (id, project_id, task_key, title, status, priority, owner, qa, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'board', ?)
            ON CONFLICT(project_id, task_key) DO UPDATE SET
                title = excluded.title,
                status = excluded.status,
                priority = excluded.priority,
                owner = excluded.owner,
                qa = excluded.qa,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                store.stable_id("task", project["id"], task["task_key"]),
                project["id"],
                task["task_key"],
                task["title"],
                task["status"],
                task["priority"],
                task["owner"],
                task["qa"],
                now,
            ),
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# 自报状态 / worktree / Agent
# ---------------------------------------------------------------------------
def scan_status_files(repo_root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    folder = repo_root / "ai-team" / "status"
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        data = _read_json(path)
        if not data.get("role"):
            continue
        out[_norm_role(str(data["role"]))] = data
    return out


def role_for_worktree(name: str) -> str | None:
    """worktree 目录名 → 角色。目录可能带项目前缀（``ddp-be`` / ``mat-fe``）。"""
    name = str(name or "").strip()
    if not name:
        return None
    if name in WORKTREE_ROLE_BY_DIR:
        return WORKTREE_ROLE_BY_DIR[name]
    for key, role in WORKTREE_ROLE_BY_DIR.items():
        if name.endswith("-" + key):
            return role
    lowered = name.lower()
    if lowered.endswith(("-qa", "-qa-devops")):
        return "qa-devops"
    return _norm_role(name)


def scan_worktrees(repo_root: Path) -> list[dict[str, Any]]:
    raw = _git("worktree", "list", "--porcelain", cwd=repo_root)
    items: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in raw.splitlines():
        if line.startswith("worktree "):
            if current:
                items.append(current)
            current = {"path": line[len("worktree "):], "head": None, "branch": None}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):][:8]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].replace("refs/heads/", "")
    if current:
        items.append(current)

    for item in items:
        path = Path(item["path"])
        item["role_key"] = role_for_worktree(path.name)
        item["present"] = path.exists()
        item["branch"] = item.get("branch") or ("(detached)" if item.get("head") else None)
        if item["present"]:
            changes = _git("status", "--porcelain", cwd=path).strip()
            item["dirty"] = len([ln for ln in changes.splitlines() if ln.strip()])
        else:
            item["dirty"] = 0
    return items


def _task_id_for_key(conn: sqlite3.Connection, project_id: str, task_key: str | None) -> str | None:
    if not task_key:
        return None
    row = conn.execute(
        "SELECT id FROM tasks WHERE project_id = ? AND task_key = ?",
        (project_id, task_key),
    ).fetchone()
    return row["id"] if row else None


def backfill_agents(conn: sqlite3.Connection, project: dict[str, Any]) -> int:
    repo = Path(project["repo_root"])
    status_by_role = scan_status_files(repo)
    worktrees = scan_worktrees(repo)
    agents = conn.execute(
        "SELECT * FROM project_agents WHERE project_id = ?", (project["id"],)
    ).fetchall()
    updated = 0
    for agent in agents:
        row = dict(agent)
        status = status_by_role.get(row["role_key"])
        worktree = next(
            (
                wt
                for wt in worktrees
                if wt.get("role_key") == row["role_key"]
                and (wt.get("present") or wt.get("path"))
            ),
            None,
        )
        branch = row.get("current_branch")
        worktree_path = row.get("worktree_path")
        last_heartbeat = row.get("last_heartbeat_at")
        agent_status = row.get("status") or "offline"
        current_task_id = row.get("current_task_id")

        metadata = store.loads_json(row.get("metadata_json"), {})
        if status:
            agent_status = SELF_REPORT_STATUS_MAP.get(str(status.get("state")), agent_status)
            last_heartbeat = store.iso_utc(status.get("updated_at")) or last_heartbeat
            # status 文件里的 worktree 可能是迁移前的旧路径（2026-11-04 目录收拢到
            # ~/agent-team/<code>-team/worktrees）。只在路径真实存在时才采信，
            # 否则以上面 git worktree list 的实时结果为准。
            reported = status.get("worktree")
            if reported and Path(str(reported)).expanduser().is_dir():
                worktree_path = str(reported)
            if status.get("task"):
                current_task_id = _task_id_for_key(conn, project["id"], str(status["task"])) or current_task_id
            metadata["self_report"] = {
                "state": status.get("state"),
                "task": status.get("task"),
                "note": status.get("note"),
                "commit": status.get("commit"),
                "report": status.get("report"),
            }
        elif worktree and worktree.get("present"):
            agent_status = "idle" if agent_status == "offline" else agent_status
        if worktree and worktree.get("branch"):
            branch = branch or worktree["branch"]
        # 实时 worktree 路径优先：它是 git 的真实状态，status 文件与历史库里
        # 都可能残留迁移前的旧路径。
        if worktree and worktree.get("path") and worktree.get("present"):
            worktree_path = worktree["path"]

        conn.execute(
            """
            UPDATE project_agents SET
                status = ?,
                current_task_id = ?,
                current_branch = ?,
                worktree_path = ?,
                last_heartbeat_at = ?,
                metadata_json = ?
            WHERE id = ?
            """,
            (
                agent_status,
                current_task_id,
                branch,
                worktree_path,
                last_heartbeat,
                store.dumps_json(metadata),
                row["id"],
            ),
        )
        updated += 1

        if status:
            occurred = store.iso_utc(status.get("updated_at")) or store.now_iso()
            event_type = "status_report"
            message = str(status.get("note") or "")
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_events
                    (id, project_id, agent_id, session_id, event_type, message, tool, ok,
                     reported_state, occurred_at)
                VALUES (?, ?, ?, NULL, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    store.stable_id("evt", project["id"], row["id"], occurred, event_type),
                    project["id"],
                    row["id"],
                    event_type,
                    message,
                    str(status.get("state") or "").strip() or None,
                    occurred,
                ),
            )
    return updated


# ---------------------------------------------------------------------------
# 交付物
# ---------------------------------------------------------------------------
def backfill_deliveries(conn: sqlite3.Connection, project: dict[str, Any]) -> int:
    repo = Path(project["repo_root"])
    count = 0
    for folder in (repo / "ai-team" / "reports", repo / "docs" / "reports"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            try:
                rel = path.relative_to(repo).as_posix()
            except ValueError:
                rel = path.name
            match = re.search(r"TEAM-\d+", path.name, re.IGNORECASE)
            task_key = match.group(0).upper() if match else None
            task_id = _task_id_for_key(conn, project["id"], task_key)
            sha_match = re.search(r"\b[0-9a-f]{7,40}\b", path.name)
            conn.execute(
                """
                INSERT INTO deliveries
                    (id, project_id, task_id, title, kind, path, sha, created_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'mirror')
                ON CONFLICT(id) DO UPDATE SET
                    task_id = excluded.task_id,
                    title = excluded.title,
                    kind = excluded.kind,
                    path = excluded.path,
                    sha = excluded.sha,
                    created_at = excluded.created_at,
                    source = 'mirror'
                """,
                (
                    store.stable_id("delivery", project["id"], rel),
                    project["id"],
                    task_id,
                    path.name,
                    _delivery_kind(path.name),
                    rel,
                    sha_match.group(0) if sha_match else None,
                    store.from_mtime(mtime),
                ),
            )
            count += 1
    return count


# ---------------------------------------------------------------------------
# 事件 / Token（session transcript 增量解析）
# ---------------------------------------------------------------------------
def _ingest_transcript(
    conn: sqlite3.Connection,
    project_id: str,
    path: Path,
    session_id: str,
    agent_id: str,
    offset: int,
    counts: dict[str, int],
) -> tuple[int, str | None]:
    """从 offset 起解析新增的完整行；返回 (新 offset, 最后时间戳)。"""
    try:
        size = path.stat().st_size
    except OSError:
        return offset, None
    if offset > size:
        offset = 0
    new_offset = offset
    last_ts: str | None = None
    try:
        fh = path.open("rb")
    except OSError:
        return offset, None
    with fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # 写入中的半行，下次再读
            new_offset += len(raw)
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            ts = store.iso_utc(rec.get("timestamp"))
            if ts:
                last_ts = ts
            if rec.get("type") != "assistant":
                continue
            message = rec.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            message_uuid = str(message.get("id") or rec.get("uuid") or "")
            if not message_uuid:
                continue
            model = str(message.get("model") or "unknown")
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            cache_write = int(usage.get("cache_creation_input_tokens") or 0)
            total = input_tokens + output_tokens + cache_read + cache_write
            occurred = ts or store.now_iso()
            # R2 / D-2：采集时就把成本口径定下来，不留「以后再算」。
            # transcript 里没有 provider 账单金额，所以只能估算；估算必须命中
            # 当时生效的价格版本，否则保持 UNKNOWN —— 不用当前单价倒填历史。
            estimated: float | None = None
            price_id: str | None = None
            if total <= 0:
                # 零 token 的消息（如 <synthetic>）本来就不会计费：
                # 这是「确定为零」，不是「不知道」，因此单独标一个质量值。
                quality = "not_billable"
                estimated = 0.0
            else:
                price = cost.resolve_price(conn, model, occurred)
                if price is not None:
                    # 不截断精度：单价是「每百万 token」，单条消息金额可能在 1e-7
                    # 量级，round(6) 会把小额消息抹成 0，累积后与账单对不上。
                    estimated = cost.estimate_usd(price, {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_tokens": cache_read,
                        "cache_write_tokens": cache_write,
                    })
                    price_id = cost.price_version_id(price)
                    quality = cost.COST_ESTIMATED
                else:
                    quality = cost.COST_UNKNOWN
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO token_usage_events
                    (id, project_id, agent_id, session_id, message_uuid, model,
                     input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                     total_tokens, source, occurred_at,
                     estimated_cost_usd, price_version, cost_quality)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'transcript', ?, ?, ?, ?)
                """,
                (
                    store.stable_id("tok", session_id, message_uuid, agent_id, model),
                    project_id,
                    agent_id,
                    session_id,
                    message_uuid,
                    model,
                    input_tokens,
                    output_tokens,
                    cache_read,
                    cache_write,
                    total,
                    occurred,
                    estimated,
                    price_id,
                    quality,
                ),
            )
            counts["tokens"] += max(cur.rowcount, 0)
    return new_offset, last_ts


def _role_map(conn: sqlite3.Connection, project_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT id, role_key FROM project_agents WHERE project_id = ?", (project_id,)
    ).fetchall()
    return {row["role_key"]: row["id"] for row in rows}


#: 内建审查类 subagent：它们做的是「审查/校验」，归到 QA，而不是无归属。
BUILTIN_ROLE_BY_AGENT_TYPE = {
    "code-reviewer": "qa-devops",
    "security-auditor": "qa-devops",
    "test-engineer": "qa-devops",
    "security-engineer": "qa-devops",
}


def _resolve_subagent_role(meta: dict[str, Any], role_agents: dict[str, str]) -> str:
    """Subagent 归属哪个角色（按可信度从高到低），认不出返回空串由调用方回落。

    1. ``name``：人起的名字（ux-review / architect-review），最准；
    2. ``agentType``：显式角色（frontend-engineer 等）；
    3. 内建审查类（code-reviewer / security-auditor）→ QA；
    4. ``description`` 里的关键词（PRD → 产品，契约/架构 → 架构，测试 → QA …）。

    全部认不出时返回空串，调用方挂到父会话的主人名下——因为它的确是那个会话
    派生出来的 Subagent，这比挂到字符串 "unknown" 上诚实得多。
    """
    for candidate in (meta.get("name"), meta.get("agentType")):
        role = _norm_role(str(candidate or "").strip())
        if role != "unknown" and role in role_agents:
            return role
    builtin = BUILTIN_ROLE_BY_AGENT_TYPE.get(str(meta.get("agentType") or "").strip().lower())
    if builtin and builtin in role_agents:
        return builtin
    description = str(meta.get("description") or "")
    if description:
        role = _norm_role(description)
        if role != "unknown" and role in role_agents:
            return role
    return ""


def _insert_event(
    conn: sqlite3.Connection,
    project_id: str,
    agent_id: str | None,
    session_id: str | None,
    event_type: str,
    message: str,
    tool: str | None,
    ok: int | None,
    occurred_at: str,
    counts: dict[str, int],
) -> None:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO agent_events
            (id, project_id, agent_id, session_id, event_type, message, tool, ok, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            store.stable_id("evt", project_id, session_id, agent_id, occurred_at, event_type, tool),
            project_id,
            agent_id,
            session_id,
            event_type,
            message,
            tool,
            ok,
            occurred_at,
        ),
    )
    counts["events"] += max(cur.rowcount, 0)


def repair_event_attribution(conn: sqlite3.Connection, project: dict[str, Any],
                             *, dry_run: bool = True) -> dict[str, Any]:
    """把历史事件里 agent_id 为空/unknown 的行，按 transcript 源文件重新归属。

    只做 UPDATE，不删行、不改时间：以 (session_id, occurred_at, event_type, tool)
    回源匹配。匹配不上的保持原样并单独计数，绝不猜测。
    """
    role_agents = _role_map(conn, project["id"])
    owner_agent_id = role_agents.get("general-owner")
    root = projects_root()
    # 源索引：subagent (session, occurred, agentType) → role_key
    wanted: dict[tuple[str, str, str], str] = {}
    for slug in project_slugs(project):
        slug_dir = root / slug
        if not slug_dir.is_dir():
            continue
        for session_dir in sorted(p for p in slug_dir.iterdir() if p.is_dir()):
            sub = session_dir / "subagents"
            if not sub.is_dir():
                continue
            for meta_path in sorted(sub.glob("*.meta.json")):
                meta = _read_json(meta_path)
                agent_file = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
                if not agent_file.is_file():
                    continue
                role_key = _resolve_subagent_role(meta, role_agents)
                target = role_agents.get(role_key) or owner_agent_id
                if not target:
                    continue
                wanted[(session_dir.name, str(meta.get("agentType") or ""), str(meta.get("description") or ""))] = target

    rows = conn.execute(
        "SELECT id, agent_id, session_id, event_type, tool, message, occurred_at"
        " FROM agent_events WHERE project_id = ? AND (agent_id IS NULL OR agent_id = ''"
        " OR lower(agent_id) IN ('unknown', 'none', 'null'))",
        (project["id"],),
    ).fetchall()
    planned: list[tuple[str, str, str]] = []
    unmatched = 0
    for row in rows:
        target = None
        if row["event_type"] == "subagent_run":
            key = (str(row["session_id"] or ""), str(row["tool"] or ""), str(row["message"] or ""))
            target = wanted.get(key)
        else:
            # session_activity / status_report 属于主会话，归总负责人
            target = owner_agent_id
        if target:
            planned.append((target, row["id"], row["event_type"]))
        else:
            unmatched += 1
    if not dry_run:
        for target, event_id, _kind in planned:
            conn.execute("UPDATE agent_events SET agent_id = ? WHERE id = ?", (target, event_id))
    by_type: dict[str, int] = {}
    for _t, _i, kind in planned:
        by_type[kind] = by_type.get(kind, 0) + 1
    return {
        "project_id": project["id"],
        "dry_run": dry_run,
        "scanned": len(rows),
        "planned": len(planned),
        "unmatched": unmatched,
        "by_type": by_type,
    }


def backfill_events_and_tokens(conn: sqlite3.Connection, project: dict[str, Any]) -> dict[str, int]:
    counts = {"events": 0, "tokens": 0}
    root = projects_root()
    role_agents = _role_map(conn, project["id"])
    owner_agent_id = role_agents.get("general-owner")
    settings = project.get("settings_json")
    if isinstance(settings, str):
        settings = store.loads_json(settings, {})
    if not isinstance(settings, dict):
        settings = {}
    raw_cursors = settings.get("transcript_cursors", {})
    cursors = raw_cursors if isinstance(raw_cursors, dict) else {}

    for slug in project_slugs(project):
        slug_dir = root / slug
        if not slug_dir.is_dir():
            continue
        for path in sorted(slug_dir.glob("*.jsonl")):
            session_id = path.stem
            key = str(path)
            try:
                offset = int(cursors.get(key, 0))
            except (TypeError, ValueError):
                offset = 0
            new_offset, last_ts = _ingest_transcript(
                conn, project["id"], path, session_id, owner_agent_id or "general-owner", offset, counts
            )
            cursors[key] = new_offset
            if last_ts:
                _insert_event(
                    conn, project["id"], owner_agent_id, session_id,
                    "session_activity", f"会话 {session_id} 有新活动",
                    None, None, last_ts, counts,
                )

        for session_dir in sorted(p for p in slug_dir.iterdir() if p.is_dir()):
            sub = session_dir / "subagents"
            if not sub.is_dir():
                continue
            for meta_path in sorted(sub.glob("*.meta.json")):
                meta = _read_json(meta_path)
                agent_file = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
                if not agent_file.is_file():
                    continue
                # 归属顺序：meta.name（如 ux-review）→ agentType → 父主会话的主人。
                # agentType 常见 general-purpose / Explore / code-reviewer，认不出角色时
                # 必须回落到父会话，否则事件挂到字符串 "unknown" 上，谁都不显示。
                role_key = _resolve_subagent_role(meta, role_agents)
                agent_id = role_agents.get(role_key) or owner_agent_id or role_key
                agent_id = None if str(agent_id) in ("", "unknown") else agent_id
                session_id = session_dir.name
                key = str(agent_file)
                try:
                    offset = int(cursors.get(key, 0))
                except (TypeError, ValueError):
                    offset = 0
                new_offset, last_ts = _ingest_transcript(
                    conn, project["id"], agent_file, session_id, agent_id, offset, counts
                )
                cursors[key] = new_offset
                occurred = last_ts
                if not occurred:
                    try:
                        occurred = store.from_mtime(agent_file.stat().st_mtime)
                    except OSError:
                        occurred = store.now_iso()
                _insert_event(
                    conn, project["id"], agent_id, session_id, "subagent_run",
                    str(meta.get("description") or ""), str(meta.get("agentType") or ""),
                    1 if meta.get("finished") else 0, occurred, counts,
                )

    settings = project.get("settings_json")
    if isinstance(settings, str):
        settings = store.loads_json(settings, {})
    if not isinstance(settings, dict):
        settings = {}
    settings["transcript_cursors"] = cursors
    conn.execute(
        "UPDATE agent_projects SET settings_json = ? WHERE id = ?",
        (store.dumps_json(settings), project["id"]),
    )
    return counts


# ---------------------------------------------------------------------------
# 执行记录（session jsonl 聚合，B-07）
# ---------------------------------------------------------------------------
def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return str(block.get("text") or "")
    return ""


def _scan_jsonl(path: Path) -> dict[str, Any]:
    """读取一个 session jsonl 的首末时间戳、最后事件与模型。"""
    info: dict[str, Any] = {
        "first": None,
        "last": None,
        "last_type": None,
        "last_stop": None,
        "last_error": False,
        "model": None,
        "summary": None,
    }
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return info
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            ts = store.iso_utc(rec.get("timestamp"))
            if ts:
                if info["first"] is None:
                    info["first"] = ts
                info["last"] = ts
            rec_type = str(rec.get("type") or "")
            info["last_type"] = rec_type or info["last_type"]
            message = rec.get("message")
            if isinstance(message, dict):
                model = message.get("model")
                if model:
                    info["model"] = str(model)
                stop = message.get("stop_reason")
                if stop:
                    info["last_stop"] = str(stop)
            if rec.get("is_error") or rec.get("error"):
                info["last_error"] = True
            else:
                info["last_error"] = False
            if rec_type == "result" and rec.get("is_error"):
                info["last_error"] = True
            if info["summary"] is None and rec_type == "user" and message is not None:
                text = _message_text(message).strip()
                if text and not text.startswith("<"):
                    info["summary"] = text[:500]
    return info


def _infer_execution_status(scan: dict[str, Any], meta: dict[str, Any] | None) -> str:
    if scan.get("last_error"):
        return "failed"
    if meta is not None:
        finished = meta.get("finished")
        if finished is False:
            return "running"
        if finished is True:
            return "succeeded"
    last_type = scan.get("last_type")
    if last_type == "result":
        return "succeeded"
    if last_type == "assistant":
        return "succeeded" if scan.get("last_stop") in (None, "end_turn", "stop_sequence") else "running"
    if last_type in ("user", "progress", "system"):
        return "running"
    return "succeeded" if scan.get("last") else "unknown"


def _duration_ms(first: str | None, last: str | None) -> int | None:
    if not first or not last:
        return None
    try:
        start = datetime.fromisoformat(first.replace("Z", "+00:00"))
        end = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(int((end - start).total_seconds() * 1000), 0)


def _upsert_execution(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    session_id: str,
    agent_id: str,
    path: Path,
    meta: dict[str, Any] | None = None,
) -> None:
    scan = _scan_jsonl(path)
    status = _infer_execution_status(scan, meta)
    summary = (str(meta.get("description") or "").strip() if meta else "") or scan.get("summary")
    error_summary = None
    if meta and meta.get("error"):
        error_summary = str(meta.get("error"))[:500]
    elif status == "failed" and scan.get("summary"):
        error_summary = scan["summary"][:500]
    token_row = conn.execute(
        """
        SELECT COALESCE(SUM(total_tokens), 0) AS total
        FROM token_usage_events
        WHERE project_id = ? AND session_id = ? AND agent_id = ?
        """,
        (project_id, session_id, agent_id),
    ).fetchone()
    total_tokens = int(token_row["total"] or 0) if token_row else 0
    execution_id = store.stable_id("exec", project_id, session_id, agent_id)
    conn.execute(
        """
        INSERT INTO executions
            (id, project_id, agent_id, task_id, session_id, status, started_at, finished_at,
             duration_ms, summary, error_summary, model_id, total_tokens, source)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'transcript')
        ON CONFLICT(project_id, session_id, agent_id) DO UPDATE SET
            status = excluded.status,
            started_at = excluded.started_at,
            finished_at = excluded.finished_at,
            duration_ms = excluded.duration_ms,
            summary = excluded.summary,
            error_summary = excluded.error_summary,
            model_id = excluded.model_id,
            total_tokens = excluded.total_tokens,
            source = excluded.source
        """,
        (
            execution_id,
            project_id,
            agent_id,
            session_id,
            status,
            scan.get("first"),
            scan.get("last"),
            _duration_ms(scan.get("first"), scan.get("last")),
            summary[:500] if summary else None,
            error_summary,
            scan.get("model"),
            total_tokens,
        ),
    )


def backfill_executions(conn: sqlite3.Connection, project: dict[str, Any]) -> int:
    root = projects_root()
    role_agents = _role_map(conn, project["id"])
    owner_agent_id = role_agents.get("general-owner") or "general-owner"
    count = 0
    for slug in project_slugs(project):
        slug_dir = root / slug
        if not slug_dir.is_dir():
            continue
        for path in sorted(slug_dir.glob("*.jsonl")):
            _upsert_execution(
                conn, project["id"], session_id=path.stem,
                agent_id=owner_agent_id, path=path,
            )
            count += 1
        for session_dir in sorted(p for p in slug_dir.iterdir() if p.is_dir()):
            sub = session_dir / "subagents"
            if not sub.is_dir():
                continue
            for meta_path in sorted(sub.glob("*.meta.json")):
                meta = _read_json(meta_path)
                agent_file = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
                if not agent_file.is_file():
                    continue
                role_key = _norm_role(str(meta.get("agentType") or ""))
                agent_id = role_agents.get(role_key) or role_key
                _upsert_execution(
                    conn, project["id"], session_id=session_dir.name,
                    agent_id=agent_id, path=agent_file, meta=meta,
                )
                count += 1
    return count


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------
def backfill(conn: sqlite3.Connection, project: dict[str, Any]) -> dict[str, int]:
    counts = {
        "tasks": backfill_tasks(conn, project),
        "agents": backfill_agents(conn, project),
        "deliveries": backfill_deliveries(conn, project),
    }
    counts.update(backfill_events_and_tokens(conn, project))
    counts["executions"] = backfill_executions(conn, project)
    return counts


# ---------------------------------------------------------------------------
# 会话实时流（主会话 + subagent 的 transcript）
# ---------------------------------------------------------------------------
SESSION_STALE_SECONDS = 300


def worktree_paths(project: dict[str, Any]) -> list[Path]:
    """项目的 Git worktree 目录（``~/agent-team/<code>-team/worktrees/*``）。

    执行器是在这些 worktree 里跑 claude 的，它们的会话 transcript 也按各自
    工作目录的 slug 落盘；不扫这些目录，「实时会话」就只剩主会话。
    """
    from . import config as config_module

    code = str(project.get("id") or project.get("code") or "").strip()
    if not code:
        return []
    base = config_module.team_dir(code) / "worktrees"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir())


def project_slugs(project: dict[str, Any]) -> list[str]:
    """配置里的 slug + 由 repo_root 推导的本机 slug + 各 worktree 的 slug。

    配置常从别的机器抄来（Mac 的 ``-Users-hualei-*`` 到服务器上并不存在），
    只按配置扫会得到 0 个会话，所以扫描时始终并上本机推导值。
    """
    slugs = [str(x) for x in (project.get("session_slugs") or [])]
    repo_root = str(project.get("repo_root") or "").strip()
    if repo_root:
        derived = repo_root.replace("/", "-")
        if derived not in slugs:
            slugs.append(derived)
    for path in worktree_paths(project):
        derived = str(path).replace("/", "-")
        if derived not in slugs:
            slugs.append(derived)
    return slugs


def session_files(project: dict[str, Any]) -> list[dict[str, Any]]:
    """按项目登记的 slug 列出会话 transcript 文件。

    主会话：``<slug>/<session_id>.jsonl``；
    Subagent：``<slug>/<session_id>/subagents/agent-<agent_id>.jsonl``。
    """
    root = projects_root()
    out: list[dict[str, Any]] = []
    wt_slugs = {str(p).replace("/", "-"): p for p in worktree_paths(project)}
    for slug in project_slugs(project):
        slug_dir = root / str(slug)
        if not slug_dir.is_dir():
            continue
        wt = wt_slugs.get(str(slug))
        for path in sorted(slug_dir.glob("*.jsonl")):
            out.append({"path": path, "session_id": path.stem, "kind": "main",
                        "agent_id": None, "meta": None,
                        "worktree": wt.name if wt else None})
        for session_dir in sorted(p for p in slug_dir.iterdir() if p.is_dir()):
            sub = session_dir / "subagents"
            if not sub.is_dir():
                continue
            for meta_path in sorted(sub.glob("*.meta.json")):
                transcript = meta_path.with_name(meta_path.name.replace(".meta.json", ".jsonl"))
                if not transcript.is_file():
                    continue
                agent_id = meta_path.name[len("agent-"):].replace(".meta.json", "") if meta_path.name.startswith("agent-") else None
                out.append({"path": transcript, "session_id": session_dir.name, "kind": "subagent",
                            "agent_id": agent_id, "meta": _read_json(meta_path)})
    return out


def _trace_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content[:1200]
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image":
                    parts.append("[图片]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)[:1200]
    return ""


def _trace_tool_detail(payload: dict[str, Any]) -> str:
    for key in ("description", "command", "file_path", "pattern", "query", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")[:160]
    return ""


def parse_transcript_trace(path: Path, limit: int = 160) -> dict[str, Any]:
    """把 transcript 还原成时间线：思考 / 文本 / 工具调用+结果。"""
    events: list[dict[str, Any]] = []
    pending: dict[str, int] = {}
    tokens = 0
    model = None
    started_at = None
    last_at = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                ts = rec.get("timestamp")
                if ts:
                    started_at = started_at or ts
                    last_at = ts
                message = rec.get("message") or {}
                if not isinstance(message, dict):
                    continue
                if message.get("model"):
                    model = message["model"]
                usage = message.get("usage") or {}
                if isinstance(usage, dict):
                    tokens += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind == "thinking":
                        text = str(block.get("thinking") or "").strip()
                        if text:
                            events.append({"kind": "thinking", "text": text[:900], "at": ts})
                    elif kind == "text":
                        text = str(block.get("text") or "").strip()
                        if text:
                            events.append({"kind": "text", "text": text[:900], "at": ts})
                    elif kind == "tool_use":
                        pending[str(block.get("id"))] = len(events)
                        events.append({
                            "kind": "tool",
                            "tool": block.get("name"),
                            "detail": _trace_tool_detail(block.get("input") or {}),
                            "at": ts,
                        })
                    elif kind == "tool_result":
                        output = _trace_result_text(block)
                        ok = not block.get("is_error")
                        idx = pending.get(str(block.get("tool_use_id")))
                        if idx is not None and idx < len(events):
                            events[idx]["result"] = output
                            events[idx]["ok"] = ok
                        else:
                            events.append({"kind": "result", "text": output, "ok": ok, "at": ts})
    except OSError:
        return {"ok": False, "error": "unreadable"}
    total = len(events)
    return {
        "ok": True,
        "events": events[-limit:] if total > limit else events,
        "events_total": total,
        "truncated": total > limit,
        "tokens": tokens,
        "model": model,
        "started_at": started_at,
        "last_at": last_at,
    }
