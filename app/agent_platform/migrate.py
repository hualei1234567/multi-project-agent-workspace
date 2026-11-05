"""平台 SQLite schema v1 -> v2 迁移（独立于 DDP 业务库）。

用法::

    python3 scripts/agent_platform/migrate.py --dry-run
    python3 scripts/agent_platform/migrate.py
    python3 scripts/agent_platform/migrate.py --downgrade

设计（docs/112 §4）：
* 只做 expand：``schema.sql`` 幂等建表 + ``PRAGMA table_info`` 判断后
  ``ALTER TABLE ADD COLUMN``；不改历史列语义。
* 迁移前把当前 DB 复制为 ``platform.db.bak-<UTC>``，最多保留 3 份。
* 迁移前后各表 ``COUNT(*)`` 对账，已有表行数不一致则回滚并报错。
* ``schema_version`` 只保留一行：迁移写 2；``--downgrade`` 删 v2 新表并回落 1，
  新增列保留（SQLite 删列会丢数据），输出提示。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # 直接 `python3 scripts/agent_platform/migrate.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent_platform import store
else:
    from . import store

TARGET_VERSION = 2
BACKUP_KEEP = 3

V2_TABLES = (
    "task_dependencies",
    "executions",
    "project_documents",
    "project_favorites",
    "project_recent_visits",
    "audit_logs",
    "idempotency_keys",
)

# (table, [(column, ddl_type)])，顺序按 docs/112 §3。
COLUMN_UPGRADES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("agent_projects", (
        ("owner", "TEXT"),
        ("archived_at", "TEXT"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
    )),
    ("project_agents", (
        ("role_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("model_id", "TEXT"),
        ("last_seen_at", "TEXT"),
    )),
    ("tasks", (
        ("description", "TEXT"),
        ("assignee_agent_id", "TEXT"),
        ("created_by", "TEXT"),
        ("acceptance_criteria_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("blocked_reason", "TEXT"),
        ("branch_name", "TEXT"),
        ("started_at", "TEXT"),
        ("completed_at", "TEXT"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
    )),
    ("deliveries", (
        ("summary", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'submitted'"),
        ("reviewer", "TEXT"),
        ("reviewed_at", "TEXT"),
        ("rejection_reason", "TEXT"),
        ("updated_at", "TEXT"),
        # 附加列（docs/112 未列）：区分仓库镜像行与平台内交付，列表据此标记 readonly。
        ("source", "TEXT NOT NULL DEFAULT 'platform'"),
    )),
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _log(message: str) -> None:
    print(f"[agent-platform] {message}", file=sys.stderr)


def schema_statements() -> list[str]:
    """读取 schema.sql，去掉 ``--`` 注释行后按分号拆分。"""
    text = store.SCHEMA_PATH.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.strip().startswith("--")]
    statements = []
    for chunk in "\n".join(lines).split(";"):
        statement = chunk.strip()
        if statement:
            statements.append(statement)
    return statements


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {row[1] for row in rows}


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0] or 0) if row else 0


def _row_counts(conn: sqlite3.Connection, tables: set[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in sorted(tables):
        try:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        except sqlite3.DatabaseError:
            continue
        counts[table] = int(row[0] or 0) if row else 0
    return counts


def plan_changes(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    missing_tables = [name for name in V2_TABLES if name not in tables]
    missing_columns: list[tuple[str, str, str]] = []
    for table, columns in COLUMN_UPGRADES:
        if table not in tables:
            continue
        present = _columns(conn, table)
        for column, ddl in columns:
            if column not in present:
                missing_columns.append((table, column, ddl))
    schema_tables = set()
    for statement in schema_statements():
        upper = statement.upper()
        marker = "CREATE TABLE IF NOT EXISTS "
        if upper.startswith(marker):
            schema_tables.add(statement[len(marker):].split("(")[0].strip())
    missing_schema_tables = sorted(schema_tables - tables)
    return {
        "tables": tables,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "missing_schema_tables": missing_schema_tables,
    }


def _db_path(conn: sqlite3.Connection) -> Path | None:
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.DatabaseError:
        return None
    for row in rows:
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


def backup_database(path: Path) -> Path | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")  # noqa: UP017
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    backups = sorted(path.parent.glob(f"{path.name}.bak-*"))
    for old in backups[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return target


# ---------------------------------------------------------------------------
# 升级 / 降级
# ---------------------------------------------------------------------------
def _backfill_mirror_source(conn: sqlite3.Connection) -> int:
    """把 v1 采集来的交付行标记为 mirror（id 由 repo 相对路径稳定生成）。"""
    columns = _columns(conn, "deliveries")
    if "source" not in columns:
        return 0
    changed = 0
    for row in conn.execute("SELECT id, project_id, path FROM deliveries").fetchall():
        if not row["path"]:
            continue
        if row["id"] == store.stable_id("delivery", row["project_id"], row["path"]):
            changed += max(
                conn.execute(
                    "UPDATE deliveries SET source = 'mirror' WHERE id = ?", (row["id"],)
                ).rowcount,
                0,
            )
    return changed


def apply(
    conn: sqlite3.Connection,
    *,
    backup: bool = True,
    no_backup: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    """幂等升级到 v2。返回 {version, backup, changed}；失败整体回滚。"""
    before_version = current_version(conn)
    plan = plan_changes(conn)
    needs_work = bool(
        plan["missing_schema_tables"]
        or plan["missing_tables"]
        or plan["missing_columns"]
        or before_version < TARGET_VERSION
    )
    if not needs_work:
        if verbose:
            _log(f"schema v{before_version} 已是最新，无需迁移")
        return {"version": before_version, "backup": None, "changed": False}

    path = _db_path(conn)
    backup_path: Path | None = None
    if backup and not no_backup and path is not None and before_version >= 1:
        backup_path = backup_database(path)

    tables_before = plan["tables"] or _tables(conn)
    counts_before = _row_counts(conn, tables_before)
    statements = schema_statements()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in statements:
            conn.execute(statement)
        for table, column, ddl in plan["missing_columns"]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        _backfill_mirror_source(conn)
        conn.execute("DELETE FROM schema_version")
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (TARGET_VERSION, store.now_iso()),
        )
        counts_after = _row_counts(conn, tables_before)
        mismatched = {
            table: (counts_before.get(table, 0), counts_after.get(table, 0))
            for table in tables_before
            if counts_before.get(table, 0) != counts_after.get(table, 0)
        }
        if mismatched:
            raise RuntimeError(f"迁移对账失败，已有表行数变化：{mismatched}")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        raise

    suffix = f"，备份：{backup_path}" if backup_path else ""
    _log(f"schema v{before_version} -> v{TARGET_VERSION} 完成{suffix}")
    return {"version": TARGET_VERSION, "backup": str(backup_path) if backup_path else None,
            "changed": True, "columns_added": plan["missing_columns"]}


def downgrade(
    conn: sqlite3.Connection,
    *,
    backup: bool = True,
    no_backup: bool = False,
) -> dict[str, Any]:
    """删除 v2 新表并回落 schema_version=1；新增列保留（输出提示）。"""
    path = _db_path(conn)
    backup_path: Path | None = None
    if backup and not no_backup and path is not None and path.exists() and path.stat().st_size:
        backup_path = backup_database(path)
    dropped: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in V2_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if exists:
                conn.execute(f"DROP TABLE {table}")
                dropped.append(table)
        conn.execute("DELETE FROM schema_version")
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (1, ?)", (store.now_iso(),)
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        raise
    _log(
        "已回落到 schema v1：删除表 "
        + (", ".join(dropped) if dropped else "（无）")
        + "；新增列保留（SQLite 删列会丢数据）"
        + (f"；备份：{backup_path}" if backup_path else "")
    )
    return {"version": 1, "dropped": dropped,
            "backup": str(backup_path) if backup_path else None}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _readonly_connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.DatabaseError:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多项目 Agent 平台 SQLite schema 迁移（v1 <-> v2）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    parser.add_argument("--downgrade", action="store_true", help="回落到 v1（删 v2 新表，新增列保留）")
    parser.add_argument("--no-backup", action="store_true", help="跳过备份（仅测试用）")
    parser.add_argument("--db", help="覆盖数据库路径（默认 DDP_AGENT_PLATFORM_DB）")
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser() if args.db else store.default_db_path()
    print(f"数据库：{db_path}")

    if args.dry_run:
        conn = _readonly_connect(db_path)
        if conn is None:
            print("当前版本：0（库不存在或为空）")
            print(f"目标版本：{TARGET_VERSION}")
            print("将执行：完整 schema.sql（v2 全部表/索引）+ schema_version=2")
            print("行数：（无现有表）")
            return 0
        try:
            plan = plan_changes(conn)
            version = current_version(conn)
            counts = _row_counts(conn, plan["tables"])
            print(f"当前版本：{version}")
            print(f"目标版本：{TARGET_VERSION}")
            print(f"缺失表：{plan['missing_schema_tables'] or '（无）'}")
            print(
                "缺失列："
                + (
                    ", ".join(f"{t}.{c}" for t, c, _ in plan["missing_columns"])
                    if plan["missing_columns"]
                    else "（无）"
                )
            )
            print("行数：")
            for table in sorted(counts):
                print(f"  {table}: {counts[table]}")
            print("将执行：schema.sql 幂等建表 + 上述 ALTER + schema_version=2")
        finally:
            conn.close()
        return 0

    conn = store.connect(db_path)
    try:
        if args.downgrade:
            result = downgrade(conn, no_backup=args.no_backup)
            print(f"结果：{result}")
            return 0
        result = apply(conn, no_backup=args.no_backup, verbose=True)
        print(f"结果：{result}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
