#!/usr/bin/env python3
"""多项目 Agent 管理平台（本地只读）。

把「谁在干活、干到哪了、产出在哪」变成一块屏：

- **角色花名册**：1 Owner + 6 角色，状态优先读 `ai-team/status/*.json` 自报，
  读不到才回退到 transcript 启发式（运行中 / 已完成 / 疑似停滞）；
- **Kanban**：任务板五列（待办 / 进行中 / 评审测试 / 已完成 / 阻塞）；
- **Worktree**：每个角色独占的 checkout、分支、HEAD、未提交改动、最近提交；
- **执行过程**：每个 subagent 最近 5 次工具调用（借鉴 AutoGen Studio 的消息流）；
- **成本**：优先调 `ccusage --json`（若已安装），否则本地按 token 统计（不算钱，
  避免用过时价目表编造金额）；
- **证据链**：任务 → 提交 SHA → 交付报告。

数据全部来自本机磁盘，不联网（`ccusage` 仅在本地已安装时调用）、不写仓库：

| 数据源 | 位置 |
|---|---|
| 自报状态 | `ai-team/status/<role>.json`（`scripts/agent_status.py` 写入） |
| 子代理登记 | `~/.claude/projects/<slug>/<session>/subagents/*.meta.json` |
| 子代理过程 | 同目录 `*.jsonl` |
| 会话标题 | `<session>/custom-title.json` |
| 工作树 | `git worktree list` + `status` + `log` |
| 任务板 | `ai-team/TASK_BOARD.md` |
| 交付物 | `ai-team/reports/`、`docs/reports/` |

用法::

    python3 scripts/agents_dashboard.py            # http://127.0.0.1:8099
    python3 scripts/agents_dashboard.py --open
    python3 scripts/agents_dashboard.py --once      # 只打印一份 JSON 快照

只监听 127.0.0.1。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

try:  # 以脚本方式运行时 app/ 已在 sys.path；以模块方式运行时走包路径
    from agent_platform import auth as auth_service
    from agent_platform import config as platform_config
    from agent_platform import executor as executor_service
    from agent_platform import executor_policy
    from agent_platform import openapi as openapi_service
    from agent_platform import resources as resource_service
    from agent_platform import runtime as runtime_service
    from agent_platform import scheduler as scheduler_service
    from agent_platform import service as platform_service
    from agent_platform import store as platform_store
    from agent_platform import team as team_service
    from agent_platform import windows as windows_service
    from agent_platform import workspace as workspace_service
    from agent_platform.p4 import autonomy as autonomy_service
    from agent_platform.p4 import catalog as catalog_service
    from agent_platform.p4 import evaluation as evaluation_service
    from agent_platform.p4 import events as p4_events
    from agent_platform.p4 import governance as p4_governance
    from agent_platform.p4 import improvement as improvement_service
    from agent_platform.p4 import killswitch as p4_killswitch
    from agent_platform.p4 import knowledge as knowledge_service
    from agent_platform.p4 import outcome as outcome_service
    from agent_platform.p4 import policy as p4_policy
    from agent_platform.p4 import release as release_service
    from agent_platform.p4 import workflow as workflow_service
    from agent_platform.state_cache import StateCache
except ImportError:  # pragma: no cover
    from app.agent_platform import auth as auth_service
    from app.agent_platform import config as platform_config
    from app.agent_platform import executor as executor_service
    from app.agent_platform import executor_policy
    from app.agent_platform import openapi as openapi_service
    from app.agent_platform import resources as resource_service
    from app.agent_platform import runtime as runtime_service
    from app.agent_platform import scheduler as scheduler_service
    from app.agent_platform import service as platform_service
    from app.agent_platform import store as platform_store
    from app.agent_platform import team as team_service
    from app.agent_platform import workspace as workspace_service
    from app.agent_platform.p4 import autonomy as autonomy_service
    from app.agent_platform.p4 import catalog as catalog_service
    from app.agent_platform.p4 import evaluation as evaluation_service
    from app.agent_platform.p4 import governance as p4_governance
    from app.agent_platform.p4 import improvement as improvement_service
    from app.agent_platform.p4 import killswitch as p4_killswitch
    from app.agent_platform.p4 import knowledge as knowledge_service
    from app.agent_platform.p4 import outcome as outcome_service
    from app.agent_platform.p4 import policy as p4_policy
    from app.agent_platform.p4 import release as release_service
    from app.agent_platform.p4 import workflow as workflow_service
    from app.agent_platform.state_cache import StateCache

#: 默认项目仓库根（平台已脱离单一仓库，由 config/projects.json 决定）
REPO = platform_config.default_repo_root()
PROJECTS_ROOT = Path(
    os.environ.get("AGWS_PLATFORM_PROJECTS_ROOT") or (Path.home() / ".claude" / "projects")
).expanduser()
STATUS_DIR = REPO / "ai-team" / "status"


def _repo() -> Path:
    """当前默认项目仓库根（可被 scan_* 的 repo 参数覆盖）。"""
    return REPO


def _slug_for(repo: Path) -> str:
    return repo.resolve().as_posix().replace("/", "-")


def _slug_prefixes() -> tuple[str, ...]:
    """会话 slug：config 里所有项目仓库 + 历史迁移前的旧 slug。"""
    roots = []
    # 配置异常不阻断看板：坏配置等于只是少了几个 slug 前缀
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        for item in platform_config.load_projects_config().get("projects", []):
            raw = item.get("repo_root")
            if raw:
                roots.append(_slug_for(Path(raw).expanduser()))
    roots.extend([_slug_for(REPO), "-Users-hualei-deepseek"])
    return tuple(dict.fromkeys(roots))


SLUG_PREFIXES = _slug_prefixes()


def _slug_matches(name: str, slugs: tuple[str, ...]) -> bool:
    """会话目录名是否属于该项目。

    配置里的 slug 是**精确名单**（含迁移前的旧 slug），不再用前缀匹配：
    同目录下 ``...-repo`` 会前缀命中 ``...-repo-b``，跨项目串台正是这么来的。
    """
    return name in slugs


def _configured_slugs_for(repo: Path) -> tuple[str, ...]:
    """配置里该项目登记的 session slug（含迁移前旧 slug）；未登记时只认仓库自身 slug。

    每个项目只能看到自己的会话目录，跨项目串台会让看板的 Owner / Agent 卡片
    显示成别的项目的数据。
    """
    target = _slug_for(repo)
    # 配置异常不阻断看板：坏配置退回「只认自身 slug」这一最保守的范围
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError, AttributeError):
        for item in platform_config.load_projects_config().get("projects", []):
            raw = item.get("repo_root")
            if not raw or _slug_for(Path(raw).expanduser()) != target:
                continue
            slugs = [str(s) for s in (item.get("session_slugs") or []) if s]
            return tuple(dict.fromkeys([target, *slugs]))
    return (target,)


def _prefixes_for(repo: Path | None = None) -> tuple[str, ...]:
    """只属于某个项目的 slug 前缀：仓库自身 slug + 配置登记的历史旧 slug。"""
    if repo is None:
        return SLUG_PREFIXES
    return _configured_slugs_for(repo)

ROLE_BY_DIR = {
    "内部平台": ("General Owner", "主会话 / 主仓库"),
    "fe": ("Frontend Engineer", "独立 worktree"),
    "be": ("Backend Engineer", "独立 worktree"),
    "qa-review": ("QA & DevOps", "独立 worktree / 只读审查"),
}

ROLE_BY_KEYWORD = (
    ("PM", "Product Manager"),
    ("产品", "Product Manager"),
    ("架构", "System Architect"),
    ("architect", "System Architect"),
    ("UX", "UX Designer"),
    ("UI", "UX Designer"),
    ("QA", "QA & DevOps"),
    ("复审", "QA & DevOps"),
    ("测试", "QA & DevOps"),
    ("前端", "Frontend Engineer"),
    ("frontend", "Frontend Engineer"),
    ("后端", "Backend Engineer"),
    ("backend", "Backend Engineer"),
)

ROLE_SLUG = {
    "general owner": "general-owner",
    "product manager": "product-manager",
    "system architect": "system-architect",
    "ux designer": "ux-designer",
    "frontend engineer": "frontend-engineer",
    "backend engineer": "backend-engineer",
    "qa & devops": "qa-devops",
}

DISPLAY_BY_SLUG = {
    "general-owner": "General Owner",
    "product-manager": "Product Manager",
    "system-architect": "System Architect",
    "ux-designer": "UX Designer",
    "frontend-engineer": "Frontend Engineer",
    "backend-engineer": "Backend Engineer",
    "qa-devops": "QA & DevOps",
}

STALE_SECONDS = 300
RECENT_TOOL_LIMIT = 5
USAGE_CACHE_SECONDS = 120
_usage_cache: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


def _git(*args: str, cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd or _repo()),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _slug(role: str) -> str:
    return ROLE_SLUG.get(role.strip().lower(), re.sub(r"[^a-z0-9]+", "-", role.strip().lower()).strip("-"))


# ---------------------------------------------------------------------------
# 自报状态（STATUS.json 协议）
# ---------------------------------------------------------------------------
def scan_status_files(repo: Path | None = None) -> dict[str, dict[str, Any]]:
    """读 `ai-team/status/*.json`：角色 slug → 自报状态。"""
    out: dict[str, dict[str, Any]] = {}
    root = repo or _repo()
    status_dir = root / "ai-team" / "status"
    if not status_dir.is_dir():
        return out
    for path in status_dir.glob("*.json"):
        data = _read_json(path)
        if not data.get("role"):
            continue
        data["_file"] = str(path.relative_to(root))
        out[_slug(str(data["role"]))] = data
    return out


# ---------------------------------------------------------------------------
# 子代理 / 会话
# ---------------------------------------------------------------------------
def _role_of(description: str, agent_type: str) -> str:
    text = f"{description} {agent_type}"
    for keyword, role in ROLE_BY_KEYWORD:
        if keyword.lower() in text.lower():
            return role
    return "Subagent"


def _tool_detail(payload: dict[str, Any]) -> str:
    for key in ("description", "command", "file_path", "pattern", "query", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")[:160]
    return ""


def _scan_transcript(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "started_at": None,
        "last_event_at": None,
        "last_tool": None,
        "last_tool_detail": None,
        "last_kind": None,
        "events": 0,
        "tokens": 0,
        "model": None,
        "recent_tools": [],
    }
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return info

    info["events"] = len(lines)
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ts = rec.get("timestamp")
        if ts:
            if info["started_at"] is None:
                info["started_at"] = ts
            info["last_event_at"] = ts
        message = rec.get("message") or {}
        if not isinstance(message, dict):
            continue
        usage = message.get("usage") or {}
        if isinstance(usage, dict):
            info["tokens"] += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        if message.get("model"):
            info["model"] = message["model"]
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name")
                detail = _tool_detail(block.get("input") or {})
                info["last_tool"] = name
                info["last_tool_detail"] = detail
                info["last_kind"] = "tool"
                info["recent_tools"].append({"name": name, "detail": detail, "at": ts})
            elif block.get("type") == "tool_result":
                info["last_kind"] = "waiting"
            elif block.get("type") == "text" and str(block.get("text", "")).strip():
                info["last_kind"] = "text"
                info["last_tool"] = None
                info["last_tool_detail"] = str(block["text"]).strip()[:160]
    info["recent_tools"] = info["recent_tools"][-RECENT_TOOL_LIMIT:]
    return info


def _agent_entry(meta_path: Path) -> dict[str, Any] | None:
    agent_id = meta_path.name.replace("agent-", "").replace(".meta.json", "")
    meta = _read_json(meta_path)
    transcript = meta_path.with_name(f"agent-{agent_id}.jsonl")
    if not transcript.exists():
        return None

    info = _scan_transcript(transcript)
    try:
        mtime = transcript.stat().st_mtime
    except OSError:
        mtime = None

    finished = bool(meta.get("finished"))
    description = str(meta.get("description") or "")
    agent_type = str(meta.get("agentType") or "general-purpose")

    if finished:
        status, status_text = "done", "已完成"
    elif mtime and (time.time() - mtime) > STALE_SECONDS:
        status, status_text = "stalled", "疑似停滞"
    else:
        status, status_text = "running", "运行中"

    started_ts = _parse_iso(info["started_at"])
    last_ts = _parse_iso(info["last_event_at"]) or mtime
    duration = int(last_ts - started_ts) if started_ts and last_ts else None

    return {
        "id": agent_id,
        "role": _role_of(description, agent_type),
        "description": description,
        "agent_type": agent_type,
        "status": status,
        "status_text": status_text,
        "started_at": info["started_at"],
        "last_event_at": _iso(last_ts),
        "duration_s": duration,
        "last_tool": info["last_tool"],
        "last_detail": info["last_tool_detail"],
        "last_kind": info["last_kind"],
        "tokens": info["tokens"],
        "model": info["model"],
        "recent_tools": info["recent_tools"],
        "transcript": str(transcript),
    }


def _latest_owner_session(repo: Path | None = None) -> Path | None:
    """当前仓库 slug 下最新的主会话目录；无会话时依次回退历史 slug。"""
    if not PROJECTS_ROOT.is_dir():
        return None
    for slug in _prefixes_for(repo):
        slug_dir = PROJECTS_ROOT / slug
        if not slug_dir.is_dir():
            continue
        candidates = [
            p for p in slug_dir.iterdir()
            if p.is_dir() and p.with_suffix(".jsonl").is_file()
        ]
        if candidates:
            return max(candidates, key=lambda p: p.with_suffix(".jsonl").stat().st_mtime)
    return None


def scan_agents(limit_sessions: int = 6, per_session: int = 30,
                repo: Path | None = None) -> dict[str, Any]:
    prefixes = _prefixes_for(repo)
    sessions: list[dict[str, Any]] = []
    if PROJECTS_ROOT.is_dir():
        for slug_dir in PROJECTS_ROOT.iterdir():
            if not slug_dir.is_dir() or not _slug_matches(slug_dir.name, prefixes):
                continue
            for session in slug_dir.iterdir():
                sub = session / "subagents"
                if not sub.is_dir():
                    continue
                metas = sorted(sub.glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not metas:
                    continue
                sessions.append({
                    "session": session,
                    "slug": slug_dir.name,
                    "session_id": session.name,
                    "mtime": max(p.stat().st_mtime for p in metas),
                    "metas": metas,
                })

    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    agents: list[dict[str, Any]] = []
    owner: dict[str, Any] = {}

    # Owner 取当前仓库 slug 下最新的主会话（即使本轮还没开子代理）；
    # 当前 slug 无会话时才回退到历史 slug，避免主会话卡片停留在迁移前的旧会话。
    owner_session = _latest_owner_session(repo)
    if owner_session is not None:
        title = _read_json(owner_session / "custom-title.json").get("customTitle")
        owner = {
            "title": title or "General Owner",
            "session_id": owner_session.name,
            "slug": owner_session.parent.name,
            "last_event_at": _iso(owner_session.with_suffix(".jsonl").stat().st_mtime),
        }

    for idx, item in enumerate(sessions[:limit_sessions]):
        for meta_path in item["metas"][:per_session]:
            entry = _agent_entry(meta_path)
            if entry is None:
                continue
            entry["session_id"] = item["session_id"]
            agents.append(entry)
        if idx == 0 and not owner:
            title = _read_json(item["session"] / "custom-title.json").get("customTitle")
            main_jsonl = item["session"].with_suffix(".jsonl")
            owner = {
                "title": title or "General Owner",
                "session_id": item["session_id"],
                "slug": item["slug"],
                "last_event_at": _iso(main_jsonl.stat().st_mtime) if main_jsonl.exists() else None,
            }

    merged: dict[str, dict[str, Any]] = {}
    for entry in agents:
        current = merged.get(entry["id"])
        if current is None or (entry["last_event_at"] or "") > (current["last_event_at"] or ""):
            merged[entry["id"]] = entry
    ordered = sorted(merged.values(), key=lambda a: a["last_event_at"] or "", reverse=True)
    return {"owner": owner, "agents": ordered}


# ---------------------------------------------------------------------------
# 成本（ccusage 优先，本地 token 兜底）
# ---------------------------------------------------------------------------
def scan_usage(repo: Path | None = None) -> dict[str, Any]:
    """优先调本地已安装的 `ccusage --json`；否则按本地 transcript 统计 token。

    刻意**不**在兜底路径里估算金额：价目表会变，编一个数字比不给更糟。
    """
    now = time.time()
    key = "global" if repo is None else _slug_for(repo)
    cached = _usage_cache.get(key)
    if cached and (now - float(cached["at"])) < USAGE_CACHE_SECONDS:
        return cached["value"]

    if repo is None:
        # ccusage 按整机会话统计，只用于默认项目的「本机总用量」
        value = _usage_via_ccusage()
        if value is None:
            value = _usage_local()
    else:
        # 项目维度必须按 slug 过滤，ccusage 无法区分项目
        value = _usage_local(repo)
    _usage_cache[key] = {"at": now, "value": value}
    return value


def _find_ccusage() -> str | None:
    """找 ccusage：PATH 优先，再扫 nvm 各版本 bin（服务进程 PATH 常常不含 nvm）。"""
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


def _usage_via_ccusage() -> dict[str, Any] | None:
    """调 `ccusage claude daily --json`，取最近一天的用量。

    ccusage 20.x 的 JSON 形如 `{"daily": [{date, totalTokens, totalCost, ...}]}`；
    直接取日期最大的那一行（即今天），不自己聚合，避免口径和它对不上。
    """
    binary = _find_ccusage()
    if binary is None:
        return None
    for args in (["claude", "daily", "--json"], ["--json"]):
        try:
            out = subprocess.run(
                [binary, *args], capture_output=True, text=True, timeout=20, check=False
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0 or not out.stdout.strip():
            continue
        try:
            data = json.loads(out.stdout)
        except ValueError:
            continue
        rows = data.get("daily") or data.get("claude") or []
        if not isinstance(rows, list) or not rows:
            continue
        rows = [r for r in rows if isinstance(r, dict)]
        if not rows:
            continue
        rows.sort(key=lambda r: str(r.get("date") or r.get("period") or ""))
        latest = rows[-1]
        days = [
            {
                "date": r.get("date") or r.get("period"),
                "tokens": int(r.get("totalTokens") or 0),
                "cost": r.get("totalCost"),
            }
            for r in rows[-7:]
        ]
        models: list[dict[str, Any]] = []
        for item in latest.get("modelBreakdowns") or []:
            if not isinstance(item, dict):
                continue
            models.append({
                "name": item.get("modelName"),
                "tokens": int(item.get("inputTokens") or 0)
                + int(item.get("outputTokens") or 0)
                + int(item.get("cacheReadTokens") or 0)
                + int(item.get("cacheCreationTokens") or 0),
                "cost": item.get("cost"),
            })
        models.sort(key=lambda m: m["tokens"], reverse=True)
        return {
            "source": "ccusage",
            "available": True,
            "period": latest.get("date") or latest.get("period"),
            "total_tokens": int(latest.get("totalTokens") or 0),
            "input_tokens": int(latest.get("inputTokens") or 0),
            "output_tokens": int(latest.get("outputTokens") or 0),
            "cache_read_tokens": int(latest.get("cacheReadTokens") or 0),
            "cost_usd": latest.get("totalCost"),
            "models": latest.get("modelsUsed") or [],
            "model_breakdown": models[:4],
            "days": days,
            "note": "",
        }
    return None


def _usage_local(repo: Path | None = None) -> dict[str, Any]:
    """按本地 transcript 统计最近 24h token。"""
    prefixes = _prefixes_for(repo)
    cutoff = time.time() - 86400
    total = 0
    files = 0
    if PROJECTS_ROOT.is_dir():
        for slug_dir in PROJECTS_ROOT.iterdir():
            if not slug_dir.is_dir() or not _slug_matches(slug_dir.name, prefixes):
                continue
            for path in slug_dir.rglob("*.jsonl"):
                try:
                    if path.stat().st_mtime < cutoff:
                        continue
                except OSError:
                    continue
                files += 1
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            try:
                                rec = json.loads(line)
                            except ValueError:
                                continue
                            usage = (rec.get("message") or {}).get("usage") or {}
                            if isinstance(usage, dict):
                                total += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                except OSError:
                    continue
    return {
        "source": "local-jsonl",
        "available": True,
        "total_tokens": total,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "files": files,
        "note": "未安装 ccusage，按本机 transcript 统计最近 24h token（不估算金额）",
    }


# ---------------------------------------------------------------------------
# 单个 agent 的实时执行流
# ---------------------------------------------------------------------------
TRACE_MAX_EVENTS = 160


def _find_agent_files(agent_id: str) -> tuple[Path, Path] | None:
    """agent id → (transcript, meta)。会话目录按 slug 分，逐个找。"""
    if not re.fullmatch(r"[0-9a-zA-Z\-]{6,64}", agent_id or ""):
        return None
    if not PROJECTS_ROOT.is_dir():
        return None
    for slug_dir in PROJECTS_ROOT.iterdir():
        if not slug_dir.is_dir() or not _slug_matches(slug_dir.name, SLUG_PREFIXES):
            continue
        for session in slug_dir.iterdir():
            sub = session / "subagents"
            if not sub.is_dir():
                continue
            transcript = sub / f"agent-{agent_id}.jsonl"
            if transcript.exists():
                return transcript, sub / f"agent-{agent_id}.meta.json"
    return None


def _result_text(block: dict[str, Any]) -> str:
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


def scan_agent_trace(agent_id: str, limit: int = TRACE_MAX_EVENTS) -> dict[str, Any]:
    """把 subagent transcript 还原成时间线：思考 / 文本 / 工具调用+结果。

    tool_use 与 tool_result 按 tool_use_id 配对，结果回填到发起它的那一步上，
    和 VS Code Agents 窗口看到的是同一种结构。
    """
    located = _find_agent_files(agent_id)
    if located is None:
        return {"ok": False, "error": "not_found", "agent_id": agent_id}

    transcript, meta_path = located
    meta = _read_json(meta_path)
    events: list[dict[str, Any]] = []
    pending: dict[str, int] = {}
    tokens = 0
    model = None
    started_at = None
    last_at = None

    try:
        with transcript.open("r", encoding="utf-8", errors="replace") as fh:
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
                            "detail": _tool_detail(block.get("input") or {}),
                            "at": ts,
                            "agent_id": agent_id,
                        })
                    elif kind == "tool_result":
                        output = _result_text(block)
                        ok = not block.get("is_error")
                        idx = pending.get(str(block.get("tool_use_id")))
                        if idx is not None and idx < len(events):
                            events[idx]["result"] = output
                            events[idx]["ok"] = ok
                        else:
                            events.append({"kind": "result", "text": output, "ok": ok, "at": ts})
    except OSError:
        return {"ok": False, "error": "unreadable", "agent_id": agent_id}

    total = len(events)
    trimmed = events[-limit:] if total > limit else events
    try:
        mtime = transcript.stat().st_mtime
    except OSError:
        mtime = None

    finished = bool(meta.get("finished"))
    if finished:
        status, status_text = "done", "已完成"
    elif mtime and (time.time() - mtime) > STALE_SECONDS:
        status, status_text = "stalled", "疑似停滞"
    else:
        status, status_text = "running", "运行中"

    return {
        "ok": True,
        "agent_id": agent_id,
        "description": meta.get("description") or "",
        "agent_type": meta.get("agentType") or "",
        "status": status,
        "status_text": status_text,
        "started_at": started_at,
        "last_event_at": _iso(mtime) or last_at,
        "tokens": tokens,
        "events_total": total,
        "truncated": total > limit,
        "events": trimmed,
        "model": model,
    }


# ---------------------------------------------------------------------------
# Worktree
# ---------------------------------------------------------------------------
def scan_worktrees(repo: Path | None = None) -> list[dict[str, Any]]:
    raw = _git("worktree", "list", "--porcelain", cwd=repo or _repo())
    items: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in raw.splitlines():
        if line.startswith("worktree "):
            if current:
                items.append(current)
            current = {"path": line[len("worktree "):], "head": None, "branch": None, "detached": False}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):][:8]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].replace("refs/heads/", "")
        elif line == "detached":
            current["detached"] = True
    if current:
        items.append(current)

    for item in items:
        path = Path(item["path"])
        role, form = ROLE_BY_DIR.get(path.name, (path.name, "worktree"))
        item["role"] = role
        item["form"] = form
        item["present"] = path.exists()
        if not item["present"]:
            continue
        changes = _git("status", "--porcelain", cwd=path).strip()
        item["dirty"] = len([ln for ln in changes.splitlines() if ln.strip()])
        item["last_commit"] = _git("log", "-1", "--format=%h %s", cwd=path).strip()
        item["branch"] = item["branch"] or "(detached)"
    return items


# ---------------------------------------------------------------------------
# 任务板 / 交付物 / 证据链
# ---------------------------------------------------------------------------
KNOWN_STATUSES = frozenset({"BACKLOG", "READY", "IN_PROGRESS", "REVIEW", "TESTING", "DONE", "BLOCKED", "CANCELLED"})


def parse_task_board(repo: Path | None = None) -> list[dict[str, str]]:
    path = (repo or _repo()) / "ai-team" / "TASK_BOARD.md"
    if not path.is_file():
        return []
    tasks: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6 or not re.match(r"^TEAM-\d+$", cells[0]):
            continue
        # 标题里如果混入了未转义的 "|"，会把列撑开；找到第一个合法状态列再回填
        status_at = next((i for i, c in enumerate(cells) if c in KNOWN_STATUSES), None)
        if status_at is None or status_at < 3:
            continue
        title = " ".join(p for p in cells[1:status_at] if p)
        rest = cells[status_at:]
        tasks.append({
            "id": cells[0],
            "title": title,
            "owner": rest[1] if len(rest) > 1 else "",
            "status": rest[0],
            "priority": rest[2] if len(rest) > 2 else "",
            "depends": rest[3] if len(rest) > 3 else "",
            "ref": rest[4] if len(rest) > 4 else "",
            "qa": rest[5] if len(rest) > 5 else "",
        })
    return tasks


def scan_reports(repo: Path | None = None, limit: int = 20) -> list[dict[str, Any]]:
    root = repo or _repo()
    out: list[dict[str, Any]] = []
    for folder in (root / "ai-team" / "reports", root / "docs" / "reports"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_file() or path.name.startswith("."):
                continue
            stat = path.stat()
            out.append({
                "name": path.name,
                "dir": folder.name,
                "size": stat.st_size,
                "mtime": _iso(stat.st_mtime),
                "path": str(path.relative_to(root)),
            })
    out.sort(key=lambda r: r["mtime"] or "", reverse=True)
    return out[:limit]


def attach_links(tasks: list[dict[str, str]], reports: list[dict[str, Any]],
                 repo: Path | None = None) -> dict[str, Any]:
    """给每个任务挂上「提交 SHA / 交付报告」证据链，并按状态分组做 Kanban。"""
    for task in tasks:
        ref = task.get("ref", "")
        shas = re.findall(r"\b[0-9a-f]{7,40}\b", ref)
        commits = []
        for sha in shas[:3]:
            subject = _git("log", "-1", "--format=%s", sha).strip()
            if subject:
                commits.append({"sha": sha[:8], "subject": subject})
        matched = [
            {"name": r["name"], "path": r["path"]}
            for r in reports
            if task["id"].lower() in r["name"].lower() or task["id"].replace("TEAM-", "") in r["name"]
        ][:3]
        task["commits"] = commits
        task["reports"] = matched

    columns = [
        ("todo", "待办", ("READY", "BACKLOG")),
        ("doing", "进行中", ("IN_PROGRESS",)),
        ("review", "评审 / 测试", ("REVIEW", "TESTING")),
        ("done", "已完成", ("DONE",)),
        ("blocked", "阻塞", ("BLOCKED", "CANCELLED")),
    ]
    kanban = []
    for key, label, statuses in columns:
        kanban.append({
            "key": key,
            "label": label,
            "tasks": [t for t in tasks if t["status"] in statuses],
        })
    return {"kanban": kanban, "tasks": tasks}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def _status_from_file(status: dict[str, Any] | None) -> tuple[str, str, str] | None:
    """自报状态 → (看板状态, 状态文案, 说明)。"""
    if not status:
        return None
    state = str(status.get("state") or "")
    note = str(status.get("note") or "")
    task = str(status.get("task") or "")
    suffix = f" · {task}" if task else ""
    mapping = {
        "running": ("running", f"自报进行中{suffix}", note),
        "done": ("done", f"自报已完成{suffix}", note),
        "blocked": ("stalled", f"自报阻塞{suffix}", note),
        "idle": ("idle", "待命", note),
    }
    return mapping.get(state)


def build_roster(agent_state: dict[str, Any], worktrees: list[dict[str, Any]],
                 status_files: dict[str, dict[str, Any]],
                 repo: Path | None = None) -> list[dict[str, Any]]:
    roster: list[dict[str, Any]] = []
    owner = agent_state["owner"]
    main_wt = next((w for w in worktrees if w["path"] == str(repo or _repo())), None)
    owner_status = status_files.get("general-owner")
    derived = _status_from_file(owner_status)
    roster.append({
        "role": "General Owner",
        "form": "主会话",
        "status": derived[0] if derived else ("running" if owner else "idle"),
        "status_text": derived[1] if derived else ("主会话运行中" if owner else "未见会话"),
        "detail": (derived[2] if derived and derived[2] else (main_wt or {}).get("last_commit") or ""),
        "meta": f"分支 {(main_wt or {}).get('branch', '-')} · 未提交 {(main_wt or {}).get('dirty', 0)}",
        "last_event_at": owner.get("last_event_at"),
        "reported": bool(derived),
        "task": (owner_status or {}).get("task"),
    })

    by_role: dict[str, list[dict[str, Any]]] = {}
    for agent in agent_state["agents"]:
        by_role.setdefault(agent["role"], []).append(agent)

    for role in ("Product Manager", "System Architect", "UX Designer"):
        group = by_role.get(role, [])
        active = [a for a in group if a["status"] == "running"]
        derived = _status_from_file(status_files.get(_slug(role)))
        roster.append({
            "role": role,
            "form": "Subagent（只读）",
            "status": derived[0] if derived else ("running" if active else ("done" if group else "idle")),
            "status_text": derived[1] if derived else ("执行中" if active else ("已完成" if group else "待命")),
            "detail": derived[2] if derived and derived[2] else (active or group or [{}])[0].get("description", ""),
            "meta": f"本轮 {len(group)} 次调用",
            "last_event_at": (active or group or [{}])[0].get("last_event_at"),
            "reported": bool(derived),
            "task": (status_files.get(_slug(role)) or {}).get("task"),
        })

    for role in ("Frontend Engineer", "Backend Engineer", "QA & DevOps"):
        group = by_role.get(role, [])
        wt = next((w for w in worktrees if w["role"] == role), None)
        active = [a for a in group if a["status"] == "running"]
        derived = _status_from_file(status_files.get(_slug(role)))
        roster.append({
            "role": role,
            "form": (wt or {}).get("form", "独立 worktree"),
            "status": derived[0] if derived else ("running" if active else ("done" if group else "idle")),
            "status_text": derived[1] if derived else ("执行中" if active else ("已完成" if group else "待命")),
            "detail": (
                derived[2] if derived and derived[2]
                else ((active or group or [{}])[0].get("description", "") or (wt or {}).get("last_commit", ""))
            ),
            "meta": (
                f"{wt['path'].split('/')[-1]} · {wt.get('branch')} · 未提交 {wt.get('dirty', 0)}"
                if wt else "无 worktree"
            ),
            "last_event_at": (active or group or [{}])[0].get("last_event_at"),
            "reported": bool(derived),
            "task": (status_files.get(_slug(role)) or {}).get("task"),
        })
    return roster


def build_state(repo: Path | None = None) -> dict[str, Any]:
    root = repo or _repo()
    # 必须把项目仓库传下去：不带 repo 会扫全部项目 slug，导致看板串台
    agent_state = scan_agents(repo=root)
    worktrees = scan_worktrees(root)
    tasks = parse_task_board(root)
    reports = scan_reports(root)
    status_files = scan_status_files(root)
    linked = attach_links(tasks, reports, root)
    roster = build_roster(agent_state, worktrees, status_files, root)

    # 抽屉按 roster 里的展示名查找，所以这里用展示名做 key
    statuses = {
        display: status_files[slug]
        for slug, display in DISPLAY_BY_SLUG.items()
        if slug in status_files
    }
    return {
        "generated_at": _iso(time.time()),
        "owner": agent_state["owner"],
        "roster": roster,
        "statuses": statuses,
        "kanban": linked["kanban"],
        "tasks": linked["tasks"],
        "agents": agent_state["agents"],
        "worktrees": worktrees,
        "reports": reports,
        "usage": scan_usage(root),
        "stats": {
            "agents_running": len([a for a in agent_state["agents"] if a["status"] == "running"]),
            "agents_total": len(agent_state["agents"]),
            "tasks_in_progress": len([t for t in tasks if t["status"] == "IN_PROGRESS"]),
            "tasks_ready": len([t for t in tasks if t["status"] == "READY"]),
            "tasks_done": len([t for t in tasks if t["status"] == "DONE"]),
            "reported": len(status_files),
        },
    }


def _raw_status(platform_status: str) -> str:
    return {
        "todo": "READY",
        "in_progress": "IN_PROGRESS",
        "review": "REVIEW",
        "done": "DONE",
        "blocked": "BLOCKED",
    }.get(platform_status, "BACKLOG")


def _state_from_platform_project(project: dict[str, Any]) -> dict[str, Any]:
    """非默认项目：用平台库构造旧的 build_state 字段，保证兼容层结构一致。"""
    stats = platform_service.project_stats_for(project["id"])
    task_page = platform_service.list_tasks(project["id"], page=1, page_size=100)
    tasks = [
        {
            "id": item["task_key"],
            "title": item["title"],
            "owner": item.get("owner") or "",
            "status": _raw_status(item["status"]),
            "priority": item.get("priority") or "",
            "ref": "",
            "qa": item.get("qa") or "",
        }
        for item in task_page["items"]
    ]
    kanban = []
    for key, label, statuses in (
        ("todo", "待办", ("READY", "BACKLOG")),
        ("doing", "进行中", ("IN_PROGRESS",)),
        ("review", "评审 / 测试", ("REVIEW", "TESTING")),
        ("done", "已完成", ("DONE",)),
        ("blocked", "阻塞", ("BLOCKED", "CANCELLED")),
    ):
        kanban.append({"key": key, "label": label,
                       "tasks": [t for t in tasks if t["status"] in statuses]})

    agents = project.get("agents") or []
    roster = []
    display_status = {"working": "running", "idle": "idle", "blocked": "stalled", "offline": "offline"}
    last_event = None
    for agent in agents:
        meta = agent.get("metadata_json") or {}
        report = meta.get("self_report") or {}
        stamp = agent.get("last_heartbeat_at")
        if stamp and (last_event is None or stamp > last_event):
            last_event = stamp
        roster.append({
            "role": agent["display_name"],
            "form": agent.get("execution_mode") or "",
            "status": display_status.get(agent.get("status"), "idle"),
            "status_text": report.get("note") or agent.get("status") or "",
            "detail": report.get("note") or "",
            "meta": " · ".join(filter(None, [agent.get("current_branch"), agent.get("worktree_path")])),
            "last_event_at": agent.get("last_heartbeat_at"),
            "reported": bool(report),
            "task": report.get("task"),
        })
    owner_agent = next((a for a in agents if a.get("role_key") == "general-owner"), None)
    owner = {
        "title": project["name"],
        "session_id": None,
        "slug": None,
        "last_event_at": (owner_agent or {}).get("last_heartbeat_at"),
    }
    return {
        "generated_at": _iso(time.time()),
        "owner": owner,
        "roster": roster,
        "statuses": {},
        "kanban": kanban,
        "tasks": tasks,
        "agents": [],
        "worktrees": [],
        "reports": [],
        "usage": {
            "source": "token_usage_events",
            "available": True,
            "total_tokens": stats["token_24h"],
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
            "note": "项目 transcript 明细汇总（最近 24h）",
        },
        "stats": {
            "agents_running": stats["agent_active"],
            "agents_total": stats["agent_total"],
            "tasks_in_progress": stats["task_in_progress"],
            "tasks_ready": stats["task_todo"],
            "tasks_done": stats["task_done"],
            "reported": len([a for a in agents if (a.get("metadata_json") or {}).get("self_report")]),
        },
    }


_STATE_CACHE: StateCache | None = None
_STATE_CACHE_LOCK = threading.Lock()


def _state_cache() -> StateCache:
    """进程级状态缓存：磁盘快照放在平台库同级 state-cache/。"""
    global _STATE_CACHE
    with _STATE_CACHE_LOCK:
        if _STATE_CACHE is None:
            db_path = Path(platform_store.default_db_path())
            _STATE_CACHE = StateCache(
                db_path.parent / "state-cache", build_state_for_project, ttl=5.0
            )
        return _STATE_CACHE


def build_state_for_project(project_id: str) -> dict[str, Any]:
    """旧 ``/api/state`` 结构 + 新增 ``project``；默认项目复用原扫描逻辑。"""
    meta = platform_service.state_meta(project_id)
    repo_root: Path | None = None
    try:
        candidate = Path(meta["repo_root"]).expanduser().resolve()
        if candidate.is_dir():
            repo_root = candidate
    except OSError:
        repo_root = None
    if repo_root is not None:
        state = build_state(repo_root)
        # 仓库里没有 ai-team 任务板 / 自报状态时，用平台库补齐对应板块，
        # 既不空看板也不覆盖仓库里的真实文件。
        project = platform_service.get_project(project_id)
        fallback = _state_from_platform_project(project)
        if not (repo_root / "ai-team" / "TASK_BOARD.md").is_file():
            state["tasks"] = fallback["tasks"]
            state["kanban"] = fallback["kanban"]
            state["stats"].update({
                "tasks_in_progress": fallback["stats"]["tasks_in_progress"],
                "tasks_ready": fallback["stats"]["tasks_ready"],
                "tasks_done": fallback["stats"]["tasks_done"],
            })
        # 平台纳管模式 B：仓库没有 ai-team/status 自报目录时，
        # 花名册以平台库为准（执行器会写 working/idle），避免扫描不到会话就显示 offline。
        if not (repo_root / "ai-team" / "status").is_dir() and fallback["roster"]:
            state["roster"] = fallback["roster"]
            state["stats"]["agents_running"] = fallback["stats"]["agents_running"]
            state["stats"]["agents_total"] = fallback["stats"]["agents_total"]
            state["stats"]["reported"] = fallback["stats"]["reported"]
    else:
        project = platform_service.get_project(project_id)
        state = _state_from_platform_project(project)
    state["project"] = meta
    # 旧 usage 字段保持原样；项目用量单独给出，成本为 ccusage 会话成本近似归集。
    state["project_usage"] = platform_service.project_usage(project_id)
    # 用量明细走平台库（token_usage_events）；本机 ccusage 只作为兜底
    try:
        state["usage_breakdown"] = platform_service.usage_breakdown(project_id)
    except Exception:  # noqa: BLE001 - 用量明细失败不影响主状态
        state["usage_breakdown"] = None
    return state



def _tailscale_ip() -> str | None:
    """取本机 Tailscale IPv4（macOS 应用包路径 / Homebrew 都可能）。"""
    candidates = [
        "/Applications/Tailscale.app/Contents/MacOS/tailscale",
        "/opt/homebrew/bin/tailscale",
        "/usr/local/bin/tailscale",
    ]
    binary = next((c for c in candidates if Path(c).exists()), shutil.which("tailscale"))
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "ip", "-4"], capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    ip = out.stdout.strip().splitlines()[0].strip() if out.stdout.strip() else ""
    return ip or None



# ---------------------------------------------------------------------------
# 本地文件：预览 / 用默认程序打开
# ---------------------------------------------------------------------------
MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}


def safe_repo_path(raw: str | None, repo: Path | None = None) -> Path | None:
    """把外部传入的路径限制在指定项目仓库内，防目录穿越。"""
    if not raw:
        return None
    root = (repo or _repo()).resolve()
    candidate = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def safe_project_path(project: dict[str, Any], raw: str | None) -> Path | None:
    """把外部路径限制在“当前项目 repo_root”内；不把 project 参数拼进路径。"""
    if not raw:
        return None
    try:
        root = Path(project["repo_root"]).resolve()
    except (KeyError, OSError):
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def file_viewer_html(name: str, raw_path: str, text: str | None, error: str | None = None) -> str:
    """独立查看页：新标签页直接可读，避免裸 text/plain 被浏览器下载或空白。"""
    safe_name = html_lib.escape(name or "文件")
    safe_path = html_lib.escape(raw_path or "")
    if error:
        body = f'<div class="err">{html_lib.escape(error)}</div>'
        status = "无法预览"
    else:
        body = f'<pre class="doc">{html_lib.escape(text or "")}</pre>'
        status = "只读预览 · 内容来自项目仓库"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_name} · 文件预览</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: #0e1116; color: #e6e9ef;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }}
header {{ position: sticky; top: 0; background: #141922; border-bottom: 1px solid #263041;
  padding: 12px 20px; display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: baseline; z-index: 1; }}
header strong {{ font-size: 14px; }}
header .path {{ font-family: ui-monospace, Menlo, monospace; font-size: 11.5px; color: #8b97ab; word-break: break-all; }}
header .tag {{ margin-left: auto; font-size: 11px; color: #8b97ab; }}
.doc {{ margin: 0; padding: 20px; font-family: ui-monospace, Menlo, monospace; font-size: 12.5px;
  line-height: 1.6; white-space: pre-wrap; word-break: break-word; tab-size: 2; }}
.err {{ margin: 20px; padding: 14px 16px; border: 1px solid #5a2530; border-radius: 8px;
  background: #2a1519; color: #ffb4c0; font-size: 13px; }}
</style></head>
<body>
<header><strong>{safe_name}</strong><span class="path">{safe_path}</span><span class="tag">{status}</span></header>
{body}
</body></html>"""


