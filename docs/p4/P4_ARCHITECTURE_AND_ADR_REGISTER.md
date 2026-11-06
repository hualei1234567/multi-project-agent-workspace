# P4 架构基线与 ADR 登记册

> 交付物：P4-M0 第 5/10 份
> 依据：V6.0 §6（架构原则）、§7（权限）、§8（领域模型）、§11（API/Event）、§34（ADR 清单）
> 状态说明：全部 ADR 状态为 **Proposed**，随 P4-M0 Decision Pack 一并提交用户批准；用户批准 P4-M1 即视为批准本册。

---

## 1. 架构基线

### 1.1 部署形态

```
┌──────────────────────────────────────────────────────────────┐
│  单进程 Python 3.11（ThreadingHTTPServer，127.0.0.1:8099）    │
│                                                              │
│  agents_dashboard.py   HTTP 层 / 路由 / 鉴权入口              │
│  agent_platform/       第三期 Control Plane（复用）           │
│    ├ service.py        项目·任务·交付·审计                    │
│    ├ scheduler.py      排程（纯计算）                         │
│    ├ executor.py       唯一进程启动点                         │
│    ├ runtime.py        探测与受控动作                         │
│    └ auth.py           令牌（本期扩展为 Principal）           │
│  agent_platform/p4/    第四期 Intelligence & Operations       │
│    ├ governance/       身份·RBAC·Policy·Approval·KillSwitch   │
│    ├ eventbus/         Envelope·Outbox·Dispatcher·Inbox       │
│    ├ evaluation/        Suite·Dataset·Evaluator·Run·Result    │
│    ├ knowledge/         Source·Item·Version·Chunk·Retrieval   │
│    ├ workflow/          Definition·Version·Validate·Bind      │
│    ├ release/           Manifest·Gate·Canary·Rollback         │
│    ├ improvement/       Case·Proposal·Sandbox                 │
│    ├ outcome/           Metric·Attribution·ROI                │
│    ├ autonomy/          Eligibility·Policy 集成               │
│    └ catalog/           （P1，本期不建）                      │
│                                                              │
│  单一 SQLite：data/platform.db（schema v3 → 增量到 v4+）      │
└──────────────────────────────────────────────────────────────┘
```

**硬约束**：不新增进程、不新增数据库、不新增 HTTP 服务、不引入 Web 框架、不引入 ORM、不拆前端（与第三期 §4「明确不做的事」一致）。

### 1.2 分层职责

