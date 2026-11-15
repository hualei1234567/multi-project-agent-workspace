"""平台 SQLite schema 版本链迁移（独立于业务库）。

用法::

    python3 -m agent_platform.migrate --dry-run
    python3 -m agent_platform.migrate
    python3 -m agent_platform.migrate --to 2        # 只升到指定版本
    python3 -m agent_platform.migrate --downgrade --to 2

设计（docs/112 §4，三期 M3-0 重构）：
* **版本链**：``MIGRATIONS`` 是按版本升序的步骤表，从当前版本逐级执行到目标版本。
  新增一版 schema 只需加一个 ``MigrationStep``，不改执行逻辑。
* 只做 expand：``schema.sql`` 幂等建表 + ``PRAGMA table_info`` 判断后
  ``ALTER TABLE ADD COLUMN``；不改历史列语义。
* **绝不倒写版本号**：当前版本 ≥ 目标版本时直接跳过（v2 旧实现无条件下写 2，
  会让已升级到 v3 的库在下次缺列迁移时被打回 v2）。
* 迁移前用 ``sqlite3.Connection.backup()`` 备份（WAL 安全；旧的 ``shutil.copy2``
  只复制主文件、漏掉 ``-wal`` 里的最近提交），最多保留 3 份。
* 迁移前后各表 ``COUNT(*)`` 对账，已有表行数不一致则回滚并报错。
* ``schema_version`` 只保留一行，值为当前版本。
"""

from __future__ import annotations

import argparse
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


#: v3 新增表（三期 M3-1~M3-3）。建表语句在 schema.sql，这里只登记回滚清单。
V3_TABLES = (
    "dispatch_plans",
    "dispatch_items",
    "dispatch_logs",
    "dispatch_events",
    "shared_resources",
    "resource_claims",
    "runtime_services",
    "runtime_checks",
    "runtime_actions",
    "runtime_action_runs",
)

#: v2 新增列（v1 -> v2 的 expand 清单）。
V2_COLUMN_UPGRADES = COLUMN_UPGRADES

#: v3 新增列。
V3_COLUMN_UPGRADES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("task_dependencies", (
        # 跨项目依赖（M3-2）：0=同项目，1=跨项目。默认 0 保持既有语义。
        ("cross_project", "INTEGER NOT NULL DEFAULT 0"),
    )),
)

#: v4（四期 M1 地基）：身份授权、Policy、Kill Switch、审批、事件平面。
V4_TABLES = (
    "principals",
    "project_members",
    "policy_rules",
    "kill_switches",
    "approvals",
    "event_outbox",
    "event_inbox",
)

#: v12（四期 M8 / P1）：能力目录域。
V12_TABLES = (
    "capability_listings",
    "capability_grants",
)

#: v13（体验层）：消息已读状态与个人保存视图，只存个人偏好，不复制业务事实。
V13_TABLES = (
    "notification_states",
    "saved_views",
)

#: v15（Agent Team）：真实 Claude Code 后台会话的启动记录。
V15_TABLES = ("team_sessions",)

#: v18（窗口会话可见）：桥接把远端窗口的会话 trace 推上来，平台才能「进去看」。
V18_TABLES = ("device_traces",)

#: v19（R2）：执行追溯质量 + 成本口径分离。
V19_TABLES = ("model_price_versions",)

#: v20（R3）：P4 八大域真实链路。
V20_TABLES = (
    "workflow_runs",
    "workflow_node_runs",
    "workflow_approvals",
    "knowledge_grants",
)

#: v22（P3-M2）：资源注册中心。建表语句在 schema.sql，这里只登记回滚清单。
V22_TABLES = (
    "registry_agent_roles",
    "registry_agent_capabilities",
    "registry_skills",
    "registry_skill_grants",
    "registry_models",
    "registry_runtimes",
    "registry_runtime_leases",
    "registry_compat_matrix",
    "registry_changes",
)

V20_COLUMN_UPGRADES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    # M4：区分「有真实流量」与「纯状态流转」
    ("agent_releases", (
        ("release_mode", "TEXT NOT NULL DEFAULT 'SIMULATED'"),
        ("traffic_entry", "TEXT"),
    )),
    # M1：候选输出必须标明来源（真实执行 / 调用方塞入）
    ("eval_runs", (
        ("output_source", "TEXT NOT NULL DEFAULT 'caller_supplied'"),
        ("runtime_execution_id", "TEXT"),
        ("output_digest", "TEXT"),
    )),
    # M8：上架时对 ref 的解析结果
    ("capability_listings", (
        ("ref_resolved", "INTEGER NOT NULL DEFAULT 0"),
        ("ref_checked_at", "TEXT"),
    )),
)

