# P3 差距与依赖报告

> 日期：2026-11-15
> 审计范围：只读，未改代码/数据库/配置
> 依据：V4.0 实施蓝图 + 用户六层能力目标

---

## 一、总体结论

| 层 | 现状 | 差距 |
|---|---|---|
| 项目管理层 | **强** | 缺项目模板 |
| Agent 资源层 | **弱** | 无 Registry，资源散落 |
| 任务调度层 | **中** | 有 DAG，无 Global Task，无三模式 |
| 执行控制层 | **中** | 能执行，但缺 Gateway 与审批 |
| AgentOps 层 | **强** | 五域齐全（Eval/Knowledge/Workflow/Release/ROI） |
| 治理运营层 | **中偏强** | 框架齐，但 `policy_rules` 空 → 审批失效 |

**距离六层完整目标：约 60%。基础扎实，缺的是「资源层」和「跨项目能力」。**

---

## 二、逐层差距

### 层 1 · 项目管理层（强，差 1 项）

| 能力 | 现状 |
|---|---|
| 多项目 | ✅ `agent_projects`，material + ddp |
| 项目隔离 | ✅ slug 精确匹配 + 测试 |
| 独立 Agent 团队 | ✅ `project_agents` + `team.py` |
| **项目模板** | ❌ 无「从模板创建项目」流程（有 `agent_team_templates` 但非项目模板） |

### 层 2 · Agent 资源层（弱，最主要差距）

| 能力 | 现状 |
|---|---|
| Agent | ✅ `project_agents` 有实例，但**无角色定义注册表** |
| Skills | ⚠️ 只在 `ALLOWED_TOOLS` 白名单里出现 "Skill"，**无 Skill 定义/版本/来源/权限** |
| 模型 | ⚠️ 只有 `model_price_versions`（价格），**无 Model Provider/能力/上下文限制** |
| Runtime 资源池 | ⚠️ `runtime_services` 能采集，**无心跳超时、无租约、无可分配状态** |
| 兼容矩阵 | ❌ 无 |

**这是最大结构性缺口。** 现在的「资源」是散落在各模块的隐式约定，
没有一个地方能回答「系统里有哪些 Agent 角色/能力/模型/运行时，谁能用哪个版本」。

### 层 3 · 任务调度层（中）

| 能力 | 现状 |
|---|---|
| General Owner | ⚠️ 概念存在于 `team.py` owner 机制，无独立实体 |
| 任务拆解 | ❌ 无自动拆解（`plan()` 只排序已有任务） |
| DAG | ✅ 项目内 + 跨项目依赖，环检测 |
| **智能调度** | ⚠️ 排序（优先级→深度→时间）**是确定性算法，非智能匹配** |
| **Global Task** | ❌ 无 |
| **三模式** | ❌ SHADOW/RECOMMEND/ASSISTED 均无 |

### 层 4 · 执行控制层（中）

| 能力 | 现状 |
|---|---|
| Claude Code CLI | ✅ 真实集成 `claude -p --output-format stream-json` |
| **Mac Gateway** | ❌ 无独立 Gateway（`team.py` 直调 CLI） |
| Worktree | ✅ `worktree_path` + 独立 worktree |
| 执行监控 | ✅ 流式日志 + 按 attempt 隔离（v21）+ 超时强杀 |
| **审批** | ❌ `policy_rules` 空 → 闸门失效 |

### 层 5 · AgentOps 层（强）

| 域 | 现状 |
|---|---|
| Evaluation | ✅ `p4/evaluation.py` 480 行，dataset/evaluator/suite/run |
| Knowledge | ✅ `p4/knowledge.py` 479 行，chunk/version/grant/FTS |
| Workflow | ✅ `p4/workflow.py` 718 行，DAG + Human Gate 状态机 |
| Release | ✅ `p4/release.py` 496 行，Canary + 真实探测 + Rollback |
| ROI | ✅ `p4/outcome.py` + `cost.py`，三档成本口径 96.75% 覆盖 |