def open_with_os(path: Path) -> bool:
    """macOS 用默认程序打开（xdg-open 兜底）。仅本机看板使用。"""
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if not shutil.which(opener):
        return False
    try:
        subprocess.run([opener, str(path)], check=False, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
#: 资源类型 → 业务域。Kill Switch 支持按域停（如只停 material 的评估）。
_POLICY_DOMAINS = {
    "evaluation_run": "evaluation",
    "evaluation_dataset": "evaluation",
    "knowledge_item": "knowledge",
    "knowledge_source": "knowledge",
    "workflow_version": "workflow",
    "agent_release": "release",
    "improvement_case": "improvement",
    "outcome_record": "outcome",
}


class PlatformError_AccessDenied(Exception):
    """项目级授权未通过；响应已由闸门写出，调用方只需停止。"""


def _policy_domain(resource_type: str) -> str | None:
    return _POLICY_DOMAINS.get(resource_type)


def _run_dispatch_item(project_id: str, item_id: str, *, dry_run: bool, owner: str) -> dict[str, Any]:
    """校验项目归属后交给执行器。"""
    conn = platform_store.connect()
    try:
        platform_store.init_db(conn)
        row = conn.execute(
            "SELECT project_id FROM dispatch_items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None or row["project_id"] != project_id:
            raise scheduler_service.SchedulerError("not_found", 404, "调度项不存在")
    finally:
        conn.close()
    try:
        return executor_service.run_item(item_id, owner=owner, dry_run=dry_run)
    except executor_service.ExecutorError as exc:
        raise scheduler_service.SchedulerError(exc.code, exc.status, exc.message) from exc


def _executor_status() -> dict[str, Any]:
    """执行器开关状态，供 UI 判断是否展示「执行」按钮。"""
    return {
        "global_enabled": executor_policy.executor_enabled(),
        "env_switch": executor_policy.ENV_SWITCH,
        "allowed_tools": list(executor_policy.ALLOWED_TOOLS),
        "algorithms_note": "平台只拉起 claude，禁 push/部署/删数据",
    }


def _project_dict(project_id: str) -> dict[str, Any]:
    conn = platform_store.connect()
    try:
        platform_store.init_db(conn)
        row = conn.execute("SELECT * FROM agent_projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise platform_service.PlatformError("project_not_found", 404)
        return platform_service._project_dict(row)
    finally:
        conn.close()


def _team_status(project_id: str) -> dict[str, Any]:
    project = _project_dict(project_id)
    conn = platform_store.connect()
    try:
        platform_store.init_db(conn)
        return team_service.team_status(project, conn)
    finally:
        conn.close()


def _team_owner(project_id: str) -> dict[str, Any]:
    return team_service.owner_session_info(_project_dict(project_id))


def _team_preflight(project_id: str) -> dict[str, Any]:
    return team_service.preflight(_project_dict(project_id))


def _team_launch(project_id: str, instruction: str, *, actor: str = "local") -> dict[str, Any]:
    return team_service.launch_team(_project_dict(project_id), instruction, actor=actor)


def _team_instruct(project_id: str, session_id: str, instruction: str, *,
                   actor: str = "local") -> dict[str, Any]:
    return team_service.instruct_team(_project_dict(project_id), session_id, instruction, actor=actor)


def _team_attach(project_id: str, session_id: str, instruction: str = "",
                 *, actor: str = "local") -> dict[str, Any]:
    return team_service.attach_session(_project_dict(project_id), session_id,
                                       instruction, actor=actor)


def _team_logs(project_id: str, session_id: str, *, lines: int = 200) -> dict[str, Any]:
    return team_service.session_logs(_project_dict(project_id), session_id, lines=lines)


def _team_stop(project_id: str, session_id: str, *, actor: str = "local") -> dict[str, Any]:
    return team_service.stop_team(_project_dict(project_id), session_id, actor=actor)


# ---------------------------------------------------------------------------
# 窗口派发：各机器上打开的 Claude CLI 窗口
# ---------------------------------------------------------------------------
def _windows_list(project_id: str) -> dict[str, Any]:
    with platform_store.session() as conn:
        return windows_service.list_windows(conn, project_id)


def _windows_dispatch(project_id: str, device_id: str, session_id: str,
                      instruction: str, *, actor: str = "local") -> dict[str, Any]:
    return windows_service.enqueue_command(project_id, device_id, session_id, instruction, actor=actor)


def _windows_bind(project_id: str, device_id: str, session_id: str, *, actor: str = "local") -> dict[str, Any]:
    return windows_service.bind_window(project_id, device_id, session_id, actor=actor)


def _device_report(device_id: str, sessions: Any) -> dict[str, Any]:
    return windows_service.report_sessions(device_id, sessions if isinstance(sessions, list) else [])


def _device_traces(device_id: str, traces: Any) -> dict[str, Any]:
    return windows_service.report_traces(device_id, traces if isinstance(traces, list) else [])


def _device_pull(device_id: str, limit: int = 10) -> dict[str, Any]:
    return windows_service.pull_commands(device_id, limit=limit)


def _device_result(device_id: str, command_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return windows_service.command_result(
        device_id,
        command_id,
        ok=bool(body.get("ok")),
        session_id=(str(body.get("session_id")) if body.get("session_id") else None),
        result=str(body.get("result") or ""),
        error=str(body.get("error") or ""),
    )


def _schedulable_for(project_id: str) -> dict[str, Any]:
    """某项目当前可调度与被跳过的任务（只读）。"""
    conn = platform_store.connect()
    try:
        platform_store.init_db(conn)
        ready, skipped = scheduler_service.schedulable(conn, project_id)
        return {"ready": ready, "skipped": skipped}
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def _authorized(self, path: str, query: dict[str, list[str]]) -> bool:
        """所有请求先过鉴权，再过治理闸门；不通过时直接写响应并返回 False。

        顺序即 V6.0 §7.5 的前三步：认证主体 → 确定主体 → 全局 Kill Switch。
        项目级授权在各路由用 ``_policy_guard`` 单独判定（需要项目上下文）。
        """
        self.principal = None
        ok, reason = auth_service.check_request(
            path=path,
            client_address=self.client_address[0] if self.client_address else None,
            headers=self.headers,
            query_params=query,
        )
        if ok:
            return self._governance_gate(path)
        self.send_response(401 if reason in {"missing_token", "invalid_token"} else 403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        body = json.dumps({
            "ok": False, "error": reason,
            "message": "需要访问令牌" if reason.endswith("token") else "该接口仅限本机访问",
            "detail": {"code": reason, "message": reason},
        }, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        return False

    def _governance_gate(self, path: str) -> bool:
        """解析主体 + 全局 Kill Switch。失败时已写响应并返回 False。

        **fail-closed（R1 / S-5）**：治理依赖不可用时拒绝，不得降级为匿名全权限。
        早期实现在 ``except Exception`` 里把 ``principal`` 置空后直接放行，
        导致 Policy 库异常 / Kill Switch 查询失败时请求越过全部治理。
        现在区分三种失败：

        * 401 ``unauthenticated``：解析不到主体（未认证）；
        * 403 ``unknown_principal``：主体存在但不可用（停用/过期）；
        * 503 ``governance_unavailable``：治理依赖（DB/Policy/KillSwitch）故障。

        只读健康检查（``PUBLIC_PATHS``）不走本闸门，治理故障时仍能探活。
        """
        if path in auth_service.PUBLIC_PATHS:
            return True

        address = self.client_address[0] if self.client_address else None
        # 鉴权关闭时（默认），所有来源都按本机所有者处理：
        # 没有令牌就没有"远端主体"这个概念，避免新建项目后自己被 403。
        loopback = auth_service.is_loopback(address) or not auth_service.auth_enabled()

        try:
            conn = platform_store.connect()
        except Exception:  # noqa: BLE001 - 连不上库属于治理依赖不可用
            self.principal = None
            self._governance_fail(503, "governance_unavailable", "治理依赖不可用（数据库）")
            return False

        try:
            self.principal = p4_governance.resolve_principal(conn, is_loopback=loopback)
            blocked, why = p4_killswitch.is_blocked(conn)
        except Exception:  # noqa: BLE001 - Policy/主体/KillSwitch 任何异常都拒绝
            self.principal = None
            self._governance_fail(503, "governance_unavailable",
                                  "治理依赖不可用（主体或 Kill Switch 查询失败）")
            return False
        finally:
            conn.close()

        if blocked:
            self._json(503, {"ok": False, "error": "kill_switch",
                             "message": why,
                             "detail": {"code": "kill_switch", "message": why}})
            return False

        if self.principal is None:
            self._governance_fail(401, "unauthenticated", "无法确定请求主体")
            return False

        # 主体存在但不可用（停用/过期）同样拒绝，不落到"匿名"。
        try:
            active, reason = p4_governance.principal_active(self.principal)
        except Exception:  # noqa: BLE001
            active, reason = False, "principal_check_failed"
        if not active:
            self._governance_fail(403, "unknown_principal", f"主体不可用：{reason}")
            return False

        return True

    def _governance_fail(self, status: int, code: str, message: str) -> None:
        """治理闸门的统一失败出口：写入错误响应，不泄露其他项目是否存在。"""
        self._json(status, {
            "ok": False,
            "error": code,
            "message": message,
            "detail": {"code": code, "message": message},
        })

    def _policy_guard(self, project_id: str | None, resource_type: str,
                      action: str, *, risk: str = "R2") -> bool:
        """项目级授权判定。不通过时已写 403/412 并返回 False。"""
        conn = platform_store.connect()
        try:
            decision = p4_policy.decide(
                conn,
                principal=getattr(self, "principal", None),
                project_id=project_id,
                resource_type=resource_type,
                action=action,
                risk=risk,
                domain=_policy_domain(resource_type),
            )
        except Exception:  # noqa: BLE001 - 判定异常按拒绝处理（Deny by Default）
            decision = p4_policy.Decision(allowed=False, reason="policy_error")
        finally:
            conn.close()
        if decision.allowed:
            return True
        code = 412 if decision.requires_approval else 403
        self._json(code, {
            "ok": False,
            "error": "policy_denied",
            "message": "需要审批" if decision.requires_approval else "无权执行该操作",
            "detail": {"code": decision.reason, "message": decision.reason},
        })
        return False

    def _gate_project(self, project_id: str, action: str = "read") -> bool:
        """项目级粗粒度闸门：所有项目路由都必须先过这一关。

        细粒度权限（谁能发布、谁能审批）在各自路由上用 ``_policy_guard`` 再判；
        这里是"连这个项目都不该看见"的底线，按 HTTP 语义区分读写。
        """
        return self._policy_guard(project_id, "*", action, risk="R1")

    def _actor_id(self, *, required: bool = True) -> str:
        """取可信的操作者主体 ID（R1 / S-2）。

        审批、授权这类动作的身份只能来自认证上下文 ``self.principal``，
        绝不能信任请求体里的 ``approved_by`` / ``requested_by``——否则同一人
        用两个不同的字符串就能伪造出「两个审批人」，职责分离形同虚设。

        ``required=False`` 时返回空串（仍有 principal 就用它），用于非审批类记录。
        """
        principal = getattr(self, "principal", None)
        pid = str((principal or {}).get("id") or "").strip()
        if pid:
            return pid
        if required:
            raise PlatformError_AccessDenied()
        return ""

    #: 需要专用权限才能「开启」的高风险设置（R1 / S-6）。
    #: 关闭属于安全动作，任何有项目写权限的人都可以立即执行。
    HIGH_RISK_SETTINGS = frozenset({"execution_enabled"})

    def _guard_high_risk_settings(self, project_id: str, payload: dict[str, Any]) -> bool:
        """高风险设置变更闸门（R1 / S-6）。

        原来 ``execution_enabled`` 在 ``PATCHABLE_SETTINGS`` 里，普通 ``update``
        权限就能把项目执行器从关打开——等于用普通写权限换来了「允许平台起进程」。

        简化方案：
        * 关闭（true → false）：放行，作为紧急安全动作，但写审计；
        * 开启（false → true）：要求 ``exec_runtime:toggle`` 专用权限（owner 级）；
        * 两种情况都记录 before/after/actor/原因，便于事后追责。

        返回 False 表示已写响应、调用方应停止。
        """
        raw = payload.get("settings", payload.get("settings_json"))
        if not isinstance(raw, dict):
            return True
        touched = self.HIGH_RISK_SETTINGS & set(raw)
        if not touched:
            return True

        project = platform_service.state_meta(project_id)
        current = dict(project.get("settings_json") or {})

        for key in sorted(touched):
            before = bool(current.get(key))
            after = bool(raw.get(key))
            if before == after:
                continue  # 值没变，不算高风险变更
            action = "enable" if after else "disable"
            actor = self._actor_id()
            if not actor:
                return False
            # 开启需要专用权限；关闭是安全动作，只要有项目写权限即可
            if after and not self._policy_guard(project_id, "exec_runtime", "toggle", risk="R4"):
                return False
            platform_service.audit(
                platform_store.connect(), project_id,
                f"settings.{key}.{action}", "project_setting", project_id,
                detail={"key": key, "before": before, "after": after,
                        "reason": str(payload.get("reason") or "")[:200]},
                actor=actor,
            )
        return True

    def _eval_platform(self, fn, *args, **kwargs) -> None:
        """评估域统一出口：自带连接的 _platform 变体。"""
        conn = platform_store.connect()
        try:
            self._json(200, fn(conn, *args, **kwargs))
        except evaluation_service.EvaluationError as exc:
            self._error(exc)
        except Exception:  # noqa: BLE001
            self._json(500, {"ok": False, "error": "internal_error",
                             "detail": {"code": "internal_error", "message": "内部错误"}})
        finally:
            conn.close()

    def _knowledge_platform(self, fn, *args, **kwargs) -> None:
        conn = platform_store.connect()
        try:
            self._json(200, fn(conn, *args, **kwargs))
        except knowledge_service.KnowledgeError as exc:
            self._error(exc)
        except Exception:  # noqa: BLE001
            self._json(500, {"ok": False, "error": "internal_error",
                             "detail": {"code": "internal_error", "message": "内部错误"}})
        finally:
            conn.close()

    def _workflow_platform(self, fn, *args, **kwargs) -> None:
        conn = platform_store.connect()
        try:
            self._json(200, fn(conn, *args, **kwargs))
        except workflow_service.WorkflowError as exc:
            self._error(exc)
        except Exception:  # noqa: BLE001
            self._json(500, {"ok": False, "error": "internal_error",
                             "detail": {"code": "internal_error", "message": "内部错误"}})
        finally:
            conn.close()

    def _release_platform(self, fn, *args, **kwargs) -> None:
        conn = platform_store.connect()
        try:
            self._json(200, fn(conn, *args, **kwargs))
        except release_service.ReleaseError as exc:
            self._error(exc)
        except Exception:  # noqa: BLE001
            self._json(500, {"ok": False, "error": "internal_error",
                             "detail": {"code": "internal_error", "message": "内部错误"}})
        finally:
            conn.close()

    def _p4_call(self, fn, exc_types, *args, **kwargs) -> None:
        conn = platform_store.connect()
        try:
            self._json(200, fn(conn, *args, **kwargs))
        except exc_types as exc:
            self._error(exc)
        except Exception:  # noqa: BLE001
            self._json(500, {"ok": False, "error": "internal_error",
                             "detail": {"code": "internal_error", "message": "内部错误"}})
        finally:
            conn.close()

    def _handle_p4_misc(self, project_id: str, rest: list[str], method: str,
                        query: dict[str, list[str]]) -> bool:
        """M5 改进 / M6 价值 / M7 自主化 三个域的路由。"""
        if not rest or rest[0] not in {"improvements", "outcome", "autonomy", "catalog"}:
            return False
        # 必须先确认是本域请求再读 body：否则会把别的 POST 请求体吃掉。
        body = self._body() if method == "POST" else {}

        imp = (improvement_service.ImprovementError,)
        out = (outcome_service.OutcomeError,)
        aut = (autonomy_service.AutonomyError,)

        # ---- M5 改进 ----
        if method == "POST" and rest == ["improvements", "failures"]:
            self._p4_call(improvement_service.record_failure, imp, project_id, body)
            return True
        if method == "GET" and rest == ["improvements", "failures"]:
            self._p4_call(improvement_service.list_patterns, imp, project_id)
            return True
        if method == "POST" and rest == ["improvements", "cases"]:
            self._p4_call(improvement_service.open_case, imp, project_id, body)
            return True
        if method == "GET" and rest == ["improvements", "cases"]:
            self._p4_call(improvement_service.list_cases, imp, project_id)
            return True
        if (method == "POST" and len(rest) == 4 and rest[:2] == ["improvements", "cases"]
                and rest[3] == "root-cause"):
            self._p4_call(improvement_service.assess_root_cause, imp, rest[2], body)
            return True
        if method == "POST" and rest == ["improvements", "proposals"]:
            self._p4_call(improvement_service.create_proposal, imp, project_id, body)
            return True
        if (method == "POST" and len(rest) == 4
                and rest[:2] == ["improvements", "proposals"] and rest[3] == "sandbox"):
            self._p4_call(improvement_service.run_sandbox, imp, rest[2],
                          str(body.get("eval_run_id") or ""))
            return True
        if (method == "POST" and len(rest) == 4
                and rest[:2] == ["improvements", "proposals"]
                and rest[3] == "request-approval"):
            if not self._policy_guard(project_id, "change_proposal", "request_approval",
                                      risk="R3"):
                return True
            actor = self._actor_id(required=False)
            if not actor:
                return True
            self._p4_call(improvement_service.request_approval, imp, rest[2],
                          requested_by=actor)
            return True
        if (method == "POST" and len(rest) == 4
                and rest[:2] == ["improvements", "approvals"] and rest[3] == "approve"):
            if not self._policy_guard(project_id, "change_proposal", "approve", risk="R4"):
                return True
            actor = self._actor_id()
            if not actor:
                return True
            self._p4_call(improvement_service.approve, imp, rest[2],
                          approved_by=actor,
                          note=body.get("note"))
            return True

        # ---- M6 价值 ----
        if method == "POST" and rest == ["outcome", "metrics"]:
            self._p4_call(outcome_service.define_metric, out, project_id, body)
            return True
        if method == "GET" and rest == ["outcome", "metrics"]:
            self._p4_call(outcome_service.list_metrics, out, project_id)
            return True
        if method == "POST" and rest == ["outcome", "records"]:
            self._p4_call(outcome_service.record_value, out, project_id, body)
            return True
        if method == "GET" and rest == ["outcome", "summary"]:
            self._p4_call(outcome_service.summarize, out, project_id,
                          (query.get("metric_key") or [""])[0])
            return True
        if method == "GET" and rest == ["outcome", "cost"]:
            self._p4_call(outcome_service.cost_summary, out, project_id)
            return True

        # ---- M8 能力目录（P1）----
        cat = (catalog_service.CatalogError,)
        if method == "POST" and rest == ["catalog", "listings"]:
            self._p4_call(catalog_service.publish_listing, cat, project_id, body)
            return True
        if method == "GET" and rest == ["catalog", "listings"]:
            self._p4_call(catalog_service.available_for_project, cat, project_id)
            return True
        if method == "POST" and rest == ["catalog", "grants"]:
            if not self._policy_guard(project_id, "capability_grant", "create", risk="R3"):
                return True
            self._p4_call(
                catalog_service.grant, cat, str(body.get("listing_id") or ""), body,
                granted_by=str(body.get("granted_by") or "local"),
            )
            return True
        if method == "GET" and rest == ["catalog", "grants"]:
            self._p4_call(catalog_service.list_grants, cat, project_id)
            return True
        if (method == "POST" and len(rest) == 4
                and rest[:2] == ["catalog", "grants"] and rest[3] == "revoke"):
            if not self._policy_guard(project_id, "capability_grant", "revoke", risk="R3"):
                return True
            self._p4_call(catalog_service.revoke_grant, cat, rest[2],
                          reason=str(body.get("reason") or "未说明"),
                          revoked_by=str(body.get("revoked_by") or "local"))
            return True

        # ---- M7 自主化 ----
        if method == "POST" and rest == ["autonomy", "eligibilities"]:
            self._p4_call(
                autonomy_service.get_or_create, aut, project_id,
                task_type=str(body.get("task_type") or "default"),
                tool_scope=str(body.get("tool_scope") or "read_write_code"),
                environment=str(body.get("environment") or "dev"),
            )
            return True
        if method == "GET" and rest == ["autonomy", "eligibilities"]:
            self._p4_call(autonomy_service.list_for_project, aut, project_id)
            return True
        if (method == "POST" and len(rest) == 4
                and rest[:2] == ["autonomy", "eligibilities"] and rest[3] == "evidence"):
            self._p4_call(autonomy_service.submit_evidence, aut, rest[2],
                          body.get("evidence") or {})
            return True
        if (method == "POST" and len(rest) == 4
                and rest[:2] == ["autonomy", "eligibilities"] and rest[3] == "approve"):
            if not self._policy_guard(project_id, "autonomy_eligibility", "approve", risk="R4"):
                return True
            actor = self._actor_id()
            if not actor:
                return True
            self._p4_call(autonomy_service.approve_level, aut, rest[2],
                          level=str(body.get("level") or "A0"),
                          approved_by=actor,
                          reason=str(body.get("reason") or "未说明"))
            return True
        if (method == "POST" and len(rest) == 4
                and rest[:2] == ["autonomy", "eligibilities"] and rest[3] == "suspend"):
            self._p4_call(autonomy_service.suspend, aut, rest[2],
                          reason=str(body.get("reason") or "未说明"))
            return True
        return False

    def _handle_release(self, project_id: str, rest: list[str], method: str) -> bool:
        """发布治理域路由。已处理返回 True。"""
        if not rest or rest[0] not in {"releases", "approvals", "canaries", "rollbacks"}:
            return False
        body = self._body() if method == "POST" else {}

        if method == "POST" and rest == ["releases"]:
            if not self._policy_guard(project_id, "agent_release", "create"):
                return True
            self._release_platform(release_service.create_release, project_id, body)
            return True
        if method == "GET" and rest == ["releases"]:
            self._release_platform(release_service.list_releases, project_id)
            return True
        if method == "GET" and len(rest) == 2 and rest[0] == "releases":
            self._release_platform(release_service.get_release, rest[1])
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "releases"
                and rest[2] == "validate"):
            self._release_platform(release_service.validate_release, rest[1])
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "releases"
                and rest[2] == "evaluation"):
            self._release_platform(
                release_service.attach_evaluation, rest[1],
                str(body.get("eval_run_id") or ""),
            )
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "releases"
                and rest[2] == "request-approval"):
            if not self._policy_guard(project_id, "agent_release", "request_approval", risk="R3"):
                return True
            actor = self._actor_id(required=False)
            if not actor:
                return True
            self._release_platform(
                release_service.request_approval, rest[1],
                requested_by=actor,
                note=body.get("note"),
            )
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "releases"
                and rest[2] == "publish"):
            if not self._policy_guard(project_id, "agent_release", "publish", risk="R4"):
                return True
            self._release_platform(release_service.publish, rest[1])
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "releases"
                and rest[2] == "canary"):
            if not self._policy_guard(project_id, "agent_release", "canary", risk="R3"):
                return True
            self._release_platform(
                release_service.start_canary, rest[1],
                traffic_pct=int(body.get("traffic_pct") or 5),
                stop_threshold=body.get("stop_threshold") or {},
                created_by=str(body.get("created_by") or "local"),
            )
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "releases"
                and rest[2] == "rollback"):
            if not self._policy_guard(project_id, "agent_release", "rollback", risk="R4"):
                return True
            self._release_platform(
                release_service.rollback, rest[1],
                reason=str(body.get("reason") or "未说明"),
                deployment_id=body.get("deployment_id"),
                created_by=str(body.get("created_by") or "local"),
            )
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "approvals"
                and rest[2] == "approve"):
            if not self._policy_guard(project_id, "agent_release", "approve", risk="R4"):
                return True
            actor = self._actor_id()
            if not actor:
                return True
            self._release_platform(
                release_service.approve, rest[1],
                approved_by=actor,
                note=body.get("note"),
            )
            return True
        if method == "GET" and rest == ["canaries"]:
            self._release_platform(release_service.list_canaries, project_id)
            return True
        if method == "GET" and rest == ["rollbacks"]:
            self._release_platform(release_service.list_rollbacks, project_id)
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "canaries"
                and rest[2] == "observe"):
            self._release_platform(
                release_service.observe_canary, rest[1], body.get("metrics") or {}
            )
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "canaries"
                and rest[2] == "promote"):
            self._release_platform(release_service.promote_canary, rest[1])
            return True
        return False

    def _handle_workflow(self, project_id: str, rest: list[str], method: str) -> bool:
        """Workflow 域路由。已处理返回 True。"""
        if not rest or rest[0] not in {
            "workflows", "workflow-versions", "workflow-bindings", "workflow-runs",
            "workflow-approvals",
        }:
            return False
        body = self._body() if method == "POST" else {}

        if method == "POST" and rest == ["workflows"]:
            if not self._policy_guard(project_id, "workflow_definition", "create"):
                return True
            self._workflow_platform(workflow_service.create_definition, project_id, body)
            return True
        if method == "GET" and rest == ["workflows"]:
            self._workflow_platform(workflow_service.list_definitions, project_id)
            return True
        if (method == "POST" and len(rest) == 3
                and rest[0] == "workflows" and rest[2] == "versions"):
            if not self._policy_guard(project_id, "workflow_version", "create"):
                return True
            self._workflow_platform(workflow_service.create_version, rest[1], body)
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "workflow-versions"
                and rest[2] == "test"):
            self._workflow_platform(workflow_service.test_version, rest[1])
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "workflow-versions"
                and rest[2] == "publish"):
            if not self._policy_guard(project_id, "workflow_version", "publish", risk="R3"):
                return True
            self._workflow_platform(
                workflow_service.publish_version, rest[1],
                published_by=str(body.get("published_by") or "local"),
            )
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "workflow-versions"
                and rest[2] == "bind"):
            if not self._policy_guard(project_id, "workflow_binding", "create", risk="R3"):
                return True
            self._workflow_platform(
                workflow_service.bind, project_id, rest[1],
                materialize=bool(body.get("materialize", True)),
                created_by=str(body.get("created_by") or "local"),
            )
            return True
        if method == "GET" and rest == ["workflow-bindings"]:
            self._workflow_platform(workflow_service.list_bindings, project_id)
            return True

        # ---- 运行实例与 Human Gate（R3 / M3） ----
        if method == "GET" and rest == ["workflow-runs"]:
            self._workflow_platform(workflow_service.list_runs, project_id)
            return True
        if method == "POST" and rest == ["workflow-runs"]:
            if not self._policy_guard(project_id, "workflow_run", "start", risk="R3"):
                return True
            self._workflow_platform(
                workflow_service.start_run, project_id,
                str(body.get("binding_id") or ""),
                inputs=body.get("inputs") or {},
                correlation_id=body.get("correlation_id"),
            )
            return True
        if method == "GET" and len(rest) == 2 and rest[0] == "workflow-runs":
            self._workflow_platform(workflow_service.get_run, rest[1])
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "workflow-runs"
                and rest[2] == "approve"):
            # 审批人必须来自可信 Principal（R1 / S-2 的同一规则），不接受自报。
            actor = self._actor_id()
            self._workflow_platform(
                workflow_service.approve_node, rest[1],
                str(body.get("node_key") or ""),
                approver=actor,
                reason=str(body.get("reason") or ""),
                decision=str(body.get("decision") or "approved"),
                expires_at=body.get("expires_at"),
            )
            return True
        if (method == "POST" and len(rest) == 3 and rest[0] == "workflow-approvals"
                and rest[2] == "revoke"):
            actor = self._actor_id()
            self._workflow_platform(
                workflow_service.revoke_approval, rest[1],
                reason=str(body.get("reason") or "未说明"),
                revoked_by=actor,
            )
            return True
        return False

    def _handle_knowledge(self, project_id: str, rest: list[str], method: str,
                          query: dict[str, list[str]]) -> bool:
        """知识域路由。已处理返回 True。"""
        if not rest or rest[0] != "knowledge":
            return False
        body = self._body() if method == "POST" else {}

        if method == "POST" and rest == ["knowledge", "sources"]:
            if not self._policy_guard(project_id, "knowledge_source", "create"):
                return True
            self._knowledge_platform(knowledge_service.create_source, project_id, body)
            return True
        if method == "GET" and rest == ["knowledge", "sources"]:
            if not self._policy_guard(project_id, "knowledge_source", "read"):
                return True
            self._knowledge_platform(knowledge_service.list_sources, project_id)
            return True
        if method == "POST" and rest == ["knowledge", "items"]:
            if not self._policy_guard(project_id, "knowledge_item", "create"):
                return True
            self._knowledge_platform(knowledge_service.create_item, project_id, body)
            return True
        if method == "GET" and rest == ["knowledge", "items"]:
            self._knowledge_platform(knowledge_service.list_items, project_id)
            return True
        if (method == "POST" and len(rest) == 4
                and rest[:2] == ["knowledge", "versions"] and rest[3] == "publish"):
            if not self._policy_guard(project_id, "knowledge_item", "publish", risk="R3"):
                return True
            self._knowledge_platform(
                knowledge_service.publish_version, rest[2],
                reviewed_by=str(body.get("reviewed_by") or "local"),
            )
            return True
        if (method == "POST" and len(rest) == 4
                and rest[:2] == ["knowledge", "versions"] and rest[3] == "revoke"):
            if not self._policy_guard(project_id, "knowledge_item", "revoke", risk="R3"):
                return True
            self._knowledge_platform(
                knowledge_service.revoke_version, rest[2],
                reason=str(body.get("reason") or "未说明"),
                revoked_by=str(body.get("revoked_by") or "local"),
            )
            return True
        if method == "GET" and rest == ["knowledge", "search"]:
            principal = getattr(self, "principal", None)
            self._knowledge_platform(
                knowledge_service.search, project_id,
                (query.get("q") or [""])[0],
                limit=int((query.get("limit") or ["10"])[0]),
                principal_id=principal["id"] if principal else None,
                correlation_id=(query.get("correlation_id") or [None])[0],
            )
            return True
        return False

    def _handle_eval(self, project_id: str, rest: list[str], method: str) -> bool:
        """评估域路由。已处理返回 True。"""
        if not rest or rest[0] != "eval":
            return False
        body = self._body() if method == "POST" else {}

        if method == "POST" and rest == ["eval", "suites"]:
            if not self._policy_guard(project_id, "evaluation_suite", "create"):
                return True
            self._eval_platform(evaluation_service.create_suite, project_id, body)
            return True
        if method == "GET" and rest == ["eval", "suites"]:
            self._eval_platform(evaluation_service.list_suites, project_id)
            return True
        if method == "POST" and rest == ["eval", "datasets"]:
            if not self._policy_guard(project_id, "evaluation_dataset", "create"):
                return True
            self._eval_platform(evaluation_service.create_dataset, project_id, body)
            return True
        if method == "GET" and rest == ["eval", "datasets"]:
            self._eval_platform(evaluation_service.list_datasets, project_id)
            return True
        if (method == "POST" and len(rest) == 4
                and rest[:2] == ["eval", "datasets"] and rest[3] == "versions"):
            if not self._policy_guard(project_id, "evaluation_dataset", "update"):
                return True
            self._eval_platform(evaluation_service.freeze_version, rest[2], body)
            return True
        if method == "POST" and rest == ["eval", "evaluators"]:
            if not self._policy_guard(project_id, "evaluator", "create"):
                return True
            self._eval_platform(evaluation_service.create_evaluator, project_id, body)
            return True
        if method == "GET" and rest == ["eval", "evaluators"]:
            self._eval_platform(evaluation_service.list_evaluators, project_id)
            return True
        if method == "POST" and rest == ["eval", "runs"]:
            if not self._policy_guard(project_id, "evaluation_run", "create", risk="R2"):
                return True
            self._eval_platform(evaluation_service.run_evaluation, project_id, body)
            return True
        if method == "GET" and rest == ["eval", "runs"]:
            self._eval_platform(evaluation_service.list_runs, project_id)
            return True
        if method == "GET" and len(rest) == 3 and rest[:2] == ["eval", "runs"]:
            self._eval_platform(evaluation_service.get_run, rest[2])
            return True
        return False

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise platform_service.PlatformError("invalid_json", 400)
        if not isinstance(data, dict):
            raise platform_service.PlatformError("invalid_json", 400)
        return data

    def _fail(self, code: int, error: str, message: str) -> None:
        """统一错误体：``{ok:false, error, message, detail:{code,message}}``。

        与 ``_error()`` 同形，避免各处手工拼出字段缺失的变体。
        """
        self._json(code, {"ok": False, "error": error, "message": message,
                          "detail": {"code": error, "message": message}})

    def _not_found_route(self) -> None:
        self._fail(404, "not_found", "接口不存在")

    def _error(self, exc) -> None:
        """统一错误响应。接受 PlatformError 与同形的 SchedulerError。"""
        self._json(exc.status, {
            "ok": False, "error": exc.code, "message": exc.message,
            "detail": {"code": exc.code, "message": exc.message},
        })


    def _principal_id(self) -> str:
        """当前请求的主体 id；鉴权关闭时一律按本机所有者处理。"""
        principal = getattr(self, "principal", None)
        pid = getattr(principal, "id", None)
        if pid:
            return str(pid)
        if isinstance(principal, dict) and principal.get("id"):
            return str(principal["id"])
        return "local-owner"

    def _platform(self, fn, *args, **kwargs) -> None:
        try:
            self._json(200, fn(*args, **kwargs))
        except (platform_service.PlatformError, scheduler_service.SchedulerError,
                resource_service.ResourceError, runtime_service.RuntimeError_,
                workspace_service.ViewError, team_service.TeamError,
                windows_service.WindowError) as exc:
            self._error(exc)
        except Exception:  # noqa: BLE001 - 不向客户端泄露本机路径/堆栈
            self._json(500, {"ok": False, "error": "internal_error",
                             "detail": {"code": "internal_error", "message": "内部错误"}})

    @staticmethod
    def _public_project(project: dict[str, Any]) -> dict[str, Any]:
        # settings_json 只服务采集 cursor，属于内部状态。
        return {key: value for key, value in project.items() if key != "settings_json"}

    def _platform_project(self, fn, *args) -> None:
        try:
            self._json(200, {"project": self._public_project(fn(*args))})
        except platform_service.PlatformError as exc:
            self._error(exc)
        except Exception:  # noqa: BLE001
            self._json(500, {"ok": False, "error": "internal_error",
                             "detail": {"code": "internal_error", "message": "内部错误"}})

    def _project_from_query(self, query: dict[str, list[str]], default: str | None = None,
                            action: str = "read") -> dict[str, Any]:
        """从 query 取项目并校验访问权限（R1 / S-1）。

        早期实现只检查项目是否存在、不检查调用者是否有权访问——
        ``/view`` 与 ``/api/file`` 都走这里，等于任何持令牌者可读任意项目仓库文件。
        现在：解析不到项目 → 404；无权访问 → 抛 PlatformError_AccessDenied（响应已由闸门写出）。
        """
        project_id = (query.get("project") or [default])[0]
        if not project_id:
            raise platform_service.PlatformError("project_not_found", 404)
        project = platform_service.state_meta(project_id)
        if not self._gate_project(str(project.get("id") or project_id), action):
            raise PlatformError_AccessDenied()
        return project

    def _segments(self, path: str) -> list[str]:
        return [unquote(part) for part in path.strip("/").split("/") if part]

    def _idempotent(self, fingerprint: str, fn) -> dict[str, Any]:
        key = (self.headers.get("Idempotency-Key") or "").strip()
        if not key:
            return fn()
        if len(key) > 200:
            raise platform_service.PlatformError("invalid_payload", 422, "Idempotency-Key 过长")
        cached = platform_service.idempotency_lookup(key, fingerprint)
        if cached is not None:
            return cached
        result = fn()
        platform_service.idempotency_store(key, fingerprint, result)
        return result

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if not self._authorized(path, query):
            return
        try:
            if path == "/api/open":
                project = self._project_from_query(query)
                raw = (query.get("path") or [None])[0]
                target = safe_project_path(project, raw)
                if target is None:
                    self._fail(400, "invalid_path", "路径不可用")
                    return
                self._json(200, {"ok": open_with_os(target), "path": str(target)})
                return
            # 运行时控制（M3-3）
            if path == "/api/runtime/services":
                self._platform(runtime_service.register_service, self._body())
                return
            if path == "/api/runtime/collect":
                self._platform(runtime_service.collect_once)
                return
            if path.startswith("/api/runtime/actions/") and path.endswith("/preflight"):
                segments = self._segments(path)
                if len(segments) == 5:
                    self._platform(runtime_service.preflight, segments[3])
                    return
            if path.startswith("/api/runtime/actions/") and path.endswith("/run"):
                segments = self._segments(path)
                if len(segments) == 5 and not self._policy_guard(
                    None, "runtime_action", "run", risk="R3"
                ):
                    return
                if len(segments) == 5:
                    body = self._body()
                    self._platform(
                        runtime_service.run_action, segments[3],
                        preflight_token=body.get("preflight_token"),
                        confirm=bool(body.get("confirm")),
                        dry_run=bool(body.get("dry_run")),
                    )
                    return
            # 跨项目协作（M3-2）：共享资源是平台级，不挂在项目下
            if path == "/api/shared-resources":
                self._platform(resource_service.create_resource, self._body())
                return
            if (path.startswith("/api/shared-resources/")
                    and path.rstrip("/").endswith("/claims")):
                segments = self._segments(path)
                if len(segments) == 4:
                    self._platform(resource_service.claim_resource, segments[2], self._body())
                    return
            if path == "/api/projects":
                payload = self._body()
                fingerprint = "POST /api/projects " + hashlib.sha1(
                    json.dumps(payload, sort_keys=True).encode("utf-8")
                ).hexdigest()
                response = self._idempotent(
                    fingerprint,
                    lambda: {"project": self._public_project(platform_service.create_project(payload))},
                )
                self._json(200, response)
                return
            if path.startswith("/api/devices/"):
                segments = self._segments(path)  # api devices <id> sessions | commands [<cid> result]
                if len(segments) == 4 and segments[3] == "sessions":
                    body = self._body()
                    self._platform(_device_report, segments[2], body.get("sessions") or [])
                    return
                if len(segments) == 4 and segments[3] == "traces":
                    body = self._body()
                    self._platform(_device_traces, segments[2], body.get("traces") or [])
                    return
                if len(segments) == 6 and segments[3] == "commands" and segments[5] == "result":
                    self._platform(_device_result, segments[2], segments[4], self._body())
                    return
            if path == "/api/workspace/views":
                payload = self._body()
                principal_id = self._principal_id()

                def _save():
                    conn = platform_store.connect()
                    try:
                        return workspace_service.save_view(conn, principal_id, payload)
                    finally:
                        conn.close()

                self._platform(_save)
                return
            if path == "/api/workspace/notifications/read":
                payload = self._body()
                keys = payload.get("keys") or ([payload["key"]] if payload.get("key") else [])
                principal_id = self._principal_id()

                def _mark():
                    conn = platform_store.connect()
                    try:
                        return workspace_service.mark_notifications(
                            conn, principal_id, keys, payload.get("state") or "read"
                        )
                    finally:
                        conn.close()

                self._platform(_mark)
                return
            if path == "/api/team-templates":
                payload = self._body()
                self._platform(platform_service.create_template, payload)
                return
            segments = self._segments(path)
            if (len(segments) == 4 and segments[:2] == ["api", "team-templates"]
                    and segments[3] == "clone"):
                payload = self._body()
                self._platform(platform_service.clone_template, segments[2], payload)
                return
            if len(segments) >= 4 and segments[:2] == ["api", "projects"]:
                project_id = segments[2]
                rest = segments[3:]
                self._project_post(project_id, rest)
                return
            self._not_found_route()
        except platform_service.PlatformError as exc:
            self._error(exc)

    def _project_post(self, project_id: str, rest: list[str]) -> None:
        if not self._gate_project(project_id, "update"):
            return
        if rest == ["archive"]:
            fingerprint = f"POST /api/projects/{project_id}/archive"
            response = self._idempotent(
                fingerprint,
                lambda: {"project": self._public_project(platform_service.archive_project(project_id))},
            )
            self._json(200, response)
            return
        if rest == ["restore"]:
            self._platform_project(platform_service.restore_project, project_id)
            return
        if rest == ["backfill"]:
            self._platform(platform_service.backfill_project, project_id)
            return
        if rest == ["team", "preview"]:
            self._platform(platform_service.preview_team, project_id, self._body())
            return
        if rest == ["team", "apply"]:
            self._platform(platform_service.apply_team, project_id, self._body())
            return
        # 四期评估域（P4-M1）
        if self._handle_eval(project_id, rest, "POST"):
            return
        # 四期知识域（P4-M2）
        if self._handle_knowledge(project_id, rest, "POST", {}):
            return
        # 四期 Workflow 域（P4-M3）
        if self._handle_workflow(project_id, rest, "POST"):
            return
        # 四期发布治理域（P4-M4）
        if self._handle_release(project_id, rest, "POST"):
            return
        # 四期 改进 / 价值 / 自主化（P4-M5~M7）
        if self._handle_p4_misc(project_id, rest, "POST", {}):
            return
        if rest == ["tasks"]:
            self._platform(platform_service.create_task, project_id, self._body())
            return
        # 执行单个调度项（M3-1 T3-103）。项目与全局两道开关默认关闭。
        if len(rest) == 4 and rest[:2] == ["dispatch", "items"] and rest[3] in ("run", "dry-run"):
            body = self._body()
            dry = rest[3] == "dry-run"
            if not dry and not self._policy_guard(project_id, "dispatch_item", "run", risk="R3"):
                return
            self._platform(
                _run_dispatch_item, project_id, rest[2],
                dry_run=dry,
                owner=str(body.get("owner") or "local"),
            )
            return
        # 自主调度（M3-1）：生成计划是纯计算 + 落库，不执行任何进程。
        if rest == ["dispatch", "plan"]:
            body = self._body()
            self._platform(
                scheduler_service.create_plan, project_id,
                strategy=str(body.get("strategy") or "priority"),
                created_by=str(body.get("created_by") or "local"),
                max_items=body.get("max_items"),
                notes=body.get("notes"),
            )
            return
        if len(rest) == 3 and rest[0] == "tasks" and rest[2] == "transition":
            self._platform(platform_service.transition_task, project_id, rest[1], self._body())
            return
        if len(rest) == 3 and rest[0] == "tasks" and rest[2] == "dependencies":
            self._platform(platform_service.add_dependency, project_id, rest[1], self._body())
            return
        if rest == ["team", "launch"]:
            body = self._body()
            self._platform(_team_launch, project_id, str(body.get("instruction") or ""),
                           actor=self._principal_id())
            return
        if len(rest) == 3 and rest[0] == "team" and rest[2] == "attach":
            body = self._body()
            self._platform(_team_attach, project_id, rest[1],
                           str(body.get("instruction") or ""), actor=self._principal_id())
            return
        if (len(rest) == 3 and rest[0] == "team" and rest[2] in ("instruct", "stop")):
            body = self._body() if rest[2] == "instruct" else {}
            if rest[2] == "instruct":
                self._platform(_team_instruct, project_id, rest[1],
                               str(body.get("instruction") or ""), actor=self._principal_id())
            else:
                self._platform(_team_stop, project_id, rest[1], actor=self._principal_id())
            return
        if len(rest) == 4 and rest[0] == "windows" and rest[3] == "dispatch":
            body = self._body()
            self._platform(_windows_dispatch, project_id, rest[1], rest[2],
                           str(body.get("instruction") or ""), actor=self._principal_id())
            return
        if rest == ["windows", "bind"]:
            body = self._body()
            self._platform(_windows_bind, project_id, str(body.get("device_id") or ""),
                           str(body.get("session_id") or ""), actor=self._principal_id())
            return
        if rest == ["deliveries"]:
            self._platform(platform_service.create_delivery, project_id, self._body())
            return
        if len(rest) == 3 and rest[0] == "deliveries" and rest[2] == "review":
            self._platform(platform_service.review_delivery, project_id, rest[1], self._body())
            return
        if len(rest) == 3 and rest[0] == "deliveries" and rest[2] == "validate":
            self._platform(platform_service.validate_delivery, project_id, rest[1])
            return
        if len(rest) == 3 and rest[0] == "deliveries" and rest[2] == "resubmit":
            self._platform(platform_service.resubmit_delivery, project_id, rest[1], self._body())
            return
        if rest == ["documents"]:
            self._platform(platform_service.create_document, project_id, self._body())
            return
        if rest == ["visit"]:
            self._platform(platform_service.record_visit, project_id)
            return
        self._not_found_route()

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._authorized(path, parse_qs(parsed.query)):
            return
        try:
            segments = self._segments(path)
            if (len(segments) >= 3 and segments[:2] == ["api", "projects"]
                    and not self._gate_project(segments[2], "update")):
                return
            if len(segments) == 3 and segments[:2] == ["api", "projects"]:
                payload = self._body()
                # R1 / S-6：execution_enabled 等高风险设置走独立闸门
                if not self._guard_high_risk_settings(segments[2], payload):
                    return
                self._platform_project(platform_service.patch_project, segments[2], payload)
                return
            if len(segments) == 5 and segments[:2] == ["api", "projects"] and segments[3] == "tasks":
                payload = self._body()
                self._platform(platform_service.patch_task, segments[2], segments[4], payload)
                return
            if len(segments) == 5 and segments[:2] == ["api", "projects"] and segments[3] == "documents":
                payload = self._body()
                self._platform(platform_service.patch_document, segments[2], segments[4], payload)
                return
            self._not_found_route()
        except platform_service.PlatformError as exc:
            self._error(exc)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if not self._authorized(parsed.path, parse_qs(parsed.query)):
            return
        segments = self._segments(parsed.path)
        if (len(segments) >= 3 and segments[:2] == ["api", "projects"]
                and not self._gate_project(segments[2], "update")):
            return
        if len(segments) == 4 and segments[:2] == ["api", "projects"] and segments[3] == "favorite":
            self._platform(platform_service.set_favorite, segments[2], True)
            return
        self._not_found_route()

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not self._authorized(parsed.path, parse_qs(parsed.query)):
            return
        segments = self._segments(parsed.path)
        if (len(segments) >= 3 and segments[:2] == ["api", "projects"]
                and not self._gate_project(segments[2], "update")):
            return
        # DELETE /api/shared-resources/{id}/claims/{claim_id}
        if (len(segments) == 5 and segments[:2] == ["api", "shared-resources"]
                and segments[3] == "claims"):
            self._platform(resource_service.release_claim, segments[2], segments[4])
            return
        if (len(segments) == 7 and segments[:2] == ["api", "projects"]
                and segments[3] == "tasks" and segments[5] == "dependencies"):
            self._platform(platform_service.delete_dependency, segments[2], segments[4], segments[6])
            return
        if len(segments) == 5 and segments[:2] == ["api", "projects"] and segments[3] == "documents":
            self._platform(platform_service.delete_document, segments[2], segments[4])
            return
        if len(segments) == 4 and segments[:2] == ["api", "projects"] and segments[3] == "favorite":
            self._platform(platform_service.set_favorite, segments[2], False)
            return
        if len(segments) == 4 and segments[:2] == ["api", "workspace"] and segments[2] == "views":
            view_id = segments[3]
            principal_id = self._principal_id()

            def _delete_view():
                conn = platform_store.connect()
                try:
                    return workspace_service.delete_view(conn, principal_id, view_id)
                finally:
                    conn.close()

            self._platform(_delete_view)
            return
        self._not_found_route()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not self._authorized(parsed.path, parse_qs(parsed.query)):
            return
        path = parsed.path
        query = parse_qs(parsed.query)
        if path.startswith("/api/agent/"):
            rest = path[len("/api/agent/"):]
            agent_id, _, _ = rest.partition("/")
            trace = scan_agent_trace(agent_id)
            code = 200 if trace.get("ok") else 404
            self._send(code, json.dumps(trace, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/state":
            project_id = (query.get("project") or [platform_service.DEFAULT_PROJECT_ID])[0]
            # R1 / S-1：首屏主接口也必须过项目级授权，否则等于绕过全部隔离
            if not self._gate_project(str(project_id), "read"):
                return
            refresh = (query.get("refresh") or ["0"])[0] in ("1", "true", "yes")
            try:
                state, stale = _state_cache().get(project_id, force=refresh)
            except platform_service.PlatformError as exc:
                self._error(exc)
                return
            # 缓存命中也给出时间戳，前端可判断数据新鲜度；不改变既有字段语义。
            body = dict(state)
            # 项目元信息（名称/状态/仓库）不进缓存语义：改名/归档后必须立刻生效，
            # 否则磁盘快照会把旧项目名回放给前端。
            try:
                body["project"] = platform_service.state_meta(project_id)
            except platform_service.PlatformError:
                pass
            body["cache"] = {
                "stale": stale,
                "computed_at": state.get("generated_at"),
            }
            self._send(200, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        if path == "/healthz":
            # 健康探针：公开路径，不带令牌也能访问，只暴露存活与版本。
            self._json(200, {
                "ok": True,
                "service": "agents-dashboard",
                "schema_version": platform_store.SCHEMA_VERSION,
                "ts": platform_store.now_iso(),
            })
            return
        elif path == "/api/openapi.json":
            self._send(200, json.dumps(openapi_service.build_openapi(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif path == "/api/projects":
            self._platform(
                platform_service.list_projects,
                q=(query.get("q") or [None])[0],
                status=(query.get("status") or [None])[0],
                favorite=(query.get("favorite") or [None])[0],
            )
        elif path == "/api/runtime/services":
            self._platform(runtime_service.list_services)
        elif path == "/api/runtime/actions":
            self._platform(runtime_service.list_action_runs,
                           limit=int((query.get("limit") or ["20"])[0]))
        elif path == "/api/agent-platform/cross-project":
            self._platform(platform_service.cross_project_graph)
        elif path == "/api/shared-resources":
            self._platform(resource_service.list_resources)
        elif path.startswith("/api/shared-resources/"):
            rid = unquote(path[len("/api/shared-resources/"):].strip("/"))
            if rid:
                self._platform(resource_service.get_resource, rid)
            else:
                self._not_found_route()
        elif path == "/api/agent-platform/overview":
            self._platform(platform_service.overview)
        elif path == "/api/me/recent-projects":
            self._platform(
                platform_service.recent_projects,
                (query.get("limit") or ["20"])[0],
            )
        elif path == "/api/workspace/portfolio":
            self._platform(workspace_service.portfolio, principal_id=self._principal_id())
        elif path == "/api/workspace/my":
            self._platform(
                workspace_service.my_work,
                principal_id=self._principal_id(),
                limit=int((query.get("limit") or ["20"])[0]),
            )
        elif path.startswith("/api/devices/"):
            segments = self._segments(path)  # api devices <id> commands
            if len(segments) == 4 and segments[3] == "commands":
                self._platform(
                    _device_pull, segments[2],
                    int((query.get("limit") or ["10"])[0]),
                )
            else:
                self._not_found_route()
        elif path == "/api/workspace/views":
            self._platform(
                workspace_service.list_views,
                principal_id=self._principal_id(),
                scope=(query.get("scope") or [None])[0],
            )
        elif path == "/api/workspace/notifications":
            self._platform(
                workspace_service.notifications,
                principal_id=self._principal_id(),
                status=(query.get("status") or ["all"])[0],
                project_id=(query.get("project") or [None])[0],
                limit=int((query.get("limit") or ["100"])[0]),
            )
        elif path == "/api/search":
            raw_types = (query.get("types") or [""])[0]
            self._platform(
                workspace_service.search,
                q=(query.get("q") or [""])[0],
                types=[t.strip() for t in raw_types.split(",") if t.strip()] or None,
                project_id=(query.get("project") or [None])[0],
                limit=int((query.get("limit") or ["30"])[0]),
                principal_id=self._principal_id(),
            )
        elif path == "/api/audit-logs":
            self._platform(
                platform_service.list_audit_logs,
                project=(query.get("project") or [None])[0],
                action=(query.get("action") or [None])[0],
                from_=(query.get("from") or [None])[0],
                to=(query.get("to") or [None])[0],
                page=(query.get("page") or ["1"])[0],
                page_size=(query.get("page_size") or ["20"])[0],
            )
        elif path == "/api/team-templates":
            self._platform(platform_service.list_team_templates)
        elif path.startswith("/api/team-templates/"):
            key = unquote(path[len("/api/team-templates/"):].strip("/"))
            self._platform(
                platform_service.get_team_template, key,
                (query.get("version") or [None])[0],
            )
        elif path.startswith("/api/projects/"):
            segments = self._segments(path)
            if len(segments) < 3:
                self._not_found_route()
                return
            project_id = segments[2]
            if not self._gate_project(project_id, "read"):
                return
            rest = segments[3:]
            if not rest:
                self._platform_project(platform_service.get_project, project_id)
                return
            # 四期评估域（P4-M1）
            if self._handle_eval(project_id, rest, "GET"):
                return
            # 四期知识域（P4-M2）
            if self._handle_knowledge(project_id, rest, "GET", query):
                return
            # 四期 Workflow 域（P4-M3）
            if self._handle_workflow(project_id, rest, "GET"):
                return
            # 四期发布治理域（P4-M4）
            if self._handle_release(project_id, rest, "GET"):
                return
            # 四期 改进 / 价值 / 自主化（P4-M5~M7）
            if self._handle_p4_misc(project_id, rest, "GET", query):
                return
            # 自主调度只读（M3-1）
            if rest == ["dispatch", "plans"]:
                self._platform(
                    scheduler_service.list_plans, project_id,
                    page=(query.get("page") or ["1"])[0],
                    page_size=(query.get("page_size") or ["20"])[0],
                )
                return
            if len(rest) == 3 and rest[:2] == ["dispatch", "plans"]:
                self._platform(scheduler_service.get_plan, project_id, rest[2])
                return
            if len(rest) == 3 and rest[:2] == ["dispatch", "items"]:
                self._platform(scheduler_service.get_item, project_id, rest[2])
                return
            if rest == ["dispatch", "executor"]:
                self._json(200, _executor_status())
                return
            if rest == ["dispatch", "schedulable"]:
                self._platform(_schedulable_for, project_id)
                return
            if rest == ["tasks"]:
                self._platform(
                    platform_service.list_tasks, project_id,
                    page=(query.get("page") or ["1"])[0],
                    page_size=(query.get("page_size") or ["20"])[0],
                    status=(query.get("status") or [None])[0],
                    priority=(query.get("priority") or [None])[0],
                    assignee=(query.get("assignee") or [None])[0],
                    q=(query.get("q") or [None])[0],
                )
                return
            if len(rest) == 2 and rest[0] == "tasks":
                self._platform(platform_service.get_task, project_id, rest[1])
                return
            if len(rest) == 3 and rest[:2] == ["tasks", rest[1]] and rest[2] == "cross-dependencies":
                self._platform(platform_service.task_cross_dependencies, project_id, rest[1])
                return
            if rest == ["deliveries"]:
                self._platform(
                    platform_service.list_deliveries, project_id,
                    page=(query.get("page") or ["1"])[0],
                    page_size=(query.get("page_size") or ["20"])[0],
                    status=(query.get("status") or [None])[0],
                    kind=(query.get("kind") or [None])[0],
                    task=(query.get("task") or [None])[0],
                    stage=(query.get("stage") or [None])[0],
                )
                return
            if len(rest) == 2 and rest[0] == "deliveries":
                self._platform(platform_service.get_delivery, project_id, rest[1])
                return
            if rest == ["executions"]:
                self._platform(
                    platform_service.list_executions, project_id,
                    page=(query.get("page") or ["1"])[0],
                    page_size=(query.get("page_size") or ["20"])[0],
                    agent=(query.get("agent") or [None])[0],
                    status=(query.get("status") or [None])[0],
                    from_=(query.get("from") or [None])[0],
                    to=(query.get("to") or [None])[0],
                )
                return
            if rest == ["executions", "traceability"]:
                self._platform(platform_service.execution_traceability, project_id)
                return
            if len(rest) == 2 and rest[0] == "executions":
                self._platform(platform_service.get_execution, project_id, rest[1])
                return
            if rest == ["documents"]:
                self._platform(
                    platform_service.list_documents, project_id,
                    page=(query.get("page") or ["1"])[0],
                    page_size=(query.get("page_size") or ["20"])[0],
                    q=(query.get("q") or [None])[0],
                )
                return
            if rest == ["events"]:
                self._platform(
                    platform_service.list_events, project_id,
                    page=(query.get("page") or ["1"])[0],
                    page_size=(query.get("page_size") or ["20"])[0],
                )
                return
            if rest == ["agents", "activity"]:
                self._platform(platform_service.agent_activity, project_id)
                return
            if rest == ["team"]:
                self._platform(_team_status, project_id)
                return
            if rest == ["team", "preflight"]:
                self._platform(_team_preflight, project_id)
                return
            if rest == ["team", "owner"]:
                self._platform(_team_owner, project_id)
                return
            if rest == ["windows"]:
                self._platform(_windows_list, project_id)
                return
            if len(rest) == 3 and rest[0] == "team" and rest[2] == "logs":
                self._platform(
                    _team_logs, project_id, rest[1],
                    lines=int((query.get("lines") or ["200"])[0]),
                )
                return
            if rest == ["sessions"]:
                self._platform(platform_service.list_sessions, project_id)
                return
            if len(rest) == 2 and rest[0] == "sessions":
                self._platform(
                    platform_service.session_trace, project_id, rest[1],
                    limit=int((query.get("limit") or ["160"])[0]),
                )
                return
            if rest == ["timeline"]:
                self._platform(
                    platform_service.execution_timeline, project_id,
                    state=(query.get("state") or [None])[0],
                    limit=int((query.get("limit") or ["80"])[0]),
                )
                return
            self._not_found_route()
        elif path == "/view":
            # 独立查看页：新标签页直接展示内容；路径仍走 safe_project_path 白名单。
            try:
                project = self._project_from_query(query)
            except PlatformError_AccessDenied:
                return
            except platform_service.PlatformError as exc:
                body = file_viewer_html("文件预览", "", None, f"项目不可用：{exc.code}")
                self._send(exc.status, body.encode("utf-8"), "text/html; charset=utf-8")
                return
            raw = (query.get("path") or [None])[0]
            target = safe_project_path(project, raw)
            if target is None:
                body = file_viewer_html(Path(str(raw or "")).name, str(raw or ""), None,
                                        "文件不存在、不是常规文件，或不在当前项目仓库内（已拒绝）。")
                self._send(404, body.encode("utf-8"), "text/html; charset=utf-8")
                return
            suffix = target.suffix.lower()
            try:
                data = target.read_bytes()
            except OSError:
                body = file_viewer_html(target.name, str(raw or target), None, "文件读取失败。")
                self._send(500, body.encode("utf-8"), "text/html; charset=utf-8")
                return
            if suffix in (".html", ".htm", ".svg", ".png", ".jpg", ".jpeg", ".pdf"):
                # 原生可展示类型直接返回，新标签页打开即为文件本身。
                self._send(200, data, MIME.get(suffix, "application/octet-stream"))
                platform_service.record_file_open(project["id"], str(raw or ""))
                return
            text = data.decode("utf-8", "replace")
            body = file_viewer_html(target.name, str(raw or target), text)
            self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
            platform_service.record_file_open(project["id"], str(raw or ""))
        elif path == "/api/file":
            try:
                project = self._project_from_query(query)
            except PlatformError_AccessDenied:
                return
            except platform_service.PlatformError as exc:
                self._error(exc)
                return
            raw = (query.get("path") or [None])[0]
            target = safe_project_path(project, raw)
            if target is None:
                self._fail(404, "not_found", "文件不存在或越界")
                return
            try:
                self._send(200, target.read_bytes(), MIME.get(target.suffix.lower(), "application/octet-stream"))
            except OSError:
                self._fail(500, "unreadable", "文件读取失败")
                return
            platform_service.record_file_open(project["id"], str(raw or ""))
        elif path in ("/", "/index.html"):
            self._send(200, load_page(), "text/html; charset=utf-8")
        else:
            self._not_found_route()


def main() -> int:
    parser = argparse.ArgumentParser(description="多项目 Agent 管理平台（本地只读）")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1", help="监听地址；127.0.0.1 仅本机")
    parser.add_argument("--tailscale", action="store_true",
                        help="绑定到 Tailscale 网卡地址，手机等同一 tailnet 设备可访问")
    parser.add_argument("--once", action="store_true", help="只打印一次 JSON 快照")
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()

    # 平台库：建表 → 默认项目 → 首次回填（只写平台独立 SQLite，不写仓库）。
    try:
        platform_service.bootstrap(repo_root=REPO)
        # 每个登记项目各自回填一次：只回填默认项目会让其它项目的用量永远显示 0
        for item in platform_service.list_projects().get("items", []):
            try:
                platform_service.backfill_project(item["id"])
            except Exception as exc:  # noqa: BLE001 - 单项目回填失败不阻断启动
                print(f"[agents-dashboard] 回填 {item['id']} 失败：{exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - 平台故障不阻断旧看板
        print(f"[agents-dashboard] 平台初始化/回填失败：{exc}", file=sys.stderr)

    if args.once:
        print(json.dumps(build_state(), ensure_ascii=False, indent=2))
        return 0

    # 同时监听本机回环与 tailnet 地址：手机/局域网外设备用 tailnet IP，
    # 本机浏览器用 127.0.0.1，两者都可用，且不暴露到局域网。
    bind_hosts: list[str] = [args.host]
    if args.tailscale:
        detected = _tailscale_ip()
        if detected is None:
            print("[agents-dashboard] 未检测到 Tailscale 地址，仅监听 127.0.0.1", file=sys.stderr)
        elif detected not in bind_hosts:
            bind_hosts.append(detected)

    # listen backlog 默认只有 5：并发稍高（实测 10 并发）就会 Connection reset。
    # 平台是多线程服务，backlog 应该跟线程数一个量级。128 足够本机/局域网使用。
    class _Server(ThreadingHTTPServer):
        daemon_threads = True      # 主进程退出时不等待 worker 线程
        request_queue_size = 128   # 默认 5 太小，并发请求会被内核直接拒绝
        allow_reuse_address = True

    servers = []
    for host in bind_hosts:
        try:
            servers.append(_Server((host, args.port), Handler))
        except OSError as exc:
            print(f"[agents-dashboard] 绑定 {host}:{args.port} 失败：{exc}", file=sys.stderr)

    if not servers:
        print("[agents-dashboard] 没有可用的监听地址，退出", file=sys.stderr)
        return 1

    def _runtime_collector() -> None:
        """常驻采集：每 60 秒探测一次已登记服务。

        没有这个线程时 ``runtime_checks`` 只在手动调用时更新，UI 会显示
        陈旧状态——这正是 P4-R22 记录的缺陷。
        """
        while True:
            try:
                runtime_service.collect_once()
            except Exception as exc:  # noqa: BLE001 - 采集失败不能让线程退出
                print(f"[agents-dashboard] 运行时采集失败：{exc}", file=sys.stderr)
            time.sleep(60)

    threading.Thread(target=_runtime_collector, daemon=True).start()

    #: Event Outbox 投递线程（R2 / S-7）。
    #:
    #: 审计发现 ``dispatch_pending`` 只有定义和测试调用，生产代码从没调用过——
    #: outbox 只写不投，事件永远积压。这里补一个单线程消费者：
    #: 单飞（不并发，避免同一事件被两个线程同时标记）、失败不退出、可观测。
    #: 间隔 5 秒：事件都是低频治理事件，不需要更低延迟。
    def _event_dispatcher() -> None:
        while True:
            try:
                conn = platform_store.connect()
                try:
                    stats = p4_events.dispatch_pending(conn, consumer="platform")
                finally:
                    conn.close()
                # 只在有变化时打印，避免刷屏
                if stats["dispatched"] or stats["failed"]:
                    print(f"[agents-dashboard] 事件投递：{stats}", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 - 投递失败不能让线程退出
                print(f"[agents-dashboard] 事件投递异常：{exc}", file=sys.stderr)
            time.sleep(5)

    threading.Thread(target=_event_dispatcher, daemon=True).start()

    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = f"http://{bind_hosts[0]}:{args.port}"
    print("[agents-dashboard] 已监听：" + "、".join(f"http://{h}:{args.port}" for h in bind_hosts))
    print("                  (Ctrl-C 退出；只读，不改仓库)")
    if args.open:
        webbrowser.open(url)
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\n[agents-dashboard] bye")
    return 0


HTML_PATH = Path(__file__).resolve().parent / "agents_dashboard.html"


def load_page() -> bytes:
    """每次请求重新读 HTML（按 mtime 缓存），改样式不用重启服务。"""
    try:
        mtime = HTML_PATH.stat().st_mtime
    except OSError:
        return "<h1>agents_dashboard.html 缺失</h1>".encode()
    if _page_cache["mtime"] != mtime:
        _page_cache["mtime"] = mtime
        _page_cache["body"] = HTML_PATH.read_bytes()
    return _page_cache["body"]


_page_cache: dict[str, Any] = {"mtime": None, "body": b""}



if __name__ == "__main__":
    sys.exit(main())