#: v19 新增列：不重命名 cost_usd（三期已写入，改名会破坏兼容），
#: 而是加两列分别承载真实与估算，cost_usd 降级为「账面值」保留。
V19_COLUMN_UPGRADES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("executions", (
        ("link_quality", "TEXT"),
        ("first_seen_source", "TEXT"),
        # 执行粒度成本：actual 来自 CLI 结果里的 total_cost_usd（provider 侧），
        # estimated 由价格版本算出；两列都空即 UNKNOWN，不倒填。
        ("actual_cost_usd", "REAL"),
        ("estimated_cost_usd", "REAL"),
        ("cost_quality", "TEXT"),
        ("price_version", "TEXT"),
    )),
    ("token_usage_events", (
        ("actual_cost_usd", "REAL"),
        ("estimated_cost_usd", "REAL"),
        ("cost_quality", "TEXT"),
    )),
)

#: v17（窗口派发）：各机器打开的 Claude CLI 会话登记 + 平台下发的指令队列。
V17_TABLES = ("device_sessions", "device_commands")

#: v16（执行状态回显）：状态自报事件记录自报状态（idle/running/blocked…），
#: 让时间线与卡片能回显「Agent 自称在做什么」，而不是一律「未验证」。
#: 自报不是执行证据，``ok`` 仍然只由真实执行结果决定。
V16_COLUMN_UPGRADES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("agent_events", (("reported_state", "TEXT"),)),
)

#: v14（交付中心）：交付校验结果与返工次数。校验结果只是证据快照，
#: 交付状态仍以 ``deliveries.status`` 为唯一事实源。
V14_COLUMN_UPGRADES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("deliveries", (
        ("validation_state", "TEXT"),
        ("validated_at", "TEXT"),
        ("validation_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("resubmit_count", "INTEGER NOT NULL DEFAULT 0"),
    )),
)

#: v10（四期 M6）：业务价值域。
V10_TABLES = (
    "outcome_metric_definitions",
    "outcome_metric_versions",
    "outcome_records",
)

#: v11（四期 M7）：自主化资格域。
V11_TABLES = (
    "autonomy_eligibilities",
    "autonomy_changes",
)

#: v9（四期 M5）：持续改进域。
V9_TABLES = (
    "failure_patterns",
    "improvement_cases",
    "change_proposals",
    "sandbox_evaluations",
)

#: v8（四期 M4）：发布治理域。
V8_TABLES = (
    "agent_releases",
    "release_manifest_items",
    "canary_deployments",
    "rollback_records",
)

#: v7（四期 M3）：Workflow 域。
V7_TABLES = (
    "workflow_definitions",
    "workflow_versions",
    "workflow_test_runs",
    "workflow_bindings",
)

#: v6（四期 M2）：知识域。
V6_TABLES = (
    "knowledge_sources",
    "knowledge_items",
    "knowledge_versions",
    "knowledge_chunks",
    "knowledge_fts",
    "retrieval_traces",
)

#: v5（四期 M1）：评估域。
V5_TABLES = (
    "eval_suites",
    "eval_datasets",
    "eval_dataset_versions",
    "eval_cases",
    "eval_evaluators",
    "eval_evaluator_versions",
    "eval_runs",
    "eval_results",
)

V4_COLUMN_UPGRADES: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    # 全链路关联标识：审计与事件必须能串起同一次业务链路。
    ("audit_logs", (
        ("correlation_id", "TEXT"),
    )),
    ("dispatch_items", (
        ("correlation_id", "TEXT"),
    )),
    ("executions", (
        ("correlation_id", "TEXT"),
    )),
)


# ---------------------------------------------------------------------------
# 版本链
# ---------------------------------------------------------------------------
class MigrationStep:
    """一步 schema 变更：目标版本 + 新增表 + 新增列 + 可选数据回填。"""

    def __init__(
        self,
        version: int,
        *,
        tables: tuple[str, ...] = (),
        columns: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (),
        backfill: Any = None,
        description: str = "",
    ) -> None:
        self.version = version
        self.tables = tables
        self.columns = columns
        self.backfill = backfill
        self.description = description


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    _backfill_mirror_source(conn)


