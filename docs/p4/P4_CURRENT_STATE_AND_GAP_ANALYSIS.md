# P4 现状与差距分析

> 交付物：P4-M0 第 3/10 份
> 依据：V6.0 §3（阶段边界与复用契约）、§5（范围与优先级）、§8（领域模型）、§31（DAG）
> 前置：`P3_CAPABILITY_EVIDENCE_MATRIX.md`、`P4_REPOSITORY_BASELINE.md`

---

## 0. 结论

1. 第四期八个业务域，**没有一个能在当前代码上直接开始**。
2. 真正的阻塞不在八个业务域，而在**三个地基层**：身份与项目级授权、Policy/Kill Switch、事件平面。这三层在本仓库完全不存在，而八个域全部依赖它们。
3. V6.0 §3.3「禁止重复建设」在当前仓库**不构成约束**——被禁止重复建设的能力，第一套根本不存在（见 `P3_CAPABILITY_EVIDENCE_MATRIX.md` §6）。
4. 因此第四期的第一个工程决策不是"评估系统怎么建"，而是"**地基层先建还是先建业务域**"。本文件的答案是：**地基层必须先行，且必须建在第三期同一进程/同一库内**（ADR-P4-002）。
5. 范围冻结建议：P0 = 一个真实项目（material）的完整闭环 + 两个隔离测试项目的越权验证，不含任何跨项目知识共享。

---

## 1. 八大业务域的依赖前提矩阵

图例：✅ 就绪 ｜ ⚠️ 部分 ｜ ❌ 缺失

| 前提 | Eval | KNW | WF | REL | IMP | OUT | AUT | CAT |
|---|---|---|---|---|---|---|---|---|
| 身份主体（Principal） | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 项目级授权 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 执行引擎（第三期 Scheduler/Runtime） | ⚠️ | — | ⚠️ | — | ⚠️ | — | ⚠️ | — |
| 事件平面 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 审计链（含 correlation_id） | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| 审批机制 | ❌ | ❌ | ❌ | ❌ | ❌ | — | ❌ | ❌ |
| Registry（Agent/Skill/Model） | ⚠️ | — | ⚠️ | ❌ | — | — | ⚠️ | ⚠️ |
| Usage Ledger（成本） | ✅ | — | — | — | — | ⚠️ | ⚠️ | — |
| 存储/检索基础设施 | ❌ | ❌ | — | — | — | — | — | — |
| GitLab / CI | — | — | — | ❌ | ❌ | — | — | — |

**读法**：任何一列要开工，都必须先有第 1、2 行。这就是"地基层先行"的依据。

---

## 2. 三个地基层缺口（GAP-1/2/3）

### GAP-1 身份与项目级授权（阻断全部八个域）

- **现状**：`auth.check_request()` 只判断"是不是回环地址"和"令牌是否相等"。没有 Principal 表、没有角色、没有资源级授权、没有 Grant。
- **后果**：V6.0 §32.1 的"未授权跨项目读取=0"**无法证明**，因为"授权"这个概念不存在；今天持令牌的远端可以读写全部项目。
- **V6.0 要求**：§7.5 规定 10 步服务端鉴权顺序，第 1 步就是"认证主体"。
- **最小可行**：`principals`（user/agent/service）+ `project_members`（role）+ 服务端 `authorize(principal, project_id, resource_type, action)` + 全部 49 条路径挂钩。**不建超级角色，不建五层组织模型**（V6.0 §7.1 明确禁止为凑模型加表）。

### GAP-2 Policy Engine 与 Kill Switch（阻断 REL / IMP / AUT）

- **现状**：唯一"策略"是 `executor_policy.py` 里硬编码的白名单 + 一个环境变量开关。停执行需要改环境变量并重启进程。
- **后果**：V6.0 §32.1「Kill Switch 被第四期绕过=0」「未审批生产变更=0」无法证明；`P4-AUT-005`（Policy Engine 是最终执行点）无处落地。
- **最小可行**：`policy_rules`（项目/资源/动作/环境/风险等级）+ `kill_switches`（scope: global/project/project+domain，带 TTL 与审计）+ 单一判定入口 `policy.decide()`，执行器与路由都必须过它。环境变量开关降级为"总闸"，不再作为唯一手段。

### GAP-3 事件平面（阻断全部八域的可观测与幂等）

