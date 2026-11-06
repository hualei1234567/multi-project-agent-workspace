#!/usr/bin/env python3
"""P4-OPS-004 备份恢复演练：真备份、真校验、真恢复，不留"应该没问题"。

流程：
1. 用 ``migrate.backup_database`` 对当前库做热备（WAL 安全）；
2. 校验备份文件：``PRAGMA integrity_check``、schema_version、逐表行数对比；
3. 把备份**恢复**到一个全新文件，再校验一次（模拟真实灾难恢复）；
4. 验证全文索引可用（knowledge_fts 查询）；
5. 输出 PASS/FAIL 与 JSON 证据。

不改动源库，不删除任何备份文件。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from agent_platform import migrate, store  # noqa: E402


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for name in names}


def _revocation_replay_drill() -> dict:
    """用一个独立临时库演练"撤销 → 备份 → 恢复 → 不再命中"。"""
    import tempfile as _tempfile

    sys.path.insert(0, str(ROOT / "app"))
    from agent_platform import store as _store
    from agent_platform.p4 import knowledge as _knowledge

    workdir = Path(_tempfile.mkdtemp(prefix="p4-revoke-"))
    db = workdir / "platform.db"
    conn = _store.connect(db)
    _store.init_db(conn)
    conn.execute(
        "INSERT INTO agent_projects (id, code, name, repo_root, created_at, updated_at)"
        " VALUES ('rp', 'rp', '演练项目', '/tmp/r', '2026-11-06T00:00:00+00:00',"
        " '2026-11-06T00:00:00+00:00')")
    conn.commit()

    source = _knowledge.create_source(conn, "rp", {"title": "演练来源"})
    item = _knowledge.create_item(conn, "rp", {
        "title": "演练条目", "source_id": source["id"],
        "content": "这段内容包含独特标记 zeta-unique 用于验证撤销",
    })
    version_id = item["versions"][0]["id"]
    _knowledge.publish_version(conn, version_id, reviewed_by="qa")
    before = _knowledge.search(conn, "rp", "zeta-unique")["total"]
    _knowledge.revoke_version(conn, version_id, reason="演练撤销", revoked_by="qa")
    after_revoke = _knowledge.search(conn, "rp", "zeta-unique")["total"]

    backup_path = migrate.backup_database(db)
    conn.close()

    restored_path = workdir / "restored.db"
    bak = sqlite3.connect(backup_path)
    restored = sqlite3.connect(restored_path)
    bak.backup(restored)
    bak.close()

    residual = restored.execute(
        "SELECT COUNT(*) FROM knowledge_chunks c"
        " JOIN knowledge_versions v ON v.id = c.version_id WHERE v.status = 'revoked'"
    ).fetchone()[0]
    fts_residual = restored.execute(
        "SELECT COUNT(*) FROM knowledge_fts f"
        " JOIN knowledge_versions v ON v.id = f.version_id WHERE v.status = 'revoked'"
    ).fetchone()[0]

    # 恢复库上再查一次检索（用同一个库连接检索函数）
    restored.row_factory = sqlite3.Row
    restored.close()
    conn2 = _store.connect(restored_path)
    try:
        after_restore = _knowledge.search(conn2, "rp", "zeta-unique")["total"]
    finally:
        conn2.close()

    ok = (before == 1 and after_revoke == 0 and residual == 0
          and fts_residual == 0 and after_restore == 0)
    return {
        "pass": ok,
        "detail": (f"撤销前命中 {before}，撤销后 {after_revoke}，"
                   f"恢复后 {after_restore}；残留块 {residual}，残留索引 {fts_residual}"),
    }


def main() -> int:
    db_path = store.default_db_path()
    results: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append({"case": name, "pass": ok, "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} —— {detail}")

    if not db_path.exists():
        print(f"源库不存在：{db_path}")
        return 1

    source = sqlite3.connect(db_path)
    source.row_factory = sqlite3.Row
    src_counts = table_counts(source)
    src_version = source.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    print(f"源库：{db_path}（{len(src_counts)} 表，schema v{src_version}）")

    backup_path = migrate.backup_database(db_path)
    check("热备生成", backup_path is not None and Path(backup_path).exists(),
          f"{backup_path}")

    backup = sqlite3.connect(backup_path)
    backup.row_factory = sqlite3.Row
    integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
    check("备份完整性", integrity == "ok", f"integrity_check={integrity}")

    bak_version = backup.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    check("备份版本一致", bak_version == src_version, f"v{bak_version} vs v{src_version}")

    bak_counts = table_counts(backup)
    mismatched = {
        name: (src_counts.get(name, 0), bak_counts.get(name, 0))
        for name in set(src_counts) | set(bak_counts)
        if src_counts.get(name, 0) != bak_counts.get(name, 0)
    }
    check("逐表行数一致", not mismatched, f"不一致表：{mismatched or '无'}")

    # 恢复演练：把备份还原成一个全新库
    restore_path = Path(tempfile.mkdtemp(prefix="p4-restore-")) / "restored.db"
    restored = sqlite3.connect(restore_path)
    backup.backup(restored)
    restored.row_factory = sqlite3.Row
    r_integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
    check("恢复库完整性", r_integrity == "ok", f"integrity_check={r_integrity}")

    r_counts = table_counts(restored)
    r_mismatch = {
        name: (src_counts.get(name, 0), r_counts.get(name, 0))
        for name in set(src_counts) | set(r_counts)
        if src_counts.get(name, 0) != r_counts.get(name, 0)
    }
    check("恢复后行数一致", not r_mismatch, f"不一致表：{r_mismatch or '无'}")

    # 业务可读性：关键表能查、全文索引能查
    try:
        projects = restored.execute("SELECT COUNT(*) FROM agent_projects").fetchone()[0]
        check("恢复后业务表可读", True, f"agent_projects={projects}")
    except sqlite3.DatabaseError as exc:
        check("恢复后业务表可读", False, str(exc))

    try:
        restored.execute(
            "SELECT COUNT(*) FROM knowledge_fts WHERE knowledge_fts MATCH ?", ("test",)
        ).fetchone()
        check("恢复后全文索引可用", True, "knowledge_fts 可查询")
    except sqlite3.DatabaseError as exc:
        check("恢复后全文索引可用", False, str(exc))

    # 撤销重放检查：备份里若有已撤销知识版本，恢复后索引里不应残留
    try:
        revoked = restored.execute(
            "SELECT COUNT(*) FROM knowledge_versions WHERE status = 'revoked'"
        ).fetchone()[0]
        leftover = restored.execute(
            "SELECT COUNT(*) FROM knowledge_chunks c"
            " JOIN knowledge_versions v ON v.id = c.version_id"
            " WHERE v.status = 'revoked'"
        ).fetchone()[0]
        check("撤销状态与索引一致", leftover == 0,
              f"已撤销版本 {revoked} 个，残留索引块 {leftover} 个")
    except sqlite3.DatabaseError as exc:
        check("撤销状态与索引一致", False, str(exc))

    # 撤销重放的**实战**验证：在临时库里造一条已撤销知识，
    # 备份 → 恢复 → 确认撤销状态保留、索引无残留、检索 0 命中。
    revoke_case = _revocation_replay_drill()
    check("撤销重放（实战）", revoke_case["pass"], revoke_case["detail"])

    for conn in (source, backup, restored):
        conn.close()

    failed = [r for r in results if not r["pass"]]
    print(f"\n合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    print(f"备份文件：{backup_path}")
    print(f"恢复演练库：{restore_path}")

    out = ROOT / "docs" / "p4" / "reports" / "backup-drill.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "source_db": str(db_path),
        "backup": str(backup_path),
        "restored": str(restore_path),
        "schema_version": src_version,
        "cases": results,
        "failed": len(failed),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