def _migrate_v15_to_v16(conn: sqlite3.Connection) -> None:
    """回填历史状态自报事件的自报状态。

    每个 Agent 的早期自报无法逐条还原（事件里当时没存状态），
    只把「最近一条」对齐到当前 status 文件里的状态；更早的仍保持 NULL，
    页面会按「未验证」显示并附原始 note，不做猜测。
    """
    rows = conn.execute(
        """
        SELECT e.id AS id,
               CASE WHEN json_valid(a.metadata_json)
                    THEN json_extract(a.metadata_json, '$.self_report.state') END AS state
        FROM agent_events e
        JOIN project_agents a ON a.id = e.agent_id
        WHERE e.event_type = 'status_report' AND e.reported_state IS NULL
          AND e.id = (
              SELECT e2.id FROM agent_events e2
              WHERE e2.agent_id = e.agent_id AND e2.event_type = 'status_report'
              ORDER BY e2.occurred_at DESC LIMIT 1
          )
        """
    ).fetchall()
    for row in rows:
        state = str(row["state"] or "").strip()
        if not state:
            continue
        conn.execute(
            "UPDATE agent_events SET reported_state = ? WHERE id = ?", (state, row["id"])
        )


def _migrate_v18_to_v19(conn: sqlite3.Connection) -> None:
    """R2 数据修正：标注历史执行追溯质量 + 成本口径分层。

    D-1（不伪造关联）：历史 12 条 execution 全部 ``source='transcript'`` 且
    ``task_id IS NULL``。transcript 采集的是「本机跑过的 Claude 会话」，
    这些会话本来就不由平台调度，因此**没有** Task 归属是正常语义，
    而不是丢了关联。这里只做标注：

    * 能通过 ``dispatch_items.session_id`` 精确匹配到调度项的 → ``linked``
      并补上 task_id（这是确定性证据，不是猜测）；
    * 其余 transcript 记录 → ``legacy_unlinked``，``task_id`` 保持 NULL。

    绝不按时间或文本相似度回填 Task。

    D-2（不按现价倒填）：历史 ``cost_usd`` 全为空。价格版本表刚建立，
    没有任何一条历史事件能证明「当时用的是哪个价格」，因此一律标
    ``cost_quality='unknown'``，两列成本都保持 NULL。只有将来真实采集
    到 ``total_cost_usd`` 或命中带生效区间的价格版本才写值。
    """
    # ---- D-1：只认确定性证据（session_id 精确匹配）
    conn.execute(
        """
        UPDATE executions SET
            task_id = (
                SELECT d.task_id FROM dispatch_items d
                WHERE d.session_id = executions.session_id
                  AND d.task_id IS NOT NULL
                LIMIT 1
            ),
            link_quality = 'linked'
        WHERE task_id IS NULL
          AND EXISTS (
              SELECT 1 FROM dispatch_items d
              WHERE d.session_id = executions.session_id
                AND d.task_id IS NOT NULL
          )
        """
    )
    conn.execute(
        "UPDATE executions SET link_quality = 'linked'"
        " WHERE link_quality IS NULL AND task_id IS NOT NULL"
    )
    # 调度器写入的执行必须带 task_id（有任务才有调度项）。
    conn.execute(
        "UPDATE executions SET link_quality = CASE"
        "   WHEN task_id IS NOT NULL THEN 'linked' ELSE 'dispatcher_missing_task' END"
        " WHERE link_quality IS NULL AND source = 'dispatcher'"
    )
    # transcript 采集的人工会话：本来就不属于任何 Task。
    conn.execute(
        "UPDATE executions SET link_quality = 'legacy_unlinked'"
        " WHERE link_quality IS NULL AND task_id IS NULL AND source = 'transcript'"
    )
    conn.execute(
        "UPDATE executions SET link_quality = 'unknown' WHERE link_quality IS NULL"
    )
    # first_seen_source：记录这条执行第一次是被谁写进来的，便于解释口径差异。
    conn.execute(
        "UPDATE executions SET first_seen_source = source WHERE first_seen_source IS NULL"
    )

    # ---- D-2：历史成本一律 UNKNOWN，不倒填
    conn.execute(
        "UPDATE token_usage_events SET cost_quality = CASE"
        "   WHEN actual_cost_usd IS NOT NULL THEN 'actual'"
        "   WHEN estimated_cost_usd IS NOT NULL THEN 'estimated'"
        "   WHEN cost_usd IS NOT NULL THEN 'legacy_ledger'"
        "   ELSE 'unknown' END"
        " WHERE cost_quality IS NULL"
    )


