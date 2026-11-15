# P3-M8 既有证据映射

> 把 2026-11-06「PASS WITH RISKS」与 R7 核对结果，逐项映射到 V4.0 的 P3-M8 清单。
> **PASS WITH RISKS 不等于 ACCEPTED**，本表只做映射，不做升格。

## V4.0 P3-M8 功能闭环（13 项）

| # | 清单项 | 已有证据 | 状态 |
|---|---|---|---|
| 1 | Global Task、DAG、依赖和失败分支 | 无 Global Task；项目内 DAG 有 | **MISSING** |
| 2 | Event 去重、重试、DLQ、重放 | `p4/events.py` 全有，无 API 暴露 | **PARTIAL** |
| 3 | Registry 和兼容矩阵 | 无 | **MISSING** |
| 4 | Shadow 不产生副作用 | 模式不存在 | **NOT RUN** |
| 5 | Recommend 不派发命令 | 模式不存在 | **NOT RUN** |
| 6 | Assisted 未审批不派发 | 规则表空，闸门失效 | **FAIL** |
| 7 | Mac Gateway 设备注册/认证/心跳/撤销 | 表在，无 Gateway 进程 | **PARTIAL** |
| 8 | CLI Adapter 状态回传/超时/取消/未知 | `executor.py` 有超时与状态；无未知态 | **PARTIAL** |
| 9 | 跨项目委派 + Context Package | 无 | **MISSING** |
| 10 | Android PWA 安装/审批/通知/撤销 | manifest/sw 404 | **MISSING** |
| 11 | GitLab/CI 数据可信 | GitHub CI 通，平台侧无接入 | **PARTIAL** |
| 12 | Usage 去重、成本分离、预算 | 成本分离 ✅；去重/预算 缺 | **PARTIAL** |
| 13 | 交付物和执行记录可追溯 | `executions` + `deliveries` | **VERIFIED** |

## V4.0 P3-M8 安全阻断（9 项）

| # | 清单项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 未授权跨项目读取 = 0 | ✅ | slug 精确匹配 + 隔离测试 |
| 2 | 未授权跨项目写入 = 0 | ✅ | Principal + project_members |
| 3 | **未审批高风险命令派发 = 0** | ❌ **有风险** | `policy_rules` 空 → 无审批直接派发 |
| 4 | 设备撤销后仍可执行 = 0 | ⚠️ 无法证明 | 无 Gateway，无从验证 |
| 5 | Secret 进普通日志/Context Package = 0 | ✅ | 脱敏 + 全量扫描无泄漏 |
| 6 | 手机端持有长期 Secret = 0 | ✅ | Bearer token + localStorage，非长期 Secret |
| 7 | Kill Switch 被绕过 = 0 | ✅ | fail-closed，CI 实测 503 |
| 8 | Approval 绕过 = 0 | ❌ **同 #3** | 同一根因 |
| 9 | 未授权目录访问 = 0 | ✅ | 路径白名单 |

**7 通过 / 2 不达标（同一根因：policy_rules 空）/ 1 无法验证。**

## V4.0 P3-M8 稳定性（8 项）

| # | 清单项 | 结果 |
|---|---|---|
| 1 | 并发、队列积压、限流压测 | 用户明确不测压力（单用户）→ **不适用** |
| 2 | GitLab API 限流/断连 | 无 GitLab 接入 → **NOT RUN** |
| 3 | Gateway 断网/重连 | 无 Gateway → **NOT RUN** |
| 4 | Scheduler/Event 故障注入 | 部分（Event 投递失败不退出）→ **PARTIAL** |
| 5 | 重复事件/重复命令 | Outbox 幂等 ✅ → **VERIFIED** |
| 6 | Kill Switch | ✅ **VERIFIED** |
| 7 | 备份恢复 | ✅ R6 实测 → **VERIFIED** |
| 8 | 版本回滚 | 迁移链有 `--downgrade`，未演练 → **PARTIAL** |

## V4.0 P3-M8 运维（11 项）

| 交付物 | 状态 |
|---|---|
| 试点报告 | ✅ `docs/repair/R3_交付报告.md` |
| 容量报告 | ⚠️ 不适用（单用户） |
| 安全报告 | ✅ `docs/repair/R7` + 本文件 |
| 故障演练报告 | ✅ 边界闭环报告（kill -9 / 备份还原） |
| 发布说明 | ✅ `docs/repair/R6_遗留闭环.md` |
| **回滚手册** | ❌ MISSING |
| **运维 Runbook** | ❌ MISSING |
| 未完成风险有 Owner | ⚠️ 未逐项指定 |
| P0 全部通过 | ❌ #3/#8 不达标 |
| 重大安全问题为 0 | ❌ 审批闸门失效 |
| 正式验收 | ✅ 用户「好，全部通过」（但基于不完整的边界说明） |

## 结论

**P3-M8 现在是 `BLOCKED`，不是 `PARTIAL`。** 阻塞原因单一且明确：
`policy_rules` 为空导致审批闸门形同虚设。

这是**唯一一个必须立刻修的安全问题**，且修复成本极低（初始化几条规则）。
修完后 #3/#8 可转为通过，P0 缺口只剩 Global Task / Registry / PWA / 委派
等「未建功能」，属于范围问题而非安全问题。
