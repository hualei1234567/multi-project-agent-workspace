# P3 里程碑 DAG 与 Worktree 计划

> 日期：2026-11-15
> 依据：蓝图 §3「P3-M0」第 4 项 + §4 实施依赖顺序

---

## 1. 里程碑依赖 DAG

```
                    ┌─────────────────────────┐
                    │  P0 安全止损（已完成）   │
                    │  审批闸门 + 执行器关闭   │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  P3-M0 基线冻结          │
                    │  五份交付物 ← 本文件     │
                    └───────────┬─────────────┘
                                │ 用户批准
              ┌─────────────────┴─────────────────┐
              │                                   │
   ┌──────────▼──────────┐            ┌───────────▼──────────┐
   │  P3-M2 Registry     │            │  P3-M1 事件平面扩展  │
   │  Agent/Skill/Model  │            │  目录/DLQ/退避/新鲜度│
   │  /Runtime + 兼容矩阵│            │                      │
   └──────────┬──────────┘            └───────────┬──────────┘
              │  两者可并行（Wave A）             │
              └─────────────────┬─────────────────┘
                    ┌───────────▼─────────────┐
                    │  P3-M3 三模式 + Facade   │
                    │  SHADOW/RECOMMEND/ASSISTED│
                    └───────────┬─────────────┘
                                │
              ┌─────────────────┴─────────────────┐
   ┌──────────▼──────────┐            ┌───────────▼──────────┐
   │  P3-M5 Global Task  │            │  P3-M4 Mac Gateway   │
   │  + 委派 + Context   │            │  CLI Adapter 收敛    │
   └──────────┬──────────┘            └───────────┬──────────┘
              │           Wave B/C 可并行          │
              └─────────────────┬─────────────────┘
                    ┌───────────▼─────────────┐
                    │  P3-M6 Android PWA       │
                    │  manifest/SW/通知/审批   │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  P3-M7 GitLab/预算/效能  │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  P3-M8 综合验收          │
                    │  + Runbook + 回滚手册    │
                    └─────────────────────────┘
```

**关键路径**：P0 → M0 → **M2 Registry** → M3 → M4 → M8。
Registry 是关键路径节点（被 M3/M4/M5 共同依赖）。

---

## 2. 任务清单与文件 Owner

> 规则：同一文件任一时刻只有一个 Owner。并行任务必须用独立 Worktree。

### Wave 0（已完成 / 本文件）

| Task ID | 内容 | 文件 | Owner | 状态 |
|---|---|---|---|---|
| P0-1 | 执行器默认关闭 | `scripts/start.sh` | platform | ✅ |
| P0-2 | 审批规则初始化 | `scripts/seed-policy-rules.py` | platform | ✅ |
| P0-3 | 审批服务 | `app/agent_platform/approvals.py` | platform | ✅ |
| P0-4 | 审批 API + 闸门 | `app/agents_dashboard.py`、`openapi.py` | platform | ✅ |
| M0-D1~D5 | 五份交付物 | `docs/phase3/*.md` | platform | ✅ 待批 |

### Wave A（可并行，M0 批准后）

| Task ID | 内容 | 主文件 | 依赖 |
|---|---|---|---|
| M2-1 | Registry 表结构 + 迁移 v22 | `migrate.py`、新 `registry.py` | 契约冻结 |
| M2-2 | Agent Role / Skill / Model / Runtime 定义 | `registry.py` | M2-1 |
| M2-3 | 兼容矩阵 + 版本锁定 | `registry.py` | M2-2 |
| M2-4 | Registry API + 授权联动 | `agents_dashboard.py`、`openapi.py` | M2-2 |
| M1-1 | 事件目录 + schema 版本 | `p4/events.py` | 契约冻结 |
| M1-2 | 退避重试 + DLQ 查询 | `p4/events.py` | M1-1 |
| M1-3 | 全局总览数据新鲜度 | `agents_dashboard.py` | M1-1 |

**冲突点**：M1-3 与 M2-4 都改 `agents_dashboard.py` → 必须串行，或 M2-4 排在 M1-3 之后。

### Wave B

| Task ID | 内容 | 主文件 | 依赖 |
|---|---|---|---|
| M3-1 | Scheduler Facade | 新 `facade.py` | M2 |
| M3-2 | SHADOW / RECOMMEND / ASSISTED | `scheduler.py`、`facade.py` | M3-1 |
| M3-3 | 决策记录表 + 回放 | `migrate.py`、`facade.py` | M3-1 |
| M3-4 | `team.py` 收进 Facade | `team.py` | M3-1 |
| M4-1 | CLI Adapter | 新 `cli_adapter.py` | M2 |
| M4-2 | Mac Gateway 进程 | 新 `gateway.py` | M4-1 |

### Wave C

| Task ID | 内容 | 主文件 | 依赖 |
|---|---|---|---|
| M5-1 | Global Task + link 表 | `migrate.py`、新 `global_task.py` | M3 |
| M5-2 | 委派状态机 | `delegation.py` | M5-1 |
| M5-3 | Context Package | `context_pkg.py` | M5-2 |
| M6-1 | manifest + SW 静态路由 | `agents_dashboard.py` | 无 |
| M6-2 | 移动审批 + 设备撤销 | `agents_dashboard.html` | M6-1、M4 |

### Wave D

| Task ID | 内容 | 依赖 |
|---|---|---|
| M7-1 | 预算表 + 预警 + 暂停 | M3 |
| M7-2 | 效能指标 | M7-1 |
| M7-3 | GitLab 接入（**待 Q1 确认**） | M1 |
| M8-1 | Runbook + 回滚手册 | 全部 |
| M8-2 | 综合验收 | 全部 |

---

## 3. 契约冻结点

| 冻结点 | 冻结内容 | 冻结时机 | 之后允许的变更 |
|---|---|---|---|
| **CF-1** | Event Schema v1（event_type 目录、必填字段） | M1 开工前 | 只能加字段，不能改语义 |
| **CF-2** | Registry Schema v1（唯一键、版本规则） | M2 开工前 | 同上 |
| **CF-3** | Delegation 状态机 v1 | M5 开工前 | 同上 |
| **CF-4** | 三模式语义（SHADOW/RECOMMEND/ASSISTED） | M3 开工前 | 不改行为定义 |

**冻结后**：新增 API 一律先在 `openapi.py` 登记，`test_route_contract.py` 强制同步。

---

## 4. 验收门禁

每个里程碑必须全部通过才可进入下一个：

| 门禁 | 要求 |
|---|---|
| G1 | `./scripts/test.sh` 全绿（pytest + ruff） |
| G2 | `./scripts/acceptance.sh` 8/8 |
| G3 | UI 冒烟全通过 |
| G4 | 新增能力有对应测试用例 |
| G5 | 安全相关变更必须有《安全证据》（实测输出，不是断言） |
| G6 | 阶段报告提交并**暂停等待用户批准** |

---

## 5. Worktree 策略

| 场景 | 策略 |
|---|---|
| 单里程碑线性推进 | 直接在 `main`（本平台单用户） |
| 跨里程碑并行 | 独立 worktree + 明确文件 Owner |
| 公共 Schema/API 变更 | **必须先冻结契约**，再并行 |
| 冲突检测 | `git status` 干净 + 合并前查上游 |

**平台与业务仓库隔离**：平台改动只在 `Multi-Project-Agent-Workspace`；
`MaterialDev` / `DataPlatform` 的改动属于项目任务，不在本仓库。
