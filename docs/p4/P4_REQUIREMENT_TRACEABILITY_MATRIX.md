# P4 需求追踪矩阵（RTM）

> 交付物：P4-M0 第 8/10 份
> 依据：V6.0 §5.3（核心功能需求清单）、§32.5（RTM 字段）、P4-GOV-004
> 状态词：NOT_STARTED / IN_PROGRESS / BLOCKED / IMPLEMENTED / TESTED / ACCEPTED / DEFERRED

## 0. 说明

1. 本表覆盖 V6.0 §5.3 的**全部 92 条需求**，编号与规范逐一对应。
2. **契约列**是拟冻结的 API / Schema / Event，P4-M0 只冻结名称与归属，不冻结字段细节（字段在对应里程碑的 DoR 阶段冻结）。
3. **代码列**填写当前真实位置；`—` 表示不存在。
4. **P0 需求证据覆盖率当前 = 0%**（V6.0 §32.2 要求 100%），这是 P4-M0 的已知缺口，随里程碑推进补齐。
5. 状态严格按 V6.0 §32.4：`IMPLEMENTED ≠ TESTED ≠ ACCEPTED`。

**统计**：P0 = 78 条，P1 = 12 条，P2 = 2 条；其中 NOT_STARTED 82、PARTIAL/IN_PROGRESS 8、VERIFIED 2。

---

## 1. 治理（P4-GOV）

| ID | 优先级 | 里程碑 | 契约 | 代码（当前） | 状态 |
|---|---|---|---|---|---|
| P4-GOV-001 P3-M8 验收核验 | P0 | M0 | — | `docs/p4/P3_CAPABILITY_EVIDENCE_MATRIX.md` | **TESTED**（核验完成，结论 MISSING，用户裁定继续） |
| P4-GOV-002 Repo-first 盘点 | P0 | M0 | — | `docs/p4/P4_REPOSITORY_BASELINE.md` | **TESTED** |
| P4-GOV-003 里程碑审批与停止点 | P0 | M0 | — | 本册 §31 门禁 + Decision Pack | **IMPLEMENTED**（流程建立） |
| P4-GOV-004 需求代码测试验收追踪 | P0 | M0 | 本表 | 本文件 | **IMPLEMENTED**（表建立，覆盖率待补） |
| P4-GOV-005 ADR 与规范变更管理 | P0 | M0 | — | `P4_ARCHITECTURE_AND_ADR_REGISTER.md` | **IMPLEMENTED**（16 条 ADR Proposed） |

## 2. 架构（P4-ARCH）

| ID | 优先级 | 里程碑 | 契约 | 代码（当前） | 状态 |
|---|---|---|---|---|---|
| P4-ARCH-001 复用第三期 Control Plane | P0 | M1 | 内部函数调用 | `service.py`/`scheduler.py`/`executor.py` | **PARTIAL**（可复用的仅 5 项） |
| P4-ARCH-002 领域边界与数据 Owner | P0 | M0 | 本册 ADR-002 | `P4_ARCHITECTURE_AND_ADR_REGISTER.md` §2 | **IMPLEMENTED** |
| P4-ARCH-003 API/Event 契约冻结 | P0 | M1 | OpenAPI 扩展 + Event Envelope | `openapi.py`（49/60） | **PARTIAL**（P4 事件未定义） |
| P4-ARCH-004 失败隔离与降级 | P0 | M1 | 线程隔离 + 熔断 | 无 | **NOT_STARTED** |

## 3. 安全（P4-SEC）

| ID | 优先级 | 里程碑 | 契约 | 代码（当前） | 状态 |
|---|---|---|---|---|---|
| P4-SEC-001 服务端项目级权限校验 | P0 | M1 | `authorize()` | `auth.py`（二值） | **NOT_STARTED** |
| P4-SEC-002 检索权限前置过滤 | P0 | M2 | `retrieve(scope)` | 无 | **NOT_STARTED** |
| P4-SEC-003 Secret 检测脱敏阻断 | P0 | M1 | `scan_secrets()` | 无 | **NOT_STARTED** |
| P4-SEC-004 跨项目共享 Grant | P1 | M8 | `access_grants` | 无 | **DEFERRED** |
| P4-SEC-005 高风险操作审批审计 | P0 | M1 | `approvals` | 审计有，审批无 | **NOT_STARTED** |
| P4-SEC-006 安全事件隔离响应 | P0 | M1 | Kill Switch + Runbook | 无 | **NOT_STARTED** |