def _migrate_v19_to_v20(conn: sqlite3.Connection) -> None:
    """R3 数据修正：把已有 P4 记录的"是不是真的跑过"标清楚。

    M4：此前所有 release 都没有 mode 概念，`publish()` 只是改状态。
    没有任何一条 release 存在真实流量路由的证据，因此历史记录一律
    ``SIMULATED``。这不是贬低，而是事实：NL 里没有 traffic_entry。
    有真实流量入口的应当在发布时显式提供并留下证据。

    M1：历史 eval_run 的 candidate_outputs 全部由调用方传入（代码里就是这么写的），
    一律标 ``caller_supplied``，不得冒充运行时产出。

    M8：历史 listing 的 ref 从未被解析过，标 ``ref_resolved=0``，
    由后续校验逐个补，而不是假装都有效。
    """
    conn.execute(
        "UPDATE agent_releases SET release_mode = 'SIMULATED'"
        " WHERE release_mode IS NULL"
    )
    conn.execute(
        "UPDATE eval_runs SET output_source = 'caller_supplied'"
        " WHERE output_source IS NULL"
    )
    conn.execute(
        "UPDATE capability_listings SET ref_resolved = 0"
        " WHERE ref_resolved IS NULL"
    )


def _migrate_v21_to_v22(conn: sqlite3.Connection) -> None:
    """P3-M2 回填：把已有 project_agents 的角色去重登记成 Role Definition。

    为什么回填而不是空表开张：`project_agents` 里已经跑着 14 个真实角色，
    如果 Registry 一开始是空的，"系统里有什么角色"这个问题仍然没有答案。
    回填只做「登记」，不改任何现有实例。

    **不编造能力**：capabilities 一条都不写。现有代码里没有能力声明的依据，
    凭空填就是编数据。能力由后续人工/证据补齐。
    """
    now = store.now_iso()
    rows = conn.execute(
        "SELECT DISTINCT role_key, execution_mode FROM project_agents"
        " WHERE role_key IS NOT NULL AND role_key <> ''"
    ).fetchall()
    for row in rows:
        role_key = row["role_key"]
        mode = row["execution_mode"] or "readonly"
        exists = conn.execute(
            "SELECT 1 FROM registry_agent_roles WHERE role_key = ?", (role_key,)
        ).fetchone()
        if exists is not None:
            continue
        conn.execute(
            "INSERT INTO registry_agent_roles (id, role_key, display_name, description,"
            " execution_mode, source, version, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'project', 1, 'active', ?, ?)",
            (f"role_{role_key}", role_key, role_key,
             "由 project_agents 回填（能力待补，未编造）", mode, now, now),
        )
        conn.execute(
            "INSERT INTO registry_changes (id, entity_type, entity_id, action,"
            " from_version, to_version, diff_json, changed_by, created_at)"
            " VALUES (?, 'agent_role', ?, 'create', NULL, 1, ?, 'migration-v22', ?)",
            (f"chg_{role_key}_{now}", f"role_{role_key}",
             store.dumps_json({"source": "backfill_from_project_agents"}), now),
        )


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """v3 数据修正。

    runtime_action_runs.action_id 早期定义带外键指向 runtime_actions，
    但实际可执行动作目录在代码里（ACTION_CATALOG），表可能为空，
    会让执行记录写不进去。SQLite 改不了约束，只能重建表。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='runtime_action_runs'"
    ).fetchone()
    if row is None or "REFERENCES runtime_actions" not in (row["sql"] or ""):
        return
    conn.execute("ALTER TABLE runtime_action_runs RENAME TO runtime_action_runs_old")
    conn.execute(
        """
        CREATE TABLE runtime_action_runs (
          id               TEXT PRIMARY KEY,
          action_id        TEXT NOT NULL,
          project_id       TEXT,
          status           TEXT NOT NULL DEFAULT 'pending',
          preflight_output TEXT,
          preflight_token  TEXT,
          dry_run          INTEGER NOT NULL DEFAULT 1,
          output           TEXT,
          exit_code        INTEGER,
          started_at       TEXT,
          finished_at      TEXT,
          created_at       TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO runtime_action_runs SELECT id, action_id, project_id, status,"
        " preflight_output, preflight_token, dry_run, output, exit_code, started_at,"
        " finished_at, created_at FROM runtime_action_runs_old"
    )
    conn.execute("DROP TABLE runtime_action_runs_old")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runtime_action_runs_action"
        " ON runtime_action_runs(action_id, created_at DESC)"
    )


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """v4 数据回填：为已有项目建立本机所有者主体与成员关系。

    目的：让"启用鉴权"不打断本机使用。回环请求解析为 ``local-owner``，
    它被授予全部现有项目的 owner 角色；远端令牌解析为 ``remote-token``，
    默认不是任何项目成员——因此远端在没有显式 Grant 前只能被拒绝。
    """
    now = store.now_iso()
    conn.execute(
        "INSERT INTO principals (id, principal_type, display_name, is_platform_owner,"
        " status, created_at, updated_at)"
        " SELECT 'local-owner', 'user', '本机所有者', 1, 'active', ?, ?"
        " WHERE NOT EXISTS (SELECT 1 FROM principals WHERE id = 'local-owner')",
        (now, now),
    )
    # 远端令牌主体：默认不给任何项目成员关系。远端要访问必须先被显式授权，
    # 这正是"Deny by Default"；不给默认权限是刻意的，不是遗漏。
    conn.execute(
        "INSERT INTO principals (id, principal_type, display_name, is_platform_owner,"
        " status, created_at, updated_at)"
        " SELECT 'remote-token', 'service', '远端令牌', 0, 'active', ?, ?"
        " WHERE NOT EXISTS (SELECT 1 FROM principals WHERE id = 'remote-token')",
        (now, now),
    )
    for row in conn.execute("SELECT id FROM agent_projects").fetchall():
        conn.execute(
            "INSERT INTO project_members (id, project_id, principal_id, role, created_at)"
            " SELECT ?, ?, 'local-owner', 'owner', ?"
            " WHERE NOT EXISTS (SELECT 1 FROM project_members WHERE project_id = ?"
            " AND principal_id = 'local-owner')",
            (store.stable_id(row["id"], "local-owner"), row["id"], now, row["id"]),
        )


