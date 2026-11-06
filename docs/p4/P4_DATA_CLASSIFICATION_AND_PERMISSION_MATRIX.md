# P4 数据分类与权限矩阵

> 交付物：P4-M0 第 6/10 份
> 依据：V6.0 §7（作用域/身份/权限）、§10（数据治理）、§22（AI 安全）、P4-SEC-001～006

---

## 0. 结论

1. **当前不存在任何"授权"实现**：`auth.py` 只区分回环/远端，持令牌者可读写全部项目。本文件定义的是**目标态**，不是现状。
2. 数据分类沿用 V6.0 §10.1 四级，不新造等级。
3. 权限模型采用 **RBAC（角色）× ABAC（资源属性/环境/风险）× Grant（跨项目）**，最终一律服务端判定。
4. 三条不可协商的红线：
   - 子资源归属必须由服务端从父资源推导，**不得相信请求体里的 `project_id`**；
   - 检索必须在候选集内做权限过滤，**禁止先全局召回再过滤**；
   - Secret 级数据**不得**进入普通业务表、日志、索引、评估集。

---

## 1. 数据分类

### 1.1 现有表逐表分级（按最高敏感字段定级）

| 表 | 分类 | 理由 | 处置 |
|---|---|---|---|
| `agent_projects` | Internal | 项目元数据 | 成员可读；改需 Project Owner |
| `project_agents` | Internal | 角色配置 | 同上 |
| `agent_team_templates` | Internal | 团队模板 | 成员可读 |
| `tasks` | Internal | 任务标题/状态 | 成员按角色 |
| `task_dependencies` | Internal | 依赖关系 | 同上 |
| `deliveries` | Internal | 交付摘要 | 同上 |
| `executions` | **Confidential** | 会话 ID、摘要、错误信息 | 最小授权，导出需审计 |
| `agent_events` | Internal | 活动事件 | 成员可读 |
| `token_usage_events` | Internal | token 计量（无内容） | 成员可读；金额列加后仍 Internal |
| `audit_logs` | **Confidential** | 操作主体与对象 | Auditor 可读；不可删 |
| `dispatch_plans` / `dispatch_items` | Internal | 调度计划 | 成员可读 |
| `dispatch_logs` | **Confidential** | `claude` 进程原始输出，可能含代码与路径 | 最小授权；保留 90 天 |
| `dispatch_events` | Internal | 状态流转 | 成员可读 |
| `shared_resources` / `resource_claims` | Internal | 资源占用 | 成员可读 |
| `runtime_services` / `runtime_checks` | Internal | 服务状态 | 成员可读 |
| `runtime_actions` / `runtime_action_runs` | **Confidential** | 命令模板与执行输出 | Operator 可读；执行需审批 |
| `project_documents` | **Confidential** | 文档正文与引用 | 成员按角色 |
| `project_favorites` / `project_recent_visits` | Internal | 个人偏好 | 仅本人 |
| `idempotency_keys` | Internal | 幂等键 | 系统 |
| `schema_version` | Public | 版本号 | 只读 |
| `data/token`（文件） | **Secret** | 访问令牌 | 0600，不入库、不打印 |
| `data/platform.db.bak-*` | **Confidential** | 全库副本，含上述一切 | 0600，限本机 |

### 1.2 第四期新增实体的默认分级

| 实体 | 默认分类 | 说明 |
|---|---|---|
| EvaluationSuite / Dataset / DatasetVersion / Case | **Confidential** | 样本可能含真实业务数据；必须授权 |
| EvaluatorDefinition / Version | Internal | 评分规则 |
| EvaluationRun / Result | Confidential | 含失败样本 |
| KnowledgeSource / Item / Version / Chunk | **Confidential**（默认） | 来源许可决定上限，不得低于来源 |
| RetrievalTrace | Confidential | 含查询词与命中 |
| WorkflowDefinition / Version | Internal | 流程定义 |
| WorkflowTestRun | Confidential | 含测试输出 |
| AgentRelease / Manifest | Internal | 版本引用与摘要 |
| CanaryDeployment / RollbackRecord | Confidential | 发布操作证据 |
| ImprovementCase / ChangeProposal | Confidential | 含失败样本与根因 |
| OutcomeMetricDefinition / Record | Internal | 指标口径与数值 |
| AutonomyEligibility | Internal | 资格证据 |
| CapabilityListing（P1） | Internal | 目录信息 |

