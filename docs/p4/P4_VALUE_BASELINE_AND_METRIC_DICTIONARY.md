# P4 价值基线与指标字典

> 交付物：P4-M0 第 4/10 份
> 依据：V6.0 §17（Outcome & ROI）、§24（性能容量成本）、§32（指标与证据）、P4-OUT-001
> 原则：**Unknown 不得转成 0；Estimated 不得伪装成 Verified**（V6.0 §10.4）

---

## 0. 结论

1. 当前能冻结的基线只有 **6 项**：项目数、任务数、执行数、Token 量、测试数、库容量。
2. **成本、周期、返工、人工介入四类指标目前全部不可算**——不是因为没数据，而是因为数据链断了：
   - `token_usage_events` 只有 token 数，**没有 cost 列**（成本需外部重算）；
   - `executions.task_id` 44 条全部为空，**执行无法归到任务**；
   - 没有任何字段记录"人工介入"。
3. 因此 P4-OUT-003（任务级成本归因）在当前数据模型下是 **BLOCKED**，不是 NOT_STARTED。这是 P4-M6 的第一个前置任务。
4. 本字典冻结的是**口径**，不是目标值。目标值等业务 Owner 在 P4-M6 确认（V6.0 §32.3）。

---

## 1. 冻结基线（Verified，可复核）

| # | 指标 | 值 | 来源 | 测量时间 |
|---|---|---|---|---|
| B-1 | 纳管项目数 | 2（legacy、material） | `agent_projects` | 2026-11-06 |
| B-2 | 任务总数 / 完成数 | 33 / 27（81.8%） | `tasks` status 分布 | 2026-11-06 |
| B-3 | 执行记录数 / 成功数 | 44 / 42（95.5%） | `executions` | 2026-11-06 |
| B-4 | Token 总量 | legacy 3,267,584,533；material 260,664,478；合计 3,528,249,011 | `token_usage_events`（17,812 行） | 2026-10-28 → 2026-11-06 |
| B-5 | 自动化测试 | 192 passed / 18.55s | pytest | 2026-11-06 |
| B-6 | 平台库容量 | 9,977,856 B（25 表，16 索引） | `ls data/platform.db` | 2026-11-06 |

补充事实：

- 任务分布：legacy 27 done / 5 in_progress；material 1 todo。**material 几乎零业务数据**。
- 交付记录 17 条，全部 `submitted`，仅 1 条关联 task_id。
- 调度历史：2 个计划、1 个调度项（succeeded，exit 0，23s，$0.1715 为 claude 返回值）。
- 审计 6 条，其中 4 条为 2026-11-06 的调度执行。

---

## 2. 指标字典（口径冻结）

字段含义：**Value Source** = 数据来自哪张表/哪个计算；**Quality** = verified / estimated / partial / stale / unknown（V6.0 §10.4）；**Blocking** = 该指标不过是否阻断发布。

### 2.1 质量类（Quality）

| ID | 指标 | 口径 | 分母 | 来源 | Owner | 当前 Quality | 目标 |
|---|---|---|---|---|---|---|---|
| Q-01 | 任务一次通过率 | status=done 且无重开记录的任务数 / 全部 done 任务数 | done 任务 | `tasks`（无重开字段） | Project Owner | **unknown** | P4-M6 定 |
| Q-02 | 执行成功率 | succeeded 执行数 / 全部执行数 | 全部执行 | `executions.status` | QA | **verified**（95.5%） | 不下降 |
| Q-03 | 交付验收通过率 | approved 交付 / 全部交付 | 全部交付 | `deliveries.status` | Project Owner | **unknown**（17 条全 submitted，无 approve 记录） | P4-M6 定 |
| Q-04 | Evaluation 通过率 | decision=pass 的样本 / 实际执行样本数 | 实际执行样本（**不含未运行**） | P4-M1 新建 | Evaluation Owner | MISSING | P4-M1 定 |
| Q-05 | 关键回归检出率 | Benchmark 检出的真实回归 / 已知回归总数 | 已知回归 | P4-M1 新建 | Evaluation Owner | MISSING | 100% |

### 2.2 周期类（Cycle Time）

| ID | 指标 | 口径 | 来源 | 当前 | 说明 |
|---|---|---|---|---|---|
| C-01 | 任务周期（创建→done） | `finished_at - created_at` | `tasks` | **unknown** | 需确认 `tasks` 是否有 finished_at |
| C-02 | 执行时长 | `duration_ms` | `executions` | legacy 平均 33,270s；material 101,530s | **这是会话时长，不是任务时长**，不可当周期用 |
| C-03 | 发布周期（Proposal→published） | 状态迁移时间差 | P4-M4 新建 | MISSING | — |

**警告**：C-02 的数值看起来像"9 小时/28 小时"，因为它是 transcript 会话跨度（可能跨多次人机交互）。在 P4-M6 之前**不得**用它表述"Agent 执行效率"。

### 2.3 返工类（Rework）

| ID | 指标 | 口径 | 来源 | 当前 |
|---|---|---|---|---|
| R-01 | 失败执行重试次数 | 同 `(project_id, task_id)` 下非首次执行数 | `executions` | **BLOCKED**（`task_id` 全为 NULL） |
| R-02 | 返工率 | 被重开的任务数 / 总任务数 | `tasks` | **BLOCKED**（无重开字段） |
| R-03 | Improvement Case 数量 | 按根因分类计数 | P4-M5 新建 | MISSING |

### 2.4 人工介入类（Human Intervention）