- **现状**：4 张事件表各自写入，无 Envelope 统一字段（无 `correlation_id`/`causation_id`/`schema_version`），无 outbox，无投递，无重放，无 DLQ。
- **后果**：V6.0 §11.7 的 32 个事件无处发布；§11.8 的 at-least-once、去重、重放全部缺失；跨域链路无法串联（D-8）。
- **最小可行**：在现有表上做**向后兼容扩展**（加列）而非新建总线：统一 `event_envelope` 视图 + `outbox` 表 + `dispatcher` 线程 + `inbox` 去重表。复用第三期审计与 `agent_events`，不新建第二个事件服务。

---

## 3. 八域逐项差距

### 3.1 Evaluation（P4-M1）

| 需求 | 现状 | 差距 |
|---|---|---|
| P4-EVAL-001 Suite 管理 | 无 | 全新建 |
| P4-EVAL-002 Dataset/Case | 无 | 全新建 |
| P4-EVAL-003 Evaluator/Rubric 版本 | 无 | 全新建 |
| P4-EVAL-004 通过第三期 Runtime 执行 | `executor.run_item()` 可用 | 需补 `executions(source=dispatcher)`（D-4） |
| P4-EVAL-009 预算/超时/取消 | 有 `max_turns`/`timeout_s` | 无预算、无取消传播 |
| 数据授权（未授权数据不得进评估集） | 无主体 | 依赖 GAP-1 |

**可复用**：调度器、执行器、`dispatch_logs`（天然是评估运行日志）。
**必须新建**：Suite/Dataset/Case/Evaluator/Run/Result 六张表 + 报告页。

### 3.2 Knowledge（P4-M2）

- 现状：**完全不存在**。`project_documents`=0 行，无解析、无索引、无向量库、无检索 API。
- 依赖：GAP-1（检索权限前置过滤）、GAP-3（发布/撤销事件）。
- 关键约束（V6.0 §22.4、§13.5）：**禁止先全局召回再前端过滤**，必须在候选集内做项目过滤；Secret 不得进索引。
- 技术选型是 ADR-P4-005 的核心：SQLite FTS5（零依赖，与现有栈一致）vs 外部向量库（需运维 Owner）。**建议 FTS5 起步**，理由见 ADR。

### 3.3 Workflow（P4-M3）

- 现状：无 DSL、无 DAG 校验、无节点模型。仅 `task_dependencies`（0 行）可作原始依赖。
- 依赖：GAP-2（Human Gate 不能绕过）、GAP-3（发布事件）、第三期 Scheduler（执行不得另建引擎，P4-WF-008）。
- 关键约束：Workflow 执行状态**不得**在 P4 另建平行状态机（V6.0 §9.5），必须映射到第三期 Task/Execution。

### 3.4 Release（P4-M4）

- 现状：无 Manifest、无版本锁定、无 Canary、无回滚。`project_agents` 只有角色配置，无版本摘要。
- 依赖：Evaluation Gate（M1）、Knowledge（M2）、Workflow（M3）三者产物。
- 关键约束：`P4-REL-002` 要求 Prompt/Skill/Model/Workflow 四类版本锁定 + Digest；当前 Skill/Model 无 Registry（P3-M2 缺失），必须先补最小 Registry 或改用文件摘要。

### 3.5 Improvement（P4-M5）

- 现状：无 Failure Pattern、无 Case、无 Proposal、无 Sandbox。
- 依赖：REL（候选版本要能发布）、EVAL（沙箱评估）、GAP-2（禁止 Agent 自改生产）。

### 3.6 Outcome / ROI（P4-M6）

- 现状：**唯一有真实数据的地基**——`token_usage_events` 17,812 条，覆盖 2 个项目 10 天。
- 差距：无指标字典、无归因规则、无业务 KPI 映射、无真实/估算区分。
- 优势：P4-M6 可以**最早开始**（V6.0 §31.1 明确 M0→M6 边），因为它只依赖已有 Usage 数据。

### 3.7 Autonomy（P4-M7）

- 现状：无 A0–A4 分级，`keeps ENABLED/DISABLED` 二值。
- 依赖：EVAL + REL + IMP + OUT 四域证据，是全链路最后一环。

### 3.8 Catalog（P4-M8）

- P0 部分只要求"至少两个隔离测试项目完成越权测试"，P1 才交付 Catalog MVP。**P0 不建 Catalog**。

---

## 4. 范围冻结

### 4.1 P0（首批正式验收，必须完成）