#: 版本链：按 version 升序。新增一版只追加一项。
MIGRATIONS: tuple[MigrationStep, ...] = (
    MigrationStep(
        2,
        tables=V2_TABLES,
        columns=V2_COLUMN_UPGRADES,
        backfill=_migrate_v1_to_v2,
        description="二期：任务依赖/执行/文档/收藏/审计/幂等 + 多处扩展列",
    ),
    MigrationStep(
        3,
        tables=V3_TABLES,
        columns=V3_COLUMN_UPGRADES,
        backfill=_migrate_v2_to_v3,
        description="三期：调度、共享资源、运行时控制 + 跨项目依赖列",
    ),
    MigrationStep(
        4,
        tables=V4_TABLES,
        columns=V4_COLUMN_UPGRADES,
        backfill=_migrate_v3_to_v4,
        description="四期 M1 地基：身份授权、Policy、Kill Switch、审批、事件平面",
    ),
    MigrationStep(
        5,
        tables=V5_TABLES,
        description="四期 M1 评估域：Suite / Dataset / Case / Evaluator / Run / Result",
    ),
    MigrationStep(
        6,
        tables=V6_TABLES,
        description="四期 M2 知识域：来源 / 条目 / 版本 / 分块 / 全文索引 / 检索追踪",
    ),
    MigrationStep(
        7,
        tables=V7_TABLES,
        description="四期 M3 Workflow 域：定义 / 版本 / 测试运行 / 项目绑定",
    ),
    MigrationStep(
        8,
        tables=V8_TABLES,
        description="四期 M4 发布治理域：Release / Manifest / Canary / 回滚",
    ),
    MigrationStep(
        9,
        tables=V9_TABLES,
        description="四期 M5 改进域：失败模式 / Case / 变更提案 / 沙箱评估",
    ),
    MigrationStep(
        10,
        tables=V10_TABLES,
        columns=(("token_usage_events", (
            ("cost_usd", "REAL"),
            ("price_version", "TEXT"),
            ("cost_source", "TEXT"),
        )),),
        description="四期 M6 价值域：指标定义/版本/记录 + Usage 成本列（Expand）",
    ),
    MigrationStep(
        11,
        tables=V11_TABLES,
        description="四期 M7 自主化域：资格与等级变更",
    ),
    MigrationStep(
        12,
        tables=V12_TABLES,
        description="四期 M8 P1 能力目录：上架与授权",
    ),
    MigrationStep(
        13,
        tables=V13_TABLES,
        description="体验层：消息已读状态 + 个人保存视图",
    ),
    MigrationStep(
        14,
        columns=V14_COLUMN_UPGRADES,
        description="交付中心：校验结果快照 + 返工次数",
    ),
    MigrationStep(
        15,
        tables=V15_TABLES,
        description="Agent Team：真实 Claude Code 后台会话启动记录",
    ),
    MigrationStep(
        16,
        columns=V16_COLUMN_UPGRADES,
        backfill=_migrate_v15_to_v16,
        description="执行状态回显：状态自报事件记录自报状态",
    ),
    MigrationStep(
        17,
        tables=V17_TABLES,
        description="窗口派发：设备窗口会话登记与指令队列",
    ),
    MigrationStep(
        18,
        tables=V18_TABLES,
        description="窗口会话可见：桥接上报远端窗口的会话 trace",
    ),
    MigrationStep(
        19,
        tables=V19_TABLES,
        columns=V19_COLUMN_UPGRADES,
        backfill=_migrate_v18_to_v19,
        description="R2：执行追溯质量 + 成本口径分离（真实/估算/未知）+ 价格版本表",
    ),
    MigrationStep(
        20,
        tables=V20_TABLES,
        columns=V20_COLUMN_UPGRADES,
        backfill=_migrate_v19_to_v20,
        description="R3：P4 八大域真实链路（workflow 运行时/human gate 阻断/发布模式/评估来源）",
    ),
    MigrationStep(
        21,
        columns=(("dispatch_logs", (("attempt", "INTEGER NOT NULL DEFAULT 0"),)),),
        description="R6：调度日志按 attempt 隔离（重跑不再与旧日志混在一起）",
    ),
    MigrationStep(
        22,
        tables=V22_TABLES,
        backfill=_migrate_v21_to_v22,
        description="P3-M2：资源注册中心（角色/能力/技能/模型/运行时/兼容矩阵）",
    ),
)