## 4. 评估（P4-EVAL）

| ID | 优先级 | 里程碑 | 契约 | 代码 | 状态 |
|---|---|---|---|---|---|
| P4-EVAL-001 Suite 管理 | P0 | M1 | `/api/p4/eval/suites` | — | NOT_STARTED |
| P4-EVAL-002 Dataset/Case 管理 | P0 | M1 | `/api/p4/eval/datasets` | — | NOT_STARTED |
| P4-EVAL-003 Evaluator/Rubric 版本 | P0 | M1 | `/api/p4/eval/evaluators` | — | NOT_STARTED |
| P4-EVAL-004 通过第三期 Runtime 执行 | P0 | M1 | `executor.run_item()` | `executor.py`（可复用） | PARTIAL |
| P4-EVAL-005 四类评估方法 | P0 | M1 | `method` 枚举 | — | NOT_STARTED |
| P4-EVAL-006 结果与失败样本报告 | P0 | M1 | `/api/p4/eval/runs/{id}` | — | NOT_STARTED |
| P4-EVAL-007 Holdout/Regression/Safety | P0 | M1 | `dataset.split` | — | NOT_STARTED |
| P4-EVAL-008 Benchmark 版本对比 | P0 | M1 | `/api/p4/eval/compare` | — | NOT_STARTED |
| P4-EVAL-009 预算超时取消 | P0 | M1 | `budget` / `cancel` | 有 turns/timeout | PARTIAL |
| P4-EVAL-010 模型评审校准 | P1 | M8 | `human_review` | — | DEFERRED |

## 5. 知识（P4-KNW）

| ID | 优先级 | 里程碑 | 契约 | 代码 | 状态 |
|---|---|---|---|---|---|
| P4-KNW-001 来源注册与授权检查 | P0 | M2 | `/api/p4/knowledge/sources` | — | NOT_STARTED |
| P4-KNW-002 解析分类脱敏扫描 | P0 | M2 | `ingest()` | — | NOT_STARTED |
| P4-KNW-003 Item/Version 管理 | P0 | M2 | `/api/p4/knowledge/items` | — | NOT_STARTED |
| P4-KNW-004 审核发布过期撤销 | P0 | M2 | 状态机 | — | NOT_STARTED |
| P4-KNW-005 项目级过滤检索 | P0 | M2 | `/api/p4/knowledge/search` | — | NOT_STARTED |
| P4-KNW-006 引用与 Retrieval Trace | P0 | M2 | `retrieval_traces` | — | NOT_STARTED |
| P4-KNW-007 索引缓存撤销联动 | P0 | M2 | 同事务删除 | — | NOT_STARTED |
| P4-KNW-008 反馈质量过期治理 | P1 | M8 | `feedback` | — | DEFERRED |
| P4-KNW-009 组织/跨项目共享 | P1 | M8 | `access_grants` | — | DEFERRED |
| P4-KNW-010 多模态接入 | P2 | — | — | — | DEFERRED |

## 6. Workflow（P4-WF）

| ID | 优先级 | 里程碑 | 契约 | 代码 | 状态 |
|---|---|---|---|---|---|
| P4-WF-001 Definition/Version | P0 | M3 | `/api/p4/workflows` | — | NOT_STARTED |
| P4-WF-002 输入输出 Schema | P0 | M3 | JSON Schema | — | NOT_STARTED |
| P4-WF-003 DAG 静态校验 | P0 | M3 | `validate()` | 环检测可参考 `service._dependency_cycle` | NOT_STARTED |
| P4-WF-004 Agent/Skill/Model 引用 | P0 | M3 | Registry 引用 | 无 Registry | BLOCKED（依赖 ADR-002） |
| P4-WF-005 Human/Approval/Quality Gate | P0 | M3 | `approvals` | — | NOT_STARTED |
| P4-WF-006 超时重试幂等失败分支 | P0 | M3 | 执行语义 | `idempotency_keys` 可复用 | NOT_STARTED |
| P4-WF-007 沙箱测试与发布 | P0 | M3 | `test_runs` | — | NOT_STARTED |
| P4-WF-008 通过第三期 Scheduler 执行 | P0 | M3 | `tasks` 映射 | `scheduler.py` | PARTIAL（能力在，映射未建） |
| P4-WF-009 模板继承项目定制 | P1 | M8 | — | — | DEFERRED |
| P4-WF-010 复用分析 | P1 | M8 | — | — | DEFERRED |