### 1.3 Secret 处理规则

- 检测点：知识导入、评估样本入库、日志落盘、执行输出回写、导出。
- 检测内容：API Key、Bearer Token、私钥块、`.env` 形态、cookie。
- 命中处置：**阻断入库**（不是打码后入库）；日志侧打码；记录审计但不记录原文。
- 当前状态：**无任何检测**（`grep -ri "secret\|redact" app/` 无命中）。

---

## 2. 权限矩阵

### 2.1 角色

| 角色 | 来源 | 说明 |
|---|---|---|
| Platform Owner | 新建（`principals.is_platform_owner`） | 平台治理，不自动拥有各项目原始数据 |
| Project Owner | `project_members.role` | 本项目全权 |
| Operator | `project_members.role` | 可执行，高风险需审批 |
| Viewer | `project_members.role` | 只读 |
| Auditor | `project_members.role` 或平台级 | 只读审计证据 |
| Agent Identity | `principals.type='agent'` | 绑定 project + task + tool_scope + 有效期 |
| Evaluation Owner / Knowledge Curator / Workflow Owner / Release Manager / Metric Owner / Security Owner | 领域责任角色，**映射到上面五种** + 资源级权限 | 不新建全局超级角色 |

### 2.2 资源 × 动作 × 角色（P = 允许，A = 需审批，— = 禁止，R = 仅本人项目内）

| 资源 / 动作 | Platform Owner | Project Owner | Operator | Viewer | Auditor | Agent Identity |
|---|---|---|---|---|---|---|
| Project 读 | P | P(本项目) | P(本项目) | P(本项目) | P(范围) | R |
| Project 改/归档 | P | P(本项目) | — | — | — | — |
| Task 读 | P | P | P | P | P | R |
| Task 建/改 | P | P | P | — | — | R(受限) |
| Task 删除 | A | A | — | — | — | — |
| Execution 读 | P | P | P | — | P | R |
| Dispatch 计划建 | P | P | P | — | — | — |
| **Dispatch 执行** | A | A | A | — | — | — |
| Runtime 探测读 | P | P | P | P | P | — |
| **Runtime 动作执行** | A | A | A | — | — | — |
| Audit 读 | P | P(本项目) | — | — | P | — |
| Usage 读 | P | P | P | — | P | — |
| Evaluation Suite/Dataset 读 | P | P | P | P | P | R |
| Dataset 建/授权 | A | A | — | — | — | — |
| Evaluation Run 发起 | P | P | P | — | — | — |
| Knowledge 读 | P(范围) | P(本项目) | P(本项目) | P(本项目) | — | R |
| Knowledge 发布/撤销 | A | A | — | — | — | — |
| Workflow 定义改 | P | P | P | — | — | — |
| Workflow 发布 | A | A | — | — | — | — |
| Release 创建 | P | P | — | — | — | — |
| **Release 批准** | A(且非提案者) | A(且非提案者) | — | — | — | — |
| Canary/回滚执行 | A | A | A | — | — | — |
| Improvement Proposal 建 | P | P | P | — | — | R |
| Proposal 批准 | A | A | — | — | — | — |
| Outcome 指标定义 | A | A | — | — | — | — |
| Autonomy 等级提升 | A | — | — | — | — | — |
| Kill Switch 触发 | P | P(本项目) | P(本项目) | — | — | — |
| 跨项目 Grant 授予 | A | — | — | — | — | — |
| 导出 | A | A | — | — | — | — |

### 2.3 服务端鉴权顺序（对应 V6.0 §7.5，10 步）

| 步 | 实现点 | 当前 |
|---|---|---|
| 1 认证主体 | `auth.resolve_principal()` | ❌ 只有"是否回环" |
| 2 主体类型与有效会话 | `principals.type` + session | ❌ |
| 3 服务端确定 Project Scope | 从父资源推导，不信请求体 | ❌ |
| 4 校验资源真实归属 | `SELECT project_id FROM <t> WHERE id=?` | ❌ |
| 5 成员关系 + RBAC | `project_members` | ❌ |
| 6 ABAC（分类/环境/风险） | `policy.decide()` | ❌ |
| 7 跨项目 Grant | `access_grants` | ❌ |
| 8 Policy / 风险 / 预算 | `policy.decide()` | ❌ |
| 9 审批 | `approvals` 状态 | ❌ |
| 10 审计 + correlation_id | `_safe_audit` | ⚠️ 有审计，无 correlation_id |