TARGET_VERSION = MIGRATIONS[-1].version


def _step(version: int) -> MigrationStep | None:
    for item in MIGRATIONS:
        if item.version == version:
            return item
    return None


def _tables_upto(version: int) -> tuple[str, ...]:
    """某个版本为止引入的全部表（用于降级时按版本回滚）。"""
    names: list[str] = []
    for item in MIGRATIONS:
        if item.version <= version:
            names.extend(item.tables)
    return tuple(names)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _log(message: str) -> None:
    print(f"[agent-platform] {message}", file=sys.stderr)


#: schema.sql 里标记 v3 段的注释锚点；定向升到 v2 时裁掉这一段。
_V3_MARKER = "schema v3（三期"
_V4_MARKER = "schema v4（四期"
_V5_MARKER = "schema v5（四期"
_V6_MARKER = "schema v6（四期"
_V7_MARKER = "schema v7（四期"
_V8_MARKER = "schema v8（四期"
_V9_MARKER = "schema v9（四期"
_V10_MARKER = "schema v10（四期"
_V11_MARKER = "schema v11（四期"
_V12_MARKER = "schema v12（四期"


def schema_statements(target_version: int | None = None) -> list[str]:
    """读取 schema.sql，去掉 ``--`` 注释行后按分号拆分。

    ``target_version`` 低于 v3 时裁掉 v3 段：schema.sql 描述的是**最新**形态，
    但定向升级（如测试里的 ``--to 2``）不应顺手建出更高版本的表。
    """
    text = store.SCHEMA_PATH.read_text(encoding="utf-8")
    if target_version is not None and target_version < 3 and _V3_MARKER in text:
        text = text.split(_V3_MARKER)[0]
    if target_version is not None and target_version < 4 and _V4_MARKER in text:
        text = text.split(_V4_MARKER)[0]
    if target_version is not None and target_version < 5 and _V5_MARKER in text:
        text = text.split(_V5_MARKER)[0]
    if target_version is not None and target_version < 6 and _V6_MARKER in text:
        text = text.split(_V6_MARKER)[0]
    if target_version is not None and target_version < 7 and _V7_MARKER in text:
        text = text.split(_V7_MARKER)[0]
    if target_version is not None and target_version < 8 and _V8_MARKER in text:
        text = text.split(_V8_MARKER)[0]
    if target_version is not None and target_version < 9 and _V9_MARKER in text:
        text = text.split(_V9_MARKER)[0]
    if target_version is not None and target_version < 10 and _V10_MARKER in text:
        text = text.split(_V10_MARKER)[0]
    if target_version is not None and target_version < 11 and _V11_MARKER in text:
        text = text.split(_V11_MARKER)[0]
    if target_version is not None and target_version < 12 and _V12_MARKER in text:
        text = text.split(_V12_MARKER)[0]
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


