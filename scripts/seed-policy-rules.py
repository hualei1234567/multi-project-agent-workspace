#!/usr/bin/env python3
"""初始化审批规则。

背景：``p4/policy.decide()`` 的审批分支依赖 ``policy_rules.requires_approval``，
但生产库里这张表一直是空的（只有测试里手动插）。结果：
Policy 判定始终 ``requires_approval=False``，点一下就能起 claude 进程。

本脚本补上「高风险动作必须审批」的规则。**幂等**：按固定 id 插入，重复执行不重复建。

用法：
    python3 scripts/seed-policy-rules.py --dry-run   # 只看会插什么
    python3 scripts/seed-policy-rules.py --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from agent_platform import store  # noqa: E402

#: (规则 id, 资源类型, 动作, 最高风险, 是否需要审批, 说明)
RULES = [
    ("rule-dispatch-run-approval", "dispatch_item", "run", "R3", 1,
     "派发执行会起真实 claude 进程，必须人工审批后执行"),
    ("rule-runtime-run-approval", "runtime_action", "run", "R3", 1,
     "运行时一键操作会影响外部服务，必须审批"),
    ("rule-release-publish-approval", "agent_release", "publish", "R4", 1,
     "发布到生产必须审批"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写入")
    ap.add_argument("--dry-run", action="store_true", help="只打印")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.error("必须指定 --dry-run 或 --apply")

    db = Path(args.db).expanduser() if args.db else store.default_db_path()
    print(f"数据库：{db}")
    if not db.exists():
        print("数据库不存在，先启动一次平台生成 schema", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    existing = {r["id"] for r in conn.execute("SELECT id FROM policy_rules")}
    to_add = [r for r in RULES if r[0] not in existing]

    print(f"\n现有规则 {len(existing)} 条；本次将新增 {len(to_add)} 条")
    for rid, rtype, action, risk, need_appr, note in to_add:
        print(f"  + {rid}")
        print(f"      资源={rtype} 动作={action} 最高风险={risk} 需审批={bool(need_appr)}")
        print(f"      说明：{note}")

    if not args.apply:
        print("\n[dry-run] 未写入。确认后加 --apply")
        return 0

    if not to_add:
        print("\n无需变更（幂等）")
        return 0

    now = store.now_iso()

    with conn:
        for rid, rtype, action, risk, need_appr, note in to_add:
            conn.execute(
                "INSERT INTO policy_rules (id, scope, project_id, principal_id,"
                " principal_type, resource_type, action, environment, max_risk,"
                " effect, requires_approval, enabled, note, created_at)"
                " VALUES (?, 'global', NULL, NULL, NULL, ?, ?, '*', ?, 'allow', ?, 1, ?, ?)",
                (rid, rtype, action, risk, need_appr, note, now),
            )
    total = conn.execute("SELECT COUNT(*) AS c FROM policy_rules").fetchone()["c"]
    print(f"\n✅ 已写入，当前共 {total} 条规则")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