**10 步中 9 步缺失。** 这是 GAP-1 的量化表达。

---

## 3. 隔离检查覆盖面（V6.0 §7.7 要求 9 个面）

| 面 | 当前状态 | P4 要求 |
|---|---|---|
| API | slug 精确匹配（已验证 9 项测试） | 加主体与资源归属校验 |
| 数据库查询与关联 | 全部带 `project_id` 过滤 | 加授权集合子查询 |
| 后台 Worker / 定时任务 | 运行时采集线程（已停） | 重启并带 project scope |
| 事件消费者 | 不存在 | Outbox dispatcher 必须带 scope |
| 文件存储/预览/下载 | `file_viewer_smoke` 14 项 | 加路径 jail + 主体校验 |
| 全文检索 | 不存在 | FTS5 + 前置过滤 |
| 向量检索 | 不存在 | 预留 |
| 缓存 | `state-cache` 磁盘快照 | 按项目分文件 |
| 导出/报表/聚合 | 无导出 | 导出需审批 + 审计 |
| WebSocket/SSE/通知 | 无 | P1 |
| 日志/Trace/审计 | 有审计，无 correlation_id | 全链路关联 |
| 备份恢复 | 手动 `Connection.backup()` | 恢复后撤销重放（ADR-016） |

---

## 4. 保留与删除策略

| 数据 | 保留期 | 依据 | 删除方式 |
|---|---|---|---|
| `audit_logs` | 365 天 | 安全审计链，不得因业务删除而删 | 归档，不物理删 |
| `dispatch_logs` | 90 天 | Confidential，体积大 | 物理删 + 审计留摘要 |
| `executions` | 180 天 | 统计基线 | 归档摘要 |
| `token_usage_events` | 730 天 | 成本对账 | 不删 |
| Knowledge 版本 | 永久（revoked 保留元数据） | 撤销需可追溯 | 内容可删，元数据保留 |
| Evaluation Dataset | 随版本生命周期 | 复现性 | retired 后归档 |
| `data/token` | 永久（轮换即替换） | 凭据 | 轮换非删除 |
| 备份文件 | 14 份滚动 | ADR-016 | 过期自动清理 |

**撤销传播 9 处**（ADR-009）：业务元数据 → 文件引用 → 全文索引 → 向量索引 → 检索缓存 → 结果缓存 → 导出文件 → 下游派生摘要 → 备份重放。

---

## 5. 当前安全缺口清单（进风险册）

| ID | 缺口 | 对应 V6.0 零指标 | 严重度 |
|---|---|---|---|
| SEC-G1 | 无主体，任何人持令牌 = 全部权限 | S-01/S-02/S-07 | **P0** |
| SEC-G2 | 请求体 `project_id` 可能被用于越权（未校验归属） | S-02 | **P0** |
| SEC-G3 | 无 Secret 检测 | S-03 | **P0**（知识索引上线前必须就绪） |
| SEC-G4 | 无审批机制 | S-04/S-05 | **P0** |
| SEC-G5 | 无 Kill Switch（只有环境变量重启） | S-06 | **P0** |
| SEC-G6 | 无 correlation_id，审计无法串联 | T-04 | 中 |
| SEC-G7 | `dispatch_logs` 可能包含代码与绝对路径，无脱敏 | 数据泄漏 | 中 |
| SEC-G8 | 备份文件与其他数据同目录，无独立权限 | 恢复期泄漏 | 低 |

---

## 6. 落地顺序

1. `principals` + `project_members` + `auth.resolve_principal()`（主体）
2. `authorize()` 中间件挂到 49 条路径（授权）
3. `policy.decide()` + `kill_switch`（执行边界）
4. `approvals`（高风险动作）
5. Secret 扫描（知识/评估/日志三处入口）
6. `correlation_id` 全链路
7. 撤销重放（备份恢复时）

前 4 项是 P4-M1 的前置，第 5 项是 P4-M2 的前置，第 6 项横跨 M1–M4，第 7 项在 M4 前完成。
