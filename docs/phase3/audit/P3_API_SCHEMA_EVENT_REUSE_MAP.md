# P3 API / Schema / Event 复用映射

> 目的：P3 建设必须复用既有契约，不建平行第二套。本表逐项标明「复用 / 扩展 / 新建」。

## 1. API 复用映射

| V4.0 需要 | 现有 API | 决策 |
|---|---|---|
| 全局总览 | `/api/agent-platform/overview` | **复用**，加 `freshness` 字段 |
| 项目事件 | `/api/projects/{id}/events` | **复用**，加 `source_type`/`correlation_id` |
| 跨项目视图 | `/api/agent-platform/cross-project` | **复用** |
| 调度计划 | `/api/projects/{id}/dispatch/plan(s)` | **复用**，加 `mode` 参数 |
| 调度项执行 | `/api/projects/{id}/dispatch/items/{id}/run` | **复用**，加审批校验 |
| Runtime 服务 | `/api/runtime/services` | **复用** |
| 审批 | `/api/projects/{id}/approvals/{id}/approve` | **复用** |
| 自治资格 | `/api/projects/{id}/autonomy/eligibilities` | **复用** |
| 能力目录 | `/api/projects/{id}/catalog/listings` | **复用**（≠ Registry） |
| **Global Task** | 无 | **新建** |
| **Registry** | 无 | **新建**（独立于 catalog） |
| **委派** | 无 | **新建** |
| **Context Package** | 无 | **新建** |
| **PWA manifest/sw** | 无（404） | **新建** |
| **预算** | 无 | **新建** |

## 2. Schema 复用映射

现有 **76 张表**（schema v21）。关键判断：

| 现有表 | 用途 | P3 决策 |
|---|---|---|
| `tasks` | 项目内任务（`project_id NOT NULL`） | **不改**。Global Task 另建表 + link 表 |
| `task_dependencies` | 含跨项目依赖 | **复用**（M3-2 已支持） |
| `dispatch_plans` / `dispatch_items` | 调度 | **复用**，加 `mode` 列 |
| `dispatch_events` | 状态流转留痕 | **复用**，扩展为决策记录 |
| `executions` | 执行记录 | **复用** |
| `event_outbox` / `event_inbox` | 事件投递 | **复用**（已有幂等/DLQ/重放） |
| `runtime_services` / `runtime_checks` | Runtime 采集 | **复用**，缺租约表 |
| `principals` / `project_members` | 身份与 RBAC | **复用** |
| `policy_rules` | 策略规则 | **复用**（但当前空表，需初始化） |
| `approvals` | 审批 | **复用** |
| `audit_logs` | 审计 | **复用** |
| `kill_switches` | 熔断 | **复用** |
| `capability_listings` / `capability_grants` | 能力授权 | **复用**（≠ Registry 定义） |
| `shared_resources` / `resource_claims` | 资源争抢 | **复用** |
| `model_price_versions` | 模型价格 | **复用**，补 registry 元数据 |
| `device_commands` / `device_sessions` | 设备 | **复用**，缺 Gateway 进程 |

**新增表预估**：`global_tasks`、`task_links`、`delegations`、`context_packages`、
`agent_registry`、`skill_definitions`、`skill_versions`、`model_providers`、
`runtime_leases`、`compatibility_matrix`、`budgets`、`dispatch_decisions`。
**约 12 张，全部为新增，不改既有表结构。**

## 3. Event 复用映射

现有 `p4/events.py` 已具备 Outbox 模式的核心能力：

| V4.0 要求 | 现有实现 | 状态 |
|---|---|---|
| Event Envelope | `envelope()` 按 V6.0 §11.6 | ✅ 复用 |
| Schema Version | `SCHEMA_VERSION = 1`（模块常量） | ⚠️ 需改为按 event_type 索引 |
| 去重幂等 | `event_inbox (event_id, consumer)` | ✅ 复用 |
| 重试退避 | 有 attempts 计数，**无退避间隔** | ⚠️ 需加 |
| DLQ | `status='failed'` 到上限即停 | ⚠️ 有停机无独立 DLQ 表与查询 API |
| 重放 | `replay()` | ✅ 复用 |
| 事件目录/来源注册 | **无** | ❌ 新建 |
| correlation/causation | 字段已有 | ✅ 复用 |

**结论：事件平面是复用为主、小幅扩展，不需要第二条总线。**
这正是 `p4/events.py` 文档里写的设计取舍，方向正确。

## 4. 不能复用的地方

以下必须新建，且不可与现有表混用：

1. **Global Task ≠ tasks**。`tasks.project_id` 是 `NOT NULL`，强行放宽会破坏
   35 项现有测试与项目隔离语义。必须独立建模 + link 表。
2. **Registry ≠ capability_listings**。前者是「系统里存在什么资源」，
   后者是「某项目被授权用什么」，语义不同，合并会重蹈 R5 数据混杂的覆辙。
3. **委派 ≠ 授权**。`capability_grants` 是资源使用权，委派是任务责任转移，
   有接受/拒绝/撤销的状态机。不能复用。

## 5. 契约冻结建议

在进入 P3-M1 之前必须冻结：

- `P3_EVENT_SCHEMA_v1`（event_type 目录、必填字段、版本策略）
- `P3_REGISTRY_SCHEMA_v1`（资源身份唯一键、版本规则、兼容判定）
- `P3_DELEGATION_STATE_MACHINE_v1`（状态与转移条件）

冻结后新增 API 一律先在 `openapi.py` 登记，`test_route_contract.py` 强制同步
（现有 158 操作 / 131 路径的机制直接沿用）。