def plan_changes(
    conn: sqlite3.Connection, *, target_version: int | None = None
) -> dict[str, Any]:
    """对比当前库与目标版本的差距（只算，不写）。"""
    target = target_version or TARGET_VERSION
    tables = _tables(conn)
    missing_tables: list[str] = []
    missing_columns: list[tuple[str, str, str]] = []
    for step in MIGRATIONS:
        if step.version > target:
            continue
        missing_tables.extend(name for name in step.tables if name not in tables)
        for table, columns in step.columns:
            if table not in tables:
                continue
            present = _columns(conn, table)
            for column, ddl in columns:
                if column not in present:
                    missing_columns.append((table, column, ddl))
    schema_tables = set()
    for statement in schema_statements(target):
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
        "target_version": target,
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
    """用 sqlite3 的在线备份 API 复制库。

    旧实现用 ``shutil.copy2``，在 WAL 模式下只复制主文件，``-wal`` 里未 checkpoint
    的提交会丢失（可能得到一个缺最近事务的快照）。``Connection.backup()`` 走 SQLite
    自己的备份协议，跨 WAL 一致。
    """
    if not path.exists() or path.stat().st_size == 0:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    source_conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        dest_conn = sqlite3.connect(str(target))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()
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
    target_version: int | None = None,
    repair: bool = False,
) -> dict[str, Any]:
    """从当前版本逐级升到目标版本。返回 {version, backup, changed, steps}；失败整体回滚。

    与 v2 旧实现的区别：
    * 当前版本 ≥ 目标时**直接跳过**，绝不倒写版本号（旧代码无条件下写 2，
      会把已升到 v3 的库打回 v2）。
    * 未登记的版本（比如库是未来版本、代码是旧版）拒绝迁移而不是覆盖。
    """
    target = target_version or TARGET_VERSION
    before_version = current_version(conn)

    if before_version > target:
        raise RuntimeError(
            f"数据库版本 v{before_version} 高于目标 v{target}："
            "这通常意味着代码比库旧。拒绝迁移以免覆盖更高版本的数据。"
        )
    if target not in {0, *[item.version for item in MIGRATIONS]} and target != before_version:
        raise RuntimeError(f"目标版本 v{target} 未在 MIGRATIONS 中登记")

    pending = [item for item in MIGRATIONS if before_version < item.version <= target]
    if repair:
        # 版本号已是最新、但需要重跑数据修正（例如表结构约束变更）时用。
        pending = [item for item in MIGRATIONS if item.version <= target]
    plan = plan_changes(conn, target_version=target)
    needs_work = bool(
        pending
        or plan["missing_schema_tables"]
        or plan["missing_tables"]
        or plan["missing_columns"]
    )
    if not needs_work:
        if verbose:
            _log(f"schema v{before_version} 已是最新，无需迁移")
        return {"version": before_version, "backup": None, "changed": False, "steps": []}

    path = _db_path(conn)
    backup_path: Path | None = None
    if backup and not no_backup and path is not None and before_version >= 1:
        backup_path = backup_database(path)

    tables_before = plan["tables"] or _tables(conn)
    counts_before = _row_counts(conn, tables_before)
    statements = schema_statements(target)
    executed: list[int] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        # schema.sql 是目标形态的全量幂等建表，逐级步骤只补各自的数据回填/列。
        for statement in statements:
            conn.execute(statement)
        for table, column, ddl in plan["missing_columns"]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        for step in pending:
            if step.backfill is not None:
                step.backfill(conn)
            executed.append(step.version)
        conn.execute("DELETE FROM schema_version")
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (target, store.now_iso()),
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
    _log(f"schema v{before_version} -> v{target} 完成{suffix}")
    return {"version": target, "backup": str(backup_path) if backup_path else None,
            "changed": True, "columns_added": plan["missing_columns"],
            "steps": executed}


