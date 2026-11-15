# P3 当前状态证据矩阵

> 日期：2026-11-15
> 依据：`P3_V4_第三期缺口补齐实施蓝图.md`
> 状态口径：VERIFIED / PARTIAL / UNVERIFIED / MISSING / BLOCKED / NOT RUN / ACCEPTED
> **每行必须可追溯到代码/命令/文件**

| # | 里程碑 | V4.0 要求 | 现状 | 状态 | 证据 |
|---|---|---|---|---|---|
| 1 | P3-M0 | 五份 `docs/phase3/` 交付物 | 无 | **MISSING** | `ls docs/phase3` → No such file |
| 2 | P3-M1 | Event Schema/目录/幂等/重试/DLQ/重放 | Outbox+Inbox 已实现 | **PARTIAL** | `p4/events.py` 150 行：`emit/pending/dispatch_pending/replay`；`event_outbox`+`event_inbox` 表存在；后台线程 `_event_dispatcher` 每 5s |
| 3 | P3-M1 | 事件目录 / 来源注册 | 无独立目录表 | **MISSING** | 无 `event_catalog` 表；`SCHEMA_VERSION=1` 硬编码 |
| 4 | P3-M1 | 全局总览含数据新鲜度 | overview 存在，无 freshness 字段 | **PARTIAL** | `/api/agent-platform/overview` 200 |
| 5 | P3-M2 | Agent/Skill/Model/Runtime Registry | 分散 | **MISSING** | 无 `agent_registry`/`skill_definitions`/`model_providers` 表；`model_price_versions` 仅有价格 |
| 6 | P3-M2 | Runtime Registry + 心跳/租约 | `runtime_services`+`runtime_checks` | **PARTIAL** | `runtime.py` 382 行，60s 采集线程；缺心跳超时判定、`runtime_leases` |
| 7 | P3-M2 | 兼容矩阵 / 版本锁定 | 无 | **MISSING** | 无 `compatibility_matrix` 表 |
| 8 | P3-M3 | Scheduler 纯计算 | 已实现 | **VERIFIED** | `scheduler.py` 文档明确「不启动任何进程」；`plan()` 只落库 |
| 9 | P3-M3 | Executor 三开关 | 全局+项目双开关 | **PARTIAL** | `executor_policy.py:257` `AGWS_PLATFORM_EXECUTOR` 默认 off；`:267` 项目 `execution_enabled` |
| 10 | P3-M3 | **SHADOW 模式** | **不存在** | **MISSING** | `scheduler.py` 无 mode 参数；`create_plan(strategy, ...)` 无 shadow |
| 11 | P3-M3 | **RECOMMEND 为默认** | **不存在** | **MISSING** | 默认行为是「建计划→人工点 run→真执行」，无「只推荐不派发」态 |
| 12 | P3-M3 | **ASSISTED 需审批** | **仅 RBAC，无审批** | **MISSING** | `_policy_guard(..., risk="R3")` 走 `policy.decide()`；`policy_rules` 表**空**（0 行）→ 无 `requires_approval` 规则 → 直接放行 |
| 13 | P3-M3 | Global Task | 只有项目内 Task | **MISSING** | `tasks` 表有 `project_id NOT NULL`；无 `global_tasks`/`task_links` |
| 14 | P3-M3 | 决策记录与回放 | `dispatch_events` | **PARTIAL** | 有 `_event()` 留痕；缺决策理由与候选评分 |
| 15 | P3-M4 | 独立 Mac Gateway | `team.py` 直调 CLI | **MISSING** | `team.py:288` `executor_policy.bg_policy_args()` 直接构造 argv；无设备认证、无出站连接 |
| 16 | P3-M4 | 设备认证/心跳/撤销 | `device_commands` 等表存在 | **PARTIAL** | 表在，但无独立 Gateway 进程 |
| 17 | P3-M4 | CLI Adapter 统一 | 两处独立构造 argv | **PARTIAL** | `team.py` 与 `executor.py` 各自构造，未收敛 |
| 18 | P3-M5 | 跨项目依赖 | 已实现 | **VERIFIED** | `scheduler._dependency_map` 跨项目 join；`task_dependencies` |
| 19 | P3-M5 | **跨项目委派** | **不存在** | **MISSING** | 无 `delegations` 表；`capability_grants` 是资源授权非任务委派 |
| 20 | P3-M5 | Context Package | 无 | **MISSING** | 无 `context_packages` 表 |
| 21 | P3-M5 | 目标 Owner 接受/拒绝/撤销 | 无 | **MISSING** | — |
| 22 | P3-M6 | PWA Manifest | **404** | **MISSING** | `curl /manifest.json` → 404；`/sw.js` → 404；无 `app/static/` |
| 23 | P3-M6 | Service Worker / 离线 | **404** | **MISSING** | 同上 |
| 24 | P3-M6 | 响应式 Web | 已实现 | **VERIFIED** | 移动视口冒烟 7/7（iPhone 390 / iPad 768） |
| 25 | P3-M6 | 移动端鉴权 | 已实现 | **VERIFIED** | `auth.py` 148 行；`check_request` 在 `dashboard:1492` 接入 |
| 26 | P3-M7 | GitLab 接入 | 无 | **MISSING** | 无 `gitlab` 相关代码；`gh` 仅本地 CLI |
| 27 | P3-M7 | CI 数据可信 | GitHub Actions 已通 | **PARTIAL** | CI run `43690737301` success；无平台侧 CI 数据接入 |
| 28 | P3-M7 | Usage 去重 | `token_usage_events` | **PARTIAL** | `idempotency_keys` 表存在，未确认 token 去重 |
| 29 | P3-M7 | 成本分离 | 三档口径 | **VERIFIED** | `cost.py` 257 行；ACTUAL/ESTIMATED/LEGACY；覆盖率 96.75% |
| 30 | P3-M7 | 预算/预警/暂停 | 无 | **MISSING** | 无 `budgets` 表；`cost.py` 无预算判定 |
| 31 | P3-M8 | 综合验收 | 20/30 | **PARTIAL** | `docs/repair/R7_P3-M8核对报告.md` |
| 32 | P3-M8 | Runbook | 无 | **MISSING** | — |
| 33 | 全局 | Kill Switch | 已实现 | **VERIFIED** | `p4/killswitch.py` 127 行；fail-closed（无库返 503，CI 实测） |
| 34 | 全局 | 审计日志 | 已实现 | **VERIFIED** | `audit_logs` 表；S-3 13 处 |
| 35 | 全局 | 项目隔离 | 已实现 | **VERIFIED** | slug 精确匹配；`test_project_isolation` |

## 统计

| 状态 | 数量 |
|---|---|
| VERIFIED | 8 |
| PARTIAL | 10 |
| MISSING | 16 |
| BLOCKED | 0 |
| UNVERIFIED | 0 |
| **合计** | **35** |