**这一层是三期/四期做得最实的部分，几乎无缺口。**

### 层 6 · 治理运营层（中偏强）

| 能力 | 现状 |
|---|---|
| 权限 | ✅ Principal + RBAC + 项目成员 |
| 安全 | ✅ 路径白名单、Secret 扫描、项目隔离 |
| **审批** | ❌ **规则表空 → 形同虚设**（最紧急） |
| 审计 | ✅ `audit_logs`，S-3 覆盖 13 处 |
| 成本 | ✅ 三档口径 + 覆盖率 96.75% |
| **预算** | ❌ 无预算表、无预警、无暂停 |
| 质量 | ✅ 质量闸门、返工率（样本不足） |
| 自主化治理 | ⚠️ `autonomy_eligibilities` 有，但未与调度联动 |

---

## 三、依赖关系（决定实施顺序）

```
【P0 安全止损，可立即做，不依赖任何前置】
  A1. policy_rules 初始化（审批闸门生效）
  A2. 全局执行开关恢复默认 off

【Wave 0 · 必须在写任何新代码前完成】
  B1. P3-M0 五份交付物（基线/ADR/威胁模型/DAG/试点计划）
      ← 用户批准后才可进入 Wave A

【Wave A · 资源层与事件层，互不阻塞可并行】
  C1. P3-M2 Registry（Agent/Skill/Model/Runtime + 兼容矩阵）
  C2. P3-M1 事件扩展（目录/DLQ/重试退避/新鲜度）
      ← C1 的授权判定依赖 C2 的事件留痕，但可先各自建表

【Wave B · 调度与执行，依赖 A1+C1】
  D1. P3-M3 三模式 + Facade + 决策记录
  D2. P3-M4 Mac Gateway + CLI Adapter 收敛
      ← D2 依赖 C1 的 Runtime Registry

【Wave C · 跨项目与移动端，依赖 D1】
  E1. P3-M5 Global Task + 委派 + Context Package
  E2. P3-M6 PWA（manifest/sw/通知/移动审批）

【Wave D · 数据与验收】
  F1. P3-M7 GitLab/CI + 预算 + 效能
  F2. P3-M8 综合验收 + Runbook + 回滚手册
```

**关键约束**：
- Wave 0 完成前不得写 M1 业务代码（V4.0 明令）
- `A1` 例外——它是安全止损，且改动极小

---

## 四、工作量估算（供排期参考，非承诺）

| 波次 | 内容 | 相对量 |
|---|---|---|
| P0 | 审批规则 + 开关 | 极小（~50 行 + 数据） |
| Wave 0 | 五份文档 | 中（纯文档） |
| Wave A | Registry + 事件扩展 | 大（~12 张新表） |
| Wave B | 三模式 + Gateway | 大（核心重构） |
| Wave C | Global Task + 委派 + PWA | 大（新领域） |
| Wave D | GitLab + 预算 + 验收 | 中 |

---

## 五、风险

| 风险 | 等级 | 对策 |
|---|---|---|
| 审批失效期间被误用 | **高** | A1 立即修 |
| Registry 建了不用 | 中 | 先冻结契约，再有消费方 |
| Global Task 破坏项目隔离 | 中 | 独立表 + link，不改 `tasks` |
| 委派与授权混淆 | 中 | 语义分离（见复用映射 §4） |
| Gateway 引入公网入站 | 高 | 只允许出站连接 |
| 工作量超出单用户维护能力 | 中 | 按波次交付，每波可停 |

---

## 六、下一步

按 V4.0 门禁：
1. **立即**：A1 + A2（安全止损）
2. **提交 P3-M0 五份交付物 → 暂停等待用户批准**
3. 批准后按 Wave A → D 逐波推进，每波结束暂停

本报告与 `P3_CURRENT_STATE_EVIDENCE_MATRIX.md` 共同构成 P3-M0 的输入。