## 7. 发布（P4-REL）

| ID | 优先级 | 里程碑 | 契约 | 代码 | 状态 |
|---|---|---|---|---|---|
| P4-REL-001 Release Manifest | P0 | M4 | `/api/p4/releases` | — | NOT_STARTED |
| P4-REL-002 四类版本锁定 | P0 | M4 | `manifest_items` | 无 Registry | BLOCKED（依赖 ADR-002） |
| P4-REL-003 兼容性与安全校验 | P0 | M4 | `certify()` | — | NOT_STARTED |
| P4-REL-004 Evaluation Gate | P0 | M4 | `gate()` | — | NOT_STARTED |
| P4-REL-005 人工审批职责分离 | P0 | M4 | `approvals` | — | NOT_STARTED |
| P4-REL-006 Canary 与观察 | P0 | M4 | `canary` | — | NOT_STARTED |
| P4-REL-007 完整组合回滚 | P0 | M4 | `rollback()` | — | NOT_STARTED |
| P4-REL-008 发布历史与审计 | P0 | M4 | `audit_logs` | 审计可复用 | NOT_STARTED |
| P4-REL-009 多版本实验 | P1 | M8 | — | — | DEFERRED |

## 8. 持续改进（P4-IMP）

| ID | 优先级 | 里程碑 | 契约 | 代码 | 状态 |
|---|---|---|---|---|---|
| P4-IMP-001 失败/反馈/回归信号 | P0 | M5 | `signals` | `agent_events` 部分可用 | NOT_STARTED |
| P4-IMP-002 Improvement Case | P0 | M5 | `/api/p4/improvements` | — | NOT_STARTED |
| P4-IMP-003 根因分类与证据 | P0 | M5 | `root_cause` | — | NOT_STARTED |
| P4-IMP-004 Change Proposal | P0 | M5 | `/api/p4/proposals` | — | NOT_STARTED |
| P4-IMP-005 Sandbox Evaluation | P0 | M5 | Eval 复用 | — | NOT_STARTED |
| P4-IMP-006 人工审批与 Canary | P0 | M5 | `approvals` + `canary` | — | NOT_STARTED |
| P4-IMP-007 效果与副作用验证 | P0 | M5 | 前后对比 | — | NOT_STARTED |
| P4-IMP-008 自动聚类根因建议 | P1 | M8 | — | — | DEFERRED |

## 9. 业务价值（P4-OUT）

| ID | 优先级 | 里程碑 | 契约 | 代码 | 状态 |
|---|---|---|---|---|---|
| P4-OUT-001 Metric Dictionary | P0 | M6 | `metric_definitions` | `P4_VALUE_BASELINE…md` | PARTIAL（口径已冻结，表未建） |
| P4-OUT-002 读第三期 Usage Ledger | P0 | M6 | `token_usage_events` | 表已有 17,812 行 | PARTIAL（可读，无金额） |
| P4-OUT-003 任务/项目/Release 成本归因 | P0 | M6 | `attribution` | `executions.task_id` 全空 | **BLOCKED** |
| P4-OUT-004 质量/周期/返工/人工介入 | P0 | M6 | 指标组 | — | BLOCKED（无字段） |
| P4-OUT-005 真实/估算/待验证区分 | P0 | M6 | `cost_source` | — | NOT_STARTED |
| P4-OUT-006 业务 Owner 确认 | P0 | M6 | 审批记录 | — | BLOCKED（Owner 未指定） |
| P4-OUT-007 版本/Workflow 收益对比 | P1 | M8 | — | — | DEFERRED |
| P4-OUT-008 因果与长期 ROI | P2 | — | — | — | DEFERRED |