| 域 | P0 范围 |
|---|---|
| 地基 | GAP-1 身份与项目级授权、GAP-2 Policy+Kill Switch、GAP-3 事件平面 |
| EVAL | Suite / Dataset / Case / Evaluator / Run / 报告 / 预算超时取消 / Holdout 隔离 |
| KNW | 来源注册 → 审核 → 版本 → 权限过滤检索 → 引用 → 撤销（仅项目级私有） |
| WF | 定义/版本/Schema/DAG 校验/沙箱测试/发布/项目绑定，执行走第三期 Scheduler |
| REL | Manifest + 四类版本锁定 + Digest + 兼容性 + 评估门禁 + 人工审批 + 单项目 Canary + 完整回滚 |
| IMP | 失败信号 → Case → 根因（带证据）→ Proposal → 沙箱评估 → 人工批准 → Canary |
| OUT | 指标字典 + 真实 Usage 读取 + 成本归因 + 真实/估算标记 + 业务 Owner 确认 |
| AUT | A0–A4 定义 + 资格评估 + 范围绑定 + Policy 集成 + 自动降级；**生产默认不超过 A2** |
| OPS | 审计 + 脱敏 + 监控 + 备份恢复演练 + Runbook |
| 试点 | material 端到端闭环（1 个真实项目） |

### 4.2 明确不做（P1/P2，本期范围外）

- 跨项目 / 组织级知识共享（P1，需显式 Grant）
- 高级 Benchmark、A/B 实验（P1）
- Workflow 模板继承与多项目推广（P1）
- 多版本 Canary（P1）
- A3 Scoped Auto（P1）、A4 Extended Auto（P2）
- Capability Catalog 完整生命周期（P1）、外部能力市场（P2）
- 组织级知识图谱、因果推断（P2）

### 4.3 冻结声明

```
P0 需求条目：  P4-GOV/ARCH/SEC/EVAL/KNW/WF/REL/IMP/OUT/AUT/OPS 中标记 P0 的全部条目
P0 里程碑：    M0 → M1 → (M2 ∥ M3) → M4 → M5 → M6 → M7 → M8(P0 部分)
P1 起点：      P4-M8 P1 部分，需用户二次批准
P2：           本期不启动
```

---

## 5. 与 V6.0 §3.3「禁止重复建设」的对账

| 被禁止的平行系统 | 本仓库是否已有第一套 | 第四期处置 |
|---|---|---|
| 第二套 Identity / RBAC | ❌ 无 | **首次建设**（并入现有 HTTP 层） |
| 第二套 Task / DAG | ✅ 有 `tasks`/`task_dependencies` | 复用，只加跨项目 Grant 字段 |
| 第二套 Scheduler | ✅ 有 `scheduler.py` | 复用，P4 只提交任务不建调度器 |
| 第二套 Registry | ❌ 无 | **首次建设**（最小版，仅 Release 需要的四类） |
| 第二套 Gateway | ❌ 无 | **首次建设**（并入 `executor.py`） |
| 第二套 Event Plane | ❌ 无 | **首次建设**（扩展现有表，不新建服务） |
| 第二套 Approval / Audit | 审计 ✅；审批 ❌ | 审计复用；审批首次建设 |
| 第二套 Kill Switch | ❌ 无 | **首次建设** |
| 第二套 Usage Ledger | ✅ `token_usage_events` | 复用，禁止重复记账（P4-OUT-002） |
| 第二套 GitLab / CI Hub | ❌ 无 | P0 不建；P1 再议 |
| 第二套全局指挥中心 | ✅ 平台 UI | 复用，只加视图 |

**结论**：需要"首次建设"的 6 项，全部并入现有单进程与单库，不新增进程、不新增数据库、不新增 HTTP 服务。这一条写死进 ADR-P4-001/002。

---

## 6. 对 P4-M1 的直接输入

1. 先做 GAP-1 与 GAP-3（M1 需要"谁能跑评估"和"评估事件往哪发"）。
2. Evaluation 复用 `executor.run_item()`，但必须先补 `executions(source=dispatcher)` 写入（D-4），否则评估运行与执行事实对不上。
3. Dataset 授权的前提是项目级授权（GAP-1），所以 M1 的"未授权数据无法进入评估集"门禁只能在 GAP-1 完成后才能验证。
4. 成本对账直接用 `token_usage_events`，不得新建账本。
