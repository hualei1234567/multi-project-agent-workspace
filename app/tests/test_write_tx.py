"""写事务原子性（三期 M3-0 T3-002）。

背景：平台连接是 ``isolation_level=None``（autocommit），多语句写必须显式开事务，
否则中途失败会留下「状态改了但没留痕」这类半成品。旧代码里有几处误用 ``with conn:``
（在 autocommit 下不会开事务）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import service, store


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "platform.db"
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("DDP_AGENT_PLATFORM_DB", str(db))
    monkeypatch.setenv("DDP_AGENT_PLATFORM_REPO", str(repo))
    monkeypatch.setenv("DDP_AGENT_PLATFORM_PROJECTS_ROOT", str(tmp_path / "projects"))
    config_file = tmp_path / "projects.json"
    config_file.write_text('{"projects": []}', encoding="utf-8")
    monkeypatch.setenv("DDP_AGENT_PLATFORM_CONFIG", str(config_file))
    service.bootstrap(repo_root=repo)
    return SimpleNamespace(tmp=tmp_path, db=db, repo=repo)


def test_write_tx_commits_on_success(env):
    conn = store.connect(env.db)
    try:
        store.init_db(conn)
        with store.write_tx(conn):
            conn.execute("INSERT INTO audit_logs (id, actor, action, resource_type, result,"
                         " detail_json, created_at) VALUES ('a1','t','test','x','ok','{}','t')")
        assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE id='a1'").fetchone()[0] == 1
    finally:
        conn.close()


def test_write_tx_rolls_back_on_error(env):
    conn = store.connect(env.db)
    try:
        store.init_db(conn)
        with pytest.raises(RuntimeError), store.write_tx(conn):
            conn.execute("INSERT INTO audit_logs (id, actor, action, resource_type, result,"
                         " detail_json, created_at) VALUES ('a2','t','test','x','ok','{}','t')")
            raise RuntimeError("boom")
        # 半成品不能留下
        assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE id='a2'").fetchone()[0] == 0
    finally:
        conn.close()


def test_transition_is_atomic(env):
    """状态迁移失败时，状态与审计都不能落库。"""
    task = service.create_task("ddp", {"title": "事务用例", "task_key": "TX-001"})
    before_status = task["task"]["status"]
    before_audit = _audit_count(env.db, "task.transition")

    # 非法迁移直接抛错，不应留下任何写入
    with pytest.raises(service.PlatformError):
        service.transition_task(
            "ddp", task["task"]["id"],
            {"to_status": "done", "expected_version": task["task"]["version"]},
        )

    current = service.get_task("ddp", task["task"]["id"])["task"]
    assert current["status"] == before_status
    assert current["version"] == task["task"]["version"]
    assert _audit_count(env.db, "task.transition") == before_audit


def _audit_count(db: Path, action: str) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action = ?", (action,)).fetchone()[0]
    finally:
        conn.close()