def downgrade(
    conn: sqlite3.Connection,
    *,
    backup: bool = True,
    no_backup: bool = False,
    target_version: int = 1,
) -> dict[str, Any]:
    """回落到目标版本：删除该版本之后引入的表；新增列保留（SQLite 删列会丢数据）。"""
    before_version = current_version(conn)
    if target_version >= before_version:
        _log(f"schema v{before_version} 不低于目标 v{target_version}，无需降级")
        return {"version": before_version, "dropped": [], "backup": None}

    drop_tables: list[str] = []
    for step in MIGRATIONS:
        if target_version < step.version <= before_version:
            drop_tables.extend(step.tables)

    path = _db_path(conn)
    backup_path: Path | None = None
    if backup and not no_backup and path is not None and path.exists() and path.stat().st_size:
        backup_path = backup_database(path)
    dropped: list[str] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in drop_tables:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if exists:
                conn.execute(f"DROP TABLE {table}")
                dropped.append(table)
        conn.execute("DELETE FROM schema_version")
        conn.execute(
            "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
            (target_version, store.now_iso()),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        raise
    _log(
        f"已回落到 schema v{target_version}：删除表 "
        + (", ".join(dropped) if dropped else "（无）")
        + "；新增列保留（SQLite 删列会丢数据）"
        + (f"；备份：{backup_path}" if backup_path else "")
    )
    return {"version": target_version, "dropped": dropped,
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
    parser = argparse.ArgumentParser(
        description=f"多项目 Agent 平台 SQLite schema 版本链迁移（当前目标 v{TARGET_VERSION}）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    parser.add_argument("--downgrade", action="store_true", help="回退到 --to 指定版本（默认 v1）")
    parser.add_argument("--to", type=int, default=None,
                        help="目标版本号；升级默认取最新，降级默认 1")
    parser.add_argument("--no-backup", action="store_true", help="跳过备份（仅测试用）")
    parser.add_argument("--repair", action="store_true",
                        help="版本已是最新时也重跑各版本的数据修正步骤")
    parser.add_argument("--db", help="覆盖数据库路径（默认 AGWS_PLATFORM_DB）")
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser() if args.db else store.default_db_path()
    print(f"数据库：{db_path}")

    if args.dry_run:
        conn = _readonly_connect(db_path)
        target = args.to if args.to is not None else TARGET_VERSION
        if conn is None:
            print("当前版本：0（库不存在或为空）")
            print(f"目标版本：{target}")
            print(f"将执行：完整 schema.sql + schema_version={target}")
            print("行数：（无现有表）")
            return 0
        try:
            plan = plan_changes(conn, target_version=target)
            version = current_version(conn)
            counts = _row_counts(conn, plan["tables"])
            print(f"当前版本：{version}")
            print(f"目标版本：{target}")
            pending = [item.version for item in MIGRATIONS if version < item.version <= target]
            print(f"将执行的步骤：{[f'v{v}' for v in pending] or '（无）'}")
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
            print(f"将执行：schema.sql 幂等建表 + 上述 ALTER + schema_version={target}")
        finally:
            conn.close()
        return 0

    conn = store.connect(db_path)
    try:
        if args.downgrade:
            result = downgrade(
                conn, no_backup=args.no_backup,
                target_version=args.to if args.to is not None else 1,
            )
            print(f"结果：{result}")
            return 0
        result = apply(
            conn, no_backup=args.no_backup, verbose=True,
            target_version=args.to if args.to is not None else TARGET_VERSION,
            repair=args.repair,
        )
        print(f"结果：{result}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
