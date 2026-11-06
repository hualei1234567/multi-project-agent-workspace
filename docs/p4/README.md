# P4-M0 Decision Pack

> 提交时间：2026-11-06
> 依据：《多项目 Agent 管理平台全版本完整总文档 V1.0–V6.0》附录 A13（V6.0）§30.2
> 状态：**已完成，等待用户批准 P4-M1**

---

## 1. 交付物清单（10 + 2）

| # | 文件 | 内容 | 行数 |
|---|---|---|---:|
| 1 | `P3_CAPABILITY_EVIDENCE_MATRIX.md` | 第三期能力逐项状态（含 V4.0 P3-M0–M8 核验、8 项新缺陷） | 155 |
| 2 | `P4_REPOSITORY_BASELINE.md` | 仓库/DB/API/前端/运行态/12 项 §24.1 测量 | 208 |
| 3 | `P4_CURRENT_STATE_AND_GAP_ANALYSIS.md` | 八域依赖矩阵、三个地基层缺口、P0 范围冻结 | 183 |
| 4 | `P4_VALUE_BASELINE_AND_METRIC_DICTIONARY.md` | 6 项冻结基线 + 7 类指标口径 + BLOCKED 清单 | 151 |
| 5 | `P4_ARCHITECTURE_AND_ADR_REGISTER.md` | 架构基线与 16 条 ADR | 262 |
| 6 | `P4_DATA_CLASSIFICATION_AND_PERMISSION_MATRIX.md` | 逐表分级、角色×动作矩阵、10 步鉴权对照 | 204 |
| 7 | `P4_PILOT_AND_ROLLOUT_PLAN.md` | material 试点、四波推广、停止条件 | 135 |
| 8 | `P4_REQUIREMENT_TRACEABILITY_MATRIX.md` | 92 条需求全量追踪 | 180 |
| 9 | `P4_MILESTONE_DAG_AND_WORKTREE_PLAN.md` | DAG、门禁、Worktree、1+6 文件 Owner | 179 |
| 10 | `P4_RISK_REGISTER.md` | V6.0 18 条 + 实测新增 7 条 | 134 |
| 附 | `P4_PRECONDITION_GAP_REPORT.md` | 准入条件与阻断项 B-1～B-8 | 104 |
| 附 | `README.md` | 本文件 | — |

---

## 2. 十项结论

1. **P3-M8 无正式验收证据**（技术判定），用户 2026-11-06 裁定豁免并承担风险。
2. **V4.0 的 P3-M1/M2/M4/M7 从未建设**：事件平面、Registry、CLI Gateway、GitLab/CI/预算全部 MISSING。
3. 第四期**不能在假设里开工**：8 个业务域没有一个能直接开，全部依赖三个不存在的地基层。
4. 三个地基层 = **身份与授权、Policy + Kill Switch、事件平面**。
5. 安全零指标 7 项中，**6 项当前"测不了"**，不是"等于 0"。
6. 成本、周期、返工、人工介入四类指标**全部不可算**：无 cost 列、`executions.task_id` 全空。
7. 试点选 **material**（生产风险低、已验证过一次真实执行），**ddp 在 P0 阶段零写入**，只作越权测试对象。
8. 范围冻结：P0 = 78 条需求、1 个试点项目、两个隔离项目的越权验证；跨项目知识共享进 P1。
9. 架构冻结：**单进程 + 单库 + 零新增服务**，16 条 ADR 随本包提交批准。
10. 新增 7 条实测风险（R19–R25），其中 4 条为 P0 阻断，全部排入 M1 前置。

---

## 3. 门禁自评（V6.0 §30.2 的 9 项）

| 门禁 | 自评 |
|---|---|
| P3-M8 证据完整 | ⚠️ 用户裁定豁免 |
| 第三期依赖状态有证据 | ✅ |
| 试点项目明确 | ✅ |
| Project Owner / 业务 Owner 明确 | ❌ **待用户** |
| 数据授权明确 | ❌ **待用户** |
| P0/P1/P2 范围冻结 | ✅ |
| 关键 ADR 批准 | ⚠️ 待随本包批准 |
| 指标口径与基线可复核 | ✅ |
| 无未处理 P0 安全阻断项 | ❌ 4 项未处理（转入 M1 DoR） |

**自评结论：文档交付完成；P4-M1 开工门槛未满足，需用户三项输入 + 一次批准。**

---

## 4. 需要用户决定的三件事

### D-1 指定业务 Owner 与 Project Owner（材料试点）

建议：业务 Owner 与 Project Owner 由同一人担任（当前团队规模下职责分离成本高于收益）。
没有这个人，M1 的「数据授权」与 M6 的「KPI 确认」都无从签署。

### D-2 批准或修改数据授权四项

| 项 | 建议值 |
|---|---|
| 试点数据范围 | material 仓库代码 + 工程文档 + 执行日志，**不含客户数据** |
| transcript 是否可进评估集 | **不允许**（需逐条授权例外） |
| ddp 是否参与对照 | **不参与**，P0 阶段零 P4 数据写入 |
| 保留期 | 审计 365 天 / 执行日志 90 天 / Usage 730 天 |

### D-3 提供 GitLab 仓库地址（可选但影响 M1 DoR）

当前 `git remote -v` 为空，无 MR、无 CI。若短期不提供，P4-M1 将以"本地测试 + 手工合并"运行，并在阶段报告中标注为限制（风险 P4-R21）。

---

## 5. 批准后 P4-M1 的第一步（不是写评估）

```
1. principals + project_members + authorize() 中间件      ← 风险 R19
2. kill_switches + policy.decide() 唯一入口                ← 风险 R20
3. event_outbox / event_inbox + dispatcher                 ← 风险 R14
4. executions.task_id + source='dispatcher' + correlation_id ← 风险 R23
5. runtime 采集线程常驻 + /healthz 实现                     ← 风险 R22
6. 两隔离项目越权测试（0 命中）                              ← 门禁证据
—— 以上完成并通过测试后，才开始 Evaluation Suite/Dataset ——
```

理由：评估系统的第一个门禁是"未授权数据无法进入评估集"。没有第 1 步，这个门禁无法验证。

---

## 6. 合规声明

本阶段只做了：只读检查（git / sqlite / lsof / pytest 只读运行）、新增 `docs/p4/` 下的 Markdown 文件。
未做：修改代码、执行 Migration、创建/修改表、改配置、部署、接入生产数据、开放 P4 功能、提交 git。