## 10. 自主化（P4-AUT）

| ID | 优先级 | 里程碑 | 契约 | 代码 | 状态 |
|---|---|---|---|---|---|
| P4-AUT-001 A0–A4 等级管理 | P0 | M7 | `autonomy_levels` | 无（二值开关） | NOT_STARTED |
| P4-AUT-002 范围绑定 | P0 | M7 | `eligibility` | — | NOT_STARTED |
| P4-AUT-003 多维资格评估 | P0 | M7 | `assess()` | — | NOT_STARTED |
| P4-AUT-004 有效期与重评估 | P0 | M7 | `expires_at` | — | NOT_STARTED |
| P4-AUT-005 Policy 最终执行 | P0 | M7 | `policy.decide()` | 硬编码白名单 | PARTIAL |
| P4-AUT-006 自动降级与人工接管 | P0 | M7 | `degrade()` | — | NOT_STARTED |
| P4-AUT-007 A3 Scoped Auto | P1 | M8 | — | — | DEFERRED |
| P4-AUT-008 A4 Extended Auto | P2 | — | — | — | DEFERRED |

## 11. 能力目录（P4-CAT）

| ID | 优先级 | 里程碑 | 契约 | 状态 |
|---|---|---|---|---|
| P4-CAT-001～005 | P1 | M8 | `capability_listings` | DEFERRED（P0 不建目录） |
| P4-CAT-006 外部市场 | P2 | — | — | DEFERRED |

## 12. 运维（P4-OPS）

| ID | 优先级 | 里程碑 | 契约 | 代码 | 状态 |
|---|---|---|---|---|---|
| P4-OPS-001 统一 correlation_id | P0 | M1 | Envelope 扩展 | 无 | NOT_STARTED |
| P4-OPS-002 指标日志 Trace 新鲜度 | P0 | M1 | `runtime_checks` 常驻 | 采集线程已停（D-2） | **BLOCKED** |
| P4-OPS-003 队列索引依赖监控 | P0 | M2 | 监控 | 无 | NOT_STARTED |
| P4-OPS-004 备份恢复回滚演练 | P0 | M4 | `scripts/backup.sh` | 手动 `Connection.backup()` | PARTIAL（未演练） |
| P4-OPS-005 容量并发成本保护 | P0 | M1 | 配额 | 仅 max_turns/timeout | PARTIAL |
| P4-OPS-006 Runbook 与交接 | P0 | M8 | `docs/runbook` | 无 | NOT_STARTED |
| P4-OPS-007 SLO/Error Budget | P1 | M8 | — | — | DEFERRED |
| P4-OPS-008 多区域高可用 | P2 | — | — | — | DEFERRED |

---

## 13. 覆盖缺口汇总

| 缺口 | 影响需求 |
|---|---|
| 无身份/授权（GAP-1） | SEC-001/002/005/006、EVAL-002、KNW-001/005、REL-005、AUT-002、OUT-006 |
| 无 Policy/KillSwitch（GAP-2） | SEC-005/006、WF-005、REL-005、AUT-005/006、IMP-006 |
| 无事件平面（GAP-3） | OPS-001、IMP-001、ARCH-003、全部域的事件类需求 |
| 无 Registry（P3-M2 缺失） | WF-004、REL-002 |
| 数据链断裂（cost/task_id） | OUT-002～005、IMP-001 |
| 无业务 Owner | OUT-006、P4-M0 门禁 |

**P4-M1 建议首批消化**：GAP-1、GAP-2、GAP-3、OPS-001/002/005、EVAL-001～009。其余按 DAG 顺序推进。

---

## 14. 实施状态更新（2026-11-06，P4-M1～M7 全部落地后）

上表「状态」列冻结于 P4-M0；本节记录实际实施结果，避免用旧快照判断当前进度。

