"""平台级配置：工作区定位、默认项目、数据目录。

设计（2026-11-04 从 DDP 仓库独立出来）：

    ~/Multi-Project-Agent-Workspace/     ← 平台本体
    ├── app/                             ← 代码（本包所在）
    ├── config/projects.json             ← 纳管项目清单（可热改）
    ├── data/platform.db                 ← 平台库
    └── scripts/                         ← 启动 / 测试脚本

    ~/agent-team/<code>-team/            ← 每个项目的 team 目录
    ~/vscodeworkspace/<项目>/             ← 项目代码

所有路径都可由环境变量覆盖，便于测试与迁移：
  DDP_AGENT_PLATFORM_DB              平台库路径
  DDP_AGENT_PLATFORM_DEFAULT_REPO    默认项目仓库根（缺省取 config/projects.json 首个）
  DDP_AGENT_PLATFORM_WORKSPACE       工作区根
"""
from __future__ import annotations

import json
import os
from pathlib import Path

#: app/agent_platform/config.py → 上两级 = 工作区根
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


def workspace_root() -> Path:
    override = os.environ.get("DDP_AGENT_PLATFORM_WORKSPACE")
    return Path(override).expanduser().resolve() if override else WORKSPACE_ROOT


def data_dir() -> Path:
    return workspace_root() / "data"


def config_path() -> Path:
    return workspace_root() / "config" / "projects.json"


def load_projects_config() -> dict:
    """项目清单；不存在或损坏时返回空结构（不抛异常，平台仍可启动）。"""
    path = config_path()
    if not path.is_file():
        return {"projects": [], "default_project_id": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"projects": [], "default_project_id": None}
    return data if isinstance(data, dict) else {"projects": [], "default_project_id": None}


def default_project() -> dict:
    """默认项目配置（含 repo_root）；没有配置时给一个安全的空值。"""
    cfg = load_projects_config()
    projects = [p for p in (cfg.get("projects") or []) if isinstance(p, dict)]
    wanted = cfg.get("default_project_id")
    for item in projects:
        if wanted and item.get("id") == wanted:
            return item
    return projects[0] if projects else {}


def default_repo_root() -> Path:
    """默认项目仓库根。

    优先级：环境变量 → config/projects.json 中默认项目的 repo_root → 当前工作目录。
    换机器/换目录后只改配置，不用改代码（旧实现靠 ``Path(__file__).parents[2]``）。
    """
    override = os.environ.get("DDP_AGENT_PLATFORM_DEFAULT_REPO")
    if override:
        return Path(override).expanduser().resolve()
    repo = str(default_project().get("repo_root") or "").strip()
    if repo:
        candidate = Path(repo).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    return Path.cwd().resolve()


def team_dir(code: str) -> Path:
    """某项目的 team 目录：``~/agent-team/<code>-team``。"""
    override = os.environ.get("DDP_AGENT_PLATFORM_TEAM_ROOT")
    root = Path(override).expanduser() if override else Path.home() / "agent-team"
    return root / f"{code}-team"