| ID | 指标 | 口径 | 来源 | 当前 |
|---|---|---|---|---|
| H-01 | 自动化执行占比 | `dispatch_items` 成功数 / 全部执行数 | 1 / 44 = 2.3% | **verified**，但样本量=1，**不具统计意义** |
| H-02 | 人工审批次数 | `audit_logs` 中 action 含 approve 的记录数 | `audit_logs` | **0**（无私审批环节） |
| H-03 | 人工接管次数 | 运行中被人取消/接管的执行数 | 无字段 | **BLOCKED** |
| H-04 | 单任务人工分钟数 | 无采集 | — | **unknown** |

### 2.5 成本类（Cost）

| ID | 指标 | 口径 | 来源 | 当前 |
|---|---|---|---|---|
| $ -01 | Token 消耗 | Σ `total_tokens` | `token_usage_events` | **verified**：3,528,249,011 |
| $ -02 | 金额成本 | Σ(各模型单价 × tokens) | **`token_usage_events` 无 cost 列** | **BLOCKED** |
| $ -03 | 任务级成本 | 执行 → 任务归因 | `executions.task_id` 全 NULL | **BLOCKED** |
| $ -04 | 项目级成本 | 按 project_id 聚合 | `token_usage_events.project_id` | **partial**（只有 token，无金额） |
| $ -05 | 单次评估成本 | Evaluation Run 的 token/金额 | P4-M1 新建 | MISSING |

**这是 P4-M6 最大的工程债**：要算任何金额，必须先决定"加 cost 列（Expand）"还是"外挂计算"。按 V6.0 §26.2 Expand-and-Contract，建议加列，不新建账本（P4-OUT-002 禁止重复记账）。

### 2.6 安全类（必须为零，V6.0 §32.1）

| ID | 指标 | 当前可测性 | 当前值 |
|---|---|---|---|
| S-01 | 未授权跨项目读取 | **不可测**（无主体、无授权概念） | **无法证明为 0** |
| S-02 | 未授权跨项目写入 | 同上 | **无法证明为 0** |
| S-03 | Secret 进入普通知识索引 | 无知识索引 | 0（空集，非证据） |
| S-04 | 未审批生产变更 | 无审批机制 | **无法证明为 0** |
| S-05 | 审批绕过的高风险操作 | 无审批机制 | **无法证明为 0** |
| S-06 | Kill Switch 被绕过 | 无 Kill Switch | **无法证明为 0** |
| S-07 | 已撤销授权仍可发起新访问 | 无授权/撤销机制 | **无法证明为 0** |

**判定：7 项安全零指标中，当前只有 1 项（S-03）是"因为不存在所以为 0"，其余 6 项是"测不了"。** 按 V6.0 §32.1，这些是阻断门禁，不允许用"平均分"或"风险接受"掩盖。→ 这是 GAP-1/GAP-2 必须先建的直接理由。

### 2.7 可追溯类（必须 100%，V6.0 §32.2）

| ID | 指标 | 当前 |
|---|---|---|
| T-01 | 生产 Release Manifest 完整率 | **无 Release 概念** |
| T-02 | Evaluation Run 版本追溯率 | MISSING |
| T-03 | 已发布 Knowledge Source 可追溯率 | MISSING |
| T-04 | 高风险动作审计覆盖率 | **partial**：调度/运行时/资源已接入（6 条），但无 correlation_id，链路串不起来 |
| T-05 | P0 需求证据覆盖率 | **0%**（P0 尚未实施，见 RTM） |

---

## 3. 测量能力差距（为什么大部分是 BLOCKED）

| 缺失能力 | 影响的指标 | 补法 | 哪个里程碑 |
|---|---|---|---|
| 无 cost 列 | $-02/-03/-05 | Expand 加列 + 回填脚本 | P4-M6 前置 |
| `executions.task_id` 全空 | R-01、$-03 | 执行时写入 task_id | P4-M1 前置（也在 D-4 里） |
| 无 correlation_id | T-04、跨域链路 | Envelope 扩展（GAP-3） | P4-M1 前置 |
| 无审批表 | H-02、S-04/S-05 | Approval 表 + Policy | P4-M1 前置（GAP-2） |
| 无主体 | S-01/S-02/S-07 | Principal + Grant | P4-M1 前置（GAP-1） |
| 无重开/接管字段 | Q-01、R-02、H-03 | `tasks` 加 `reopened_count`，执行加 `interrupted_by` | P4-M5 |

---

## 4. 目标值设定规则（不预设数字）

V6.0 §32.3 明确：不在没有基线时统一规定固定准确率。因此本字典只冻结规则：

1. 每个目标必须写明：基线值 / 目标值 / 统计窗口 / 数据来源 / 样本量 / Owner / 失败处理 / 是否阻断发布。
2. 样本量 < 30 的指标只作观察，不作门禁。
3. 安全类指标（S-01～S-07）的阈值恒为 **0**，不参与加权，不被平均分抵消。
4. 成本类指标必须先区分 **真实账单** 与 **估算值**（V6.0 §17.5）。
5. 任何指标在数据源不可信时标 `unknown`，不得标 0。

---

## 5. 首批可立即改善的三项

| 动作 | 收益 | 成本 | 归属 |
|---|---|---|---|
| 执行时写入 `executions.task_id` + `source='dispatcher'` | 解锁 R-01、$-03，修正 D-4 | 小 | P4-M1 |
| `token_usage_events` Expand 增加 `cost_usd` / `price_version` | 解锁全部金额口径 | 中（需回填） | P4-M6 前置 |
| 全链路 `correlation_id` | 解锁 T-04，串起审计与事件 | 中 | P4-M1（GAP-3） |
