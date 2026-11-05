"""独立 SQLite 存储层：连接、建表、schema_version、通用时间/ID 工具。

默认库：``~/Multi-Project-Agent-Workspace/data/platform.db``（见 ``config.data_dir()``）
覆盖：环境变量 ``DDP_AGENT_PLATFORM_DB``（测试使用临时目录）。

历史：2026-11-04 前默认在 ``~/.claude/agent-platform/``；迁移时保留该路径作为
只读回退（老库存在且新库不存在时自动沿用），避免一次性搬库丢数据。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


LEGACY_DB_PATH = Path.home() / ".claude" / "agent-platform" / "platform.db"


def default_db_path() -> Path:
    """平台库路径：环境变量 → 工作区 data/ → （回退）旧 ~/.claude 路径。"""
    override = os.environ.get("DDP_AGENT_PLATFORM_DB")
    if override:
        return Path(override).expanduser()
    from . import config

    target = config.data_dir() / "platform.db"
    if not target.exists() and LEGACY_DB_PATH.exists():
        return LEGACY_DB_PATH
    return target


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")  # noqa: UP017


def iso_utc(value: str | None) -> str | None:
    """把任意 ISO8601（含 Z / 本地偏移）规整成 UTC ISO，便于字符串比较。"""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))  # noqa: FURB162
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)  # noqa: UP017
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")  # noqa: UP017


def from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds")  # noqa: UP017


def stable_id(*parts: Any) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path).expanduser() if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：显式 BEGIN/COMMIT 控制事务，便于“项目 + 角色”原子创建。
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    """建表 + 迁移到当前 schema 版本；可重复执行。

    8099 重启即自动升级：迁移逻辑统一在 ``migrate.apply``，升级时会先备份
    并在 stderr 打印版本与备份路径。返回连接（自建连接时由调用方关闭）。
    """
    own = conn is None
    conn = conn or connect()
    try:
        # 延迟导入避免 store <-> migrate 循环依赖。
        from . import migrate

        migrate.apply(conn, backup=True, verbose=True)
        return conn
    finally:
        if own:
            conn.close()


def session():
    """上下文管理：一次请求一个连接。"""
    return _Session()


class _Session:
    def __init__(self) -> None:
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.conn = connect()
        init_db(self.conn)
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.conn is not None:
            if exc_type is not None:
                try:
                    self.conn.rollback()
                except sqlite3.Error:
                    pass
            self.conn.close()
        return False


def loads_json(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