| 层 | 职责 | 禁止 |
|---|---|---|
| HTTP | 认证 → 解析 → **授权** → 路由 → 错误体 | 业务计算、直接 SQL |
| Governance | Principal / RBAC / Policy / Approval / KillSwitch | 业务语义 |
| Domain（p4/*） | 各域状态机与不变式 | 绕过 Governance 直接写 |
| Control Plane（第三期） | 任务/执行/调度/运行时/审计 | 被 P4 重写 |

### 1.3 不变式（写进代码，不只写文档）

1. 任何 P4 写操作必须经过 `policy.decide(principal, project_id, resource, action)`。
2. 任何执行必须经 `executor.run_item()`，P4 不得自行 `subprocess`。
3. 任何跨项目读取必须经 Grant；无 Grant 一律拒绝（Deny by Default）。
4. 任何状态变更必须写 `audit_logs` 且带 `correlation_id`。
5. Usage 事实只由第三期账本写，P4 只读。

---

## 2. ADR 登记册

### ADR-P4-001：第四期模块化边界与部署形态

- **背景**：平台是 13k 行单进程应用，第四期要加 8 个业务域。
- **约束**：V6.0 §6.4「不预设微服务」；§3.3 禁止平行系统；运维只有一个 Owner（本机）。
- **候选**：A) 独立服务 + 独立库；B) 同进程同库的模块化单体；C) 拆多进程同库。
- **决策**：**B**。P4 作为 `app/agent_platform/p4/` 子包，按域分目录，域间只通过明确的 service 函数调用。
- **代价**：单进程故障域大；Python GIL 限制并发（当前量级远未触顶）。
- **安全影响**：同进程意味着权限校验必须集中在一个入口，不能靠网络边界。
- **验证**：`p4` 子包可被单独 import 测试，不引入新的监听端口。

### ADR-P4-002：第三期 Control Plane 集成契约

- **背景**：V6.0 假设存在 Identity/RBAC/Registry/Gateway/EventPlane/Policy/KillSwitch，实际 6 项缺失（见证据矩阵 §6）。
- **约束**：§3.3 禁止重复建设；但"第一套不存在"时首次建设不算重复。
- **决策**：逐项归属裁定——

| 能力 | 归属 | 落地位置 |
|---|---|---|
| Identity / RBAC / Project Scope | **P4 首次建设** | `p4/governance/identity.py`，挂在 `agents_dashboard._authorized` |
| Policy Engine | **P4 首次建设** | `p4/governance/policy.py`，唯一判定入口 |
| Kill Switch | **P4 首次建设** | `p4/governance/killswitch.py`，DB 驱动，保留环境变量总闸 |
| Event Plane | **P4 首次建设** | `p4/eventbus/`，在现有事件表上扩展 |
| Registry（Agent/Skill/Model/Runtime） | **P4 建最小版** | 只覆盖 Release 需要的四类引用与摘要 |
| Gateway / CLI Adapter | **扩展第三期** | `executor.py` 增加 gateway 语义，不另起模块 |
| Task / DAG | 复用第三期 | `tasks` / `task_dependencies` |
| Scheduler | 复用第三期 | `scheduler.py` |
| Usage Ledger | 复用第三期 | `token_usage_events`（只加列，不新建） |
| Audit / Approval | 审计复用；**审批首次建设** | `audit_logs` + `p4/governance/approval.py` |

- **代价**：P4-M1 的实际第一步变成"建地基"而非"建评估"，进度看起来慢。
- **验证**：每个能力有且只有一个写入源；`grep -r "INSERT INTO"` 可枚举全部写入点。

### ADR-P4-003：多项目数据隔离与跨项目共享

- **背景**：现有多项目隔离靠 `project_id` + slug 精确匹配，无授权概念。
- **约束**：§7.1 P0 只用 `project_id`；§7.6 跨项目必须显式 Grant；§22.4 检索必须前置过滤。
- **决策**：
  - 所有 P4 表带 `project_id`，平台级资源显式 NULL 并单列白名单。
  - 新增 `project_members`（principal × project × role）。
  - 新增 `access_grants`（资源级、带 purpose/classification/expiry/approver）。
  - P0 **不开放**任何跨项目知识共享；Grant 只用于 P1。
  - 检索与聚合在 SQL 层加 `project_id IN (授权集合)`，**禁止先查全量再过滤**。
- **代价**：每个查询多一次授权集合计算（可缓存）。
- **安全影响**：直接对应 S-01/S-02 零指标。
- **验证**：两个隔离测试项目的越权用例全部 403。

### ADR-P4-004：Evaluation Dataset 与执行架构

- **背景**：第四期核心是评估，但不得自建执行引擎。
- **约束**：P4-EVAL-004「通过第三期 Runtime 执行」；§9.3 运行状态与决策状态分离。
- **决策**：
  - `EvaluationRun` → 生成 `dispatch_items` → `executor.run_item()` → 结果回写 `evaluation_results`。
  - Run 必须固定三元组：`release_id` + `dataset_version_id` + `evaluator_version_id`。
  - 样本未执行不计入通过数；`partial` 必须显示实际执行数。
- **代价**：评估吞吐受 `max_concurrency` 限制（当前每项目 1）。
- **验证**：同一三元组两次运行结果可比；`executions(source='dispatcher')` 有对应行。

### ADR-P4-005：Knowledge 存储 / 检索 / 向量索引选型

- **背景**：当前零知识能力，`project_documents`=0。
- **约束**：§6.4 不预设微服务；§13.5 检索必须权限前置；§22.4 Secret 不得入库。
- **候选**：A) SQLite FTS5；B) 外部向量库（qdrant/pgvector）；C) 纯向量内存索引。
- **决策**：**A 起步 + 预留向量列**。FTS5 与现有单库零运维一致；向量检索在 P1 评估后再定（届时走 ADR 变更）。
- **代价**：语义检索能力弱于向量库。
- **安全影响**：索引与 ACL 同库，可做事务内一致性；撤销可与索引删除同事务。
- **验证**：撤销后新请求不再命中（同事务）；Secret 扫描拦截率 100%。

### ADR-P4-006：Workflow DSL 与执行边界

- **背景**：需要可机器校验、可测试、可发布的流程定义。
- **约束**：§14.5 DSL 与静态校验；P4-WF-008 不建第二执行引擎；§9.5 不建平行执行状态机。
- **决策**：
  - DSL = JSON（节点 + 边 + 输入输出 JSON Schema）。
  - 校验：无环、无孤立节点、引用资源已授权、Schema 可解析。
  - 执行：编译成第三期 `tasks` + `task_dependencies`，由 `scheduler` 排；**不另建执行状态**。
  - Human Gate 落到 `approvals` 表，未通过则任务停在 `blocked`。
- **代价**：DSL 受第三期任务模型约束，不是通用编排引擎。
- **验证**：DAG 环检测、重复触发幂等、人工接管路径有测试。

### ADR-P4-007：Agent Release Manifest 与版本完整性

- **背景**：发布必须可完整还原；当前无版本概念。
- **约束**：P4-REL-002 四类版本锁定；§9.7 Manifest 不可变，审批后变更即失效。
- **决策**：
  - `release_manifest` 存四类引用（prompt/skill/model/workflow）的 `id + version + sha256`。
  - Manifest 整体算一个 `manifest_digest`；审批记录绑定该 digest。
  - 任一引用变化 → digest 变化 → 审批自动失效。
- **代价**：Skill/Model 无 Registry，需先建最小 Registry 或改用文件摘要（ADR-P4-002 已定）。
- **验证**：篡改任一字段后审批失效用例通过。

### ADR-P4-008：Event 一致性、Outbox/Inbox 与重放

- **背景**：现有 4 张事件表无 Envelope、无投递、无重放。
- **约束**：§11.6 不得新建第二条总线；§11.8 at-least-once + 去重 + DLQ。
- **决策**：
  - 在现有表上**加列**（`correlation_id`、`causation_id`、`schema_version`），不建新表结构。
  - 新增 `event_outbox`（事务内写）+ 单线程 `dispatcher`（复用 `state_cache` 的单飞模式）。
  - 消费端 `event_inbox`（`event_id` 唯一）去重。
  - 重放需授权 + 范围 + 幂等 + 审计。
- **代价**：单线程投递有延迟；SQLite 写竞争需 `BEGIN IMMEDIATE`。
- **验证**：outbox 积压可恢复；重复投递不产生重复副作用。

### ADR-P4-009：数据保留、删除与撤销策略

- **背景**：§10.5 要求 P4-M0 确定保留期；当前无任何保留策略，transcript 无限期保留。
- **决策**：
  - 分类沿用 Public/Internal/Confidential/Secret（§10.1），不新造等级。
  - P0 保留期：审计 365 天；执行日志 90 天；评估样本随 Dataset 版本；transcript 由第三期既有策略管理（本期不改）。
  - 撤销传播清单（9 处）：业务元数据、文件引用、全文索引、向量索引（预留）、检索缓存、结果缓存、导出、下游摘要、备份重放。
- **代价**：需要定期清理任务与撤销重放机制。
- **验证**：撤销后检索/导出/缓存全部不命中。

### ADR-P4-010：AI 安全、模型评审与人工审批

- **背景**：评估与改进都可能被模型自评影响。
- **约束**：§22；P4-REL-005 职责分离；§12.8 校准。
- **决策**：
  - 高风险发布必须人工审批，提案者不得自批。
  - 模型评审不得单独决定安全类结论；安全维度为硬门禁。
  - 评审模型与生产模型不得为同一实例（可用则分离，不可用则标记 limitation）。
- **验证**：安全失败用例无法被综合分抵消。

### ADR-P4-011：Outcome 指标口径与 ROI 归因

- **背景**：`token_usage_events` 有 token 无金额，`executions.task_id` 全空。
- **约束**：P4-OUT-002 不重复记账；§17.5 真实/估算分离；§17.7 价值归因方法。
- **决策**：
  - 对 `token_usage_events` **Expand 加列**：`cost_usd`、`price_version`、`cost_source`（actual/estimated）。
  - 归因链：`execution → task → project → release`，缺环即标 `unknown`。
  - 相关性不得表述为因果；无对照组时标 `待验证`。
- **验证**：任一条成本可回溯到 price_version 与来源。

### ADR-P4-012：自动化资格与 Policy Engine 集成

- **背景**：当前自动化只有执行器开关二值。
- **约束**：P4-AUT-005 Policy 是最终执行点；§18 三套等级严格区分（业务风险 R0–R4 / 工具 T0–T4 / 自动化 A0–A4）。
- **决策**：Eligibility 只是输入，`policy.decide()` 才是授权点；资格绑定 `(project, task_type, tool_scope, env, expiry)`；P0 生产上限 A2。
- **验证**：伪造 eligibility 无法绕过 policy。

### ADR-P4-013：可观测性、SLO 与容量策略

- **背景**：`runtime_checks` 已停更；无 correlation_id；无 SLI。
- **决策**：
  - 全链路 `correlation_id` 贯穿 HTTP → 域 → 执行 → 审计 → 事件。
  - SLI 先基线后目标（§23.3）；当前只采集不设 SLO 违约。
  - 容量：单项目并发 1、全局执行并发 2 起步，按实测调。
  - 运行时采集线程必须常驻并有心跳告警（修复 D-2）。
- **验证**：`runtime_checks` 时间戳新鲜度 < 2×采集周期。

### ADR-P4-014：试点项目、灰度与推广策略

- **决策**（详见 `P4_PILOT_AND_ROLLOUT_PLAN.md`）：
  - 试点 = **material**（业务风险低、数据少、已有执行开关验证过）。
  - 隔离对照 = **ddp**，但 P0 阶段**不接入**真实数据，仅作越权测试对象。
  - 灰度顺序：沙箱 → 单项目只读 → 单项目受控写 → 扩展。
- **验证**：material 端到端闭环一次；ddp 无任何 P4 数据写入。

### ADR-P4-015：外部 Provider / Adapter 兼容性

- **背景**：模型经 TeamoRouter（Anthropic Messages 兼容）；供应商协议可能变化。
- **决策**：
  - 所有模型调用收敛到单一 adapter 接口（`call(model_id, messages, params)`），业务代码不直接拼 HTTP。
  - 记录 `model_id + provider + api_version` 到每次运行，便于复现。
  - 不兼容即拒绝发布（P4-REL-003）。
- **验证**：切换 provider 不改业务代码。

### ADR-P4-016：备份、恢复、RPO/RTO 与撤销重放

- **背景**：现有备份是 `Connection.backup()` 手动触发，无 RPO/RTO 定义。
- **决策**：
  - RPO = 24h，RTO = 1h（单机 SQLite，可满足）。
  - 每日自动备份到 `data/backups/`，保留 14 份。
  - 恢复后必须重放撤销记录（Revocation Replay），否则会出现"备份里还有已撤销知识"（V6.0 §33 P4-R18）。
  - 每季度一次恢复演练，记录到 Runbook。
- **验证**：恢复后越权测试通过；撤销重放有记录。

---

## 3. ADR 状态表

| ADR | 主题 | 状态 | 依赖 |
|---|---|---|---|
| 001 | 模块化边界与部署形态 | Proposed | — |
| 002 | Control Plane 集成契约 | Proposed | — |
| 003 | 多项目隔离与共享 | Proposed | 002 |
| 004 | Evaluation 执行架构 | Proposed | 002、008 |
| 005 | Knowledge 选型 | Proposed | 003 |
| 006 | Workflow DSL | Proposed | 002、012 |
| 007 | Release Manifest | Proposed | 004、006 |
| 008 | Event 一致性 | Proposed | — |
| 009 | 保留删除撤销 | Proposed | 003 |
| 010 | AI 安全与审批 | Proposed | 012 |
| 011 | Outcome 口径与 ROI | Proposed | 008 |
| 012 | Autonomy 与 Policy | Proposed | 002 |
| 013 | 可观测性与 SLO | Proposed | 008 |
| 014 | 试点与灰度 | Proposed | 003 |
| 015 | Provider 兼容 | Proposed | — |
| 016 | 备份恢复与撤销重放 | Proposed | 009 |

**批准方式**：随 P4-M0 Decision Pack 提交，用户批准 P4-M1 即视为整册通过；任一条被否决，对应下游 ADR 与里程碑需重新评估。