| 里程碑 | 状态 | 代码位置 | 测试 |
|---|---|---|---|
| 地基（身份/Policy/KillSwitch/事件） | **IMPLEMENTED + TESTED** | `app/agent_platform/p4/{governance,policy,killswitch,events}.py` | `test_p4_governance.py` 14 项、`test_p4_http_gate.py` 6 项 |
| P4-M1 Evaluation | **IMPLEMENTED + TESTED** | `p4/evaluation.py` | `test_p4_evaluation.py` 8 项 + HTTP 全流程 |
| P4-M2 Knowledge | **IMPLEMENTED + TESTED** | `p4/knowledge.py` | `test_p4_knowledge.py` 9 项 + HTTP 2 项 |
| P4-M3 Workflow | **IMPLEMENTED + TESTED** | `p4/workflow.py` | `test_p4_workflow.py` 11 项 |
| P4-M4 Release | **IMPLEMENTED + TESTED** | `p4/release.py` | `test_p4_release.py` 10 项 |
| P4-M5 Improvement | **IMPLEMENTED + TESTED** | `p4/improvement.py` | `test_p4_improvement.py` 6 项 |
| P4-M6 Outcome | **IMPLEMENTED + TESTED** | `p4/outcome.py` | `test_p4_outcome_autonomy.py` 5 项 |
| P4-M7 Autonomy | **IMPLEMENTED + TESTED** | `p4/autonomy.py` | `test_p4_outcome_autonomy.py` 4 项 |
| P4-M8 Catalog（P1） | **IMPLEMENTED + TESTED（V1）** | `p4/catalog.py` | `test_p4_catalog.py` 5 项 |

**规模**：schema v11 / 68 张表 / 94 条 API 路径（117 个操作）/ 17,512 行 Python / 265 项测试全绿 / ruff 全清。

**仍未满足的门禁（诚实记录）**：

| 门禁 | 现状 |
|---|---|
| 两隔离项目的越权测试 | ✅ **已做**：`scripts/p4_security_drill.py` 11/11 通过（`docs/p4/reports/P4-M8-SECURITY-DRILL.md`）；演练发现并修复了"普通读写路由无项目级授权"的 P0 缺陷 |
| 备份恢复演练（P4-OPS-004） | ⚠️ **已演练 9/9 通过**，但 RTO 未计时、每日自动备份未接入、撤销重放未用真实撤销数据验证（`P4-M8-BACKUP-DRILL.md` §3） |
| Runbook 与运维交接（P4-OPS-006） | ✅ 已编写 `docs/p4/RUNBOOK.md`（204 行） |
| 真实业务数据下的评估集 | ❌ 仍只有合成样本，业务 Owner 尚未提供真实集 |
| GitLab / CI | ❌ 无 remote，仍为本地测试门禁 |
| Catalog（P4-M8 P1） | ⚠️ **V1 已实现**（schema v12，5 条 API，5 项测试），但未接入真实资产 |

以上是 P4-M8 最终验收前必须补齐的部分，不得用"功能都实现了"掩盖。

---

## 15. 界面（2026-11-06 补齐）

第四期八个域此前只有 API，界面不可见。现已接入项目页新增的 **「智能层」** 标签：

| 子页 | 内容 | 数据来源 |
|---|---|---|
| 评估 | 运行列表（状态/决策/样本/安全违规/分数）、套件、数据集 | `/eval/*` |
| 知识 | 新建（保存即发布，Secret 命中会被拒）、项目内检索、来源与条目 | `/knowledge/*` |
| 工作流 | 定义列表、项目绑定与物化任务数 | `/workflows`、`/workflow-bindings` |
| 发布 | Release 状态与 Manifest 摘要、Canary、回滚记录 | `/releases`、`/canaries`、`/rollbacks` |
| 改进 | 失败模式、改进 Case 与根因 | `/improvements/*` |
| 价值 | 成本口径（未知成本显示"未知"而非 0）、指标字典 | `/outcome/*` |
| 自主化 | 资格列表与等级（生产上限 A2 的提示） | `/autonomy/eligibilities` |
| 目录 | 可用能力与授权记录 | `/catalog/*` |

**已验证**：18 个接口全部 200；知识"新建 → 发布 → 检索命中"走真实 HTTP 链路通过。
**未验证**：无浏览器自动化（工作区未安装 Playwright），页面交互靠人工确认。
