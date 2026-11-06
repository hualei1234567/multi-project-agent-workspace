"""schema 版本链迁移（三期 M3-0 T3-001）。

锁定三条行为：
1. 新库从 0 一步升到最新，且逐级执行了每个 MigrationStep；
2. 当前版本 ≥ 目标时**绝不倒写版本号**（旧实现会，把 v3 库打回 v2）；
3. 降级只删目标版本之后引入的表，且版本号正确回落。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import migrate, store

V2_ONLY = {"task_dependencies", "executions", "audit_logs", "idempotency_keys"}
V3_ONLY = set(migrate.V3_TABLES)
V4_ONLY = set(migrate.V4_TABLES)
V5_ONLY = set(migrate.V5_TABLES)
V6_ONLY = set(migrate.V6_TABLES)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _version(conn: sqlite3.Connection) -> int:
    return migrate.current_version(conn)


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(db))
    conn = store.connect(db)
    yield conn, db
    conn.close()


def test_fresh_db_reaches_latest_version(fresh_db):
    conn, _ = fresh_db
    result = migrate.apply(conn, no_backup=True)
    assert result["version"] == migrate.TARGET_VERSION
    assert result["changed"] is True
    # 逐级执行了全部步骤
    assert result["steps"] == [step.version for step in migrate.MIGRATIONS]
    tables = _tables(conn)
    assert V2_ONLY <= tables
    assert V3_ONLY <= tables
    assert _version(conn) == migrate.TARGET_VERSION


def test_second_apply_is_idempotent(fresh_db):
    conn, db = fresh_db
    migrate.apply(conn, no_backup=True)
    backups_before = len(list(db.parent.glob("platform.db.bak-*")))
    again = migrate.apply(conn, no_backup=True)
    assert again["changed"] is False
    assert again["steps"] == []
    assert len(list(db.parent.glob("platform.db.bak-*"))) == backups_before


def test_does_not_write_version_backwards(fresh_db):
    """关键回归：旧实现会无条件写 TARGET_VERSION，把更高版本的库打回低版本。"""
    conn, _ = fresh_db
    migrate.apply(conn, no_backup=True)
    # 手工模拟「代码比库旧」：库版本高于代码里的目标
    conn.execute("DELETE FROM schema_version")
    conn.execute(
        "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
        (migrate.TARGET_VERSION + 1, store.now_iso()),
    )
    with pytest.raises(RuntimeError, match="高于目标"):
        migrate.apply(conn, no_backup=True)
    # 版本号保持原样，没有被改写
    assert _version(conn) == migrate.TARGET_VERSION + 1


def test_targeted_upgrade_stops_at_v2(fresh_db):
    conn, _ = fresh_db
    result = migrate.apply(conn, no_backup=True, target_version=2)
    assert result["version"] == 2
    assert result["steps"] == [2]
    tables = _tables(conn)
    assert V2_ONLY <= tables
    assert not (V3_ONLY & tables)


def test_downgrade_removes_only_later_tables(fresh_db):
    conn, _ = fresh_db
    migrate.apply(conn, no_backup=True)
    assert V3_ONLY <= _tables(conn)
    result = migrate.downgrade(conn, no_backup=True, target_version=2)
    assert result["version"] == 2
    # 降级到 v2 会同时丢掉 v3 / v4 / v5 / v6 引入的表
    assert set(result["dropped"]) == V3_ONLY | V4_ONLY | V5_ONLY | V6_ONLY
    tables = _tables(conn)
    assert not (V3_ONLY & tables)
    assert V2_ONLY <= tables
    assert _version(conn) == 2


def test_downgrade_full_returns_to_v1(fresh_db):
    conn, _ = fresh_db
    migrate.apply(conn, no_backup=True)
    migrate.downgrade(conn, no_backup=True, target_version=1)
    tables = _tables(conn)
    assert not (V2_ONLY & tables)
    assert not (V3_ONLY & tables)
    assert _version(conn) == 1


def test_downgrade_noop_when_already_lower(fresh_db):
    conn, _ = fresh_db
    migrate.apply(conn, no_backup=True, target_version=2)
    result = migrate.downgrade(conn, no_backup=True, target_version=2)
    assert result["dropped"] == []
    assert _version(conn) == 2


def test_backup_is_readable_sqlite(fresh_db):
    """备份用 SQLite 在线备份 API（WAL 安全），必须能被独立打开并读到表。

    只有已存在的库（version >= 1）才备份：全新空库没有可备份的内容。
    """
    conn, _db = fresh_db
    # 先升到 v2 造出一个"已有数据"的库，再升 v3 验证备份
    migrate.apply(conn, no_backup=True, target_version=2)
    conn.execute(
        "INSERT INTO agent_projects (id, code, name, status, repo_root, created_at, updated_at)"
        " VALUES ('bk','bk','Backup','active','/tmp/x','t','t')"
    )
    tables_before = _tables(conn)
    result = migrate.apply(conn, no_backup=False)

    assert result["backup"], "已有库升级应生成备份"
    backup_path = Path(result["backup"])
    assert backup_path.exists()
    check = sqlite3.connect(str(backup_path))
    try:
        recovered = {
            row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables_before <= recovered
        # 备份里带着升级前的数据，且不含升级后的 v3 表
        assert check.execute("SELECT COUNT(*) FROM agent_projects").fetchone()[0] == 1
        assert not (V3_ONLY & recovered)
    finally:
        check.close()


def test_no_backup_for_brand_new_db(fresh_db):
    """全新空库不产生备份：没有可回滚的内容，避免噪音文件。"""
    conn, db = fresh_db
    result = migrate.apply(conn, no_backup=False)
    assert result["backup"] is None
    assert not list(db.parent.glob("platform.db.bak-*"))


def test_unknown_target_rejected(fresh_db):
    conn, _ = fresh_db
    with pytest.raises(RuntimeError, match="未在 MIGRATIONS 中登记"):
        migrate.apply(conn, no_backup=True, target_version=99)


def test_cross_project_column_added(fresh_db):
    conn, _ = fresh_db
    migrate.apply(conn, no_backup=True)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(task_dependencies)")}
    assert "cross_project" in cols


def test_repair_reruns_steps_when_version_current(fresh_db):
    """版本已最新但需要重跑数据修正时，--repair 必须执行步骤。"""
    conn, _ = fresh_db
    migrate.apply(conn, no_backup=True)
    assert migrate.apply(conn, no_backup=True)["changed"] is False
    repaired = migrate.apply(conn, no_backup=True, repair=True)
    assert repaired["changed"] is True
    assert repaired["steps"] == [item.version for item in migrate.MIGRATIONS]


def test_v3_repair_removes_runtime_action_runs_fk(fresh_db):
    """runtime_action_runs 早期定义带外键；修复后应可写入动作目录外的 action_id。"""
    conn, _ = fresh_db
    # 先造出带外键的旧表形态，模拟已升级过的库
    migrate.apply(conn, no_backup=True)
    conn.execute("DROP TABLE runtime_action_runs")
    conn.execute(
        "CREATE TABLE runtime_action_runs ("
        " id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES runtime_actions(id),"
        " project_id TEXT, status TEXT NOT NULL DEFAULT 'pending', preflight_output TEXT,"
        " preflight_token TEXT, dry_run INTEGER NOT NULL DEFAULT 1, output TEXT,"
        " exit_code INTEGER, started_at TEXT, finished_at TEXT, created_at TEXT NOT NULL)"
    )
    migrate.apply(conn, no_backup=True, repair=True)
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='runtime_action_runs'"
    ).fetchone()[0]
    assert "REFERENCES runtime_actions" not in sql
    # 现在可以写代码目录里的动作名
    conn.execute(
        "INSERT INTO runtime_action_runs (id, action_id, status, created_at)"
        " VALUES ('r1', 'docker_ps_material', 'succeeded', 't')"
    )
