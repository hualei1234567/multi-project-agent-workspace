# P3 试点项目与推进计划

> 日期：2026-11-15
> 依据：蓝图 §3「P3-M0」第 5 项

---

## 1. 试点项目

| 项 | 选择 | 理由 |
|---|---|---|
| 试点项目 | **material**（MaterialDev 材料开发平台） | 已有 34 个任务、7 个 Agent、真实执行记录、真实数据集 |
| Project Owner | hualei（`local-owner`） | 单一用户系统 |
| Platform Owner | hualei（`local-owner`） | 同上 |
| 备用项目 | ddp（DDP 具身智能数据闭环平台） | 跨项目委派需要第二个项目时使用 |

**不选 ddp 做主试点的理由**：material 有完整真实链路证据
（R3 真实 CLI 执行、R5 数据集、R6 Canary），验证成本最低。

---

## 2. 试点范围

| 维度 | 范围 |
|---|---|
| Runtime 设备 | 本机（Mac），LaunchAgent 托管 |
| 预算 | **当前无预算控制**（P3-M7 才建）。试点期按「观察成本、不强制」处理 |
| 工具范围 | Read/Glob/Grep/Edit/Write/TodoWrite/Skill；Bash 默认关 |
| 数据范围 | material 仓库 + `~/agent-team/material-team`；**不触碰生产环境** |
| 任务类型 | 文档生成、代码修改、测试编写；**不含部署、发布、数据删除** |

---

## 3. 推进阶段（三阶梯）

V4.0 要求 Shadow → Recommend → Assisted 逐级放开。当前平台**三个模式都不存在**，
所以这是 P3-M3 的交付内容，不是现在就能执行的路径。

### 阶段 1 · SHADOW（只算不做）

**目标**：验证「候选选择 + 评分 + Policy 结果」是否合理，零副作用。

| 检查项 | 通过标准 |
|---|---|
| 不产生任何 dispatch_item | 执行前后 `SELECT COUNT(*) FROM dispatch_items` 不变 |
| 不产生任何子进程 | 无 `claude` 子进程 |
| 决策可解释 | 每条候选有评分与理由 |
| 硬过滤生效 | 越权/无租约/依赖未满足的任务不出现在候选 |

**退出条件**：连续 3 次 SHADOW 的候选集与合作者人工判断一致。

### 阶段 2 · RECOMMEND（平台默认）

**目标**：作为默认模式，展示推荐但不派发。

| 检查项 | 通过标准 |
|---|---|
| 默认模式是 RECOMMEND | 新建计划 `mode='recommend'` |
| 不派发命令 | 无 dispatch_item 进入 queued |
| UI 展示匹配理由 | 每个候选可见「为什么推荐它」 |

**退出条件**：用户明确批准进入 Assisted。

### 阶段 3 · ASSISTED（需审批）

**目标**：人工确认 + 审批通过后才派发。

| 检查项 | 通过标准 |
|---|---|
| 未审批不派发 | 412 approval_required（**已实现**） |
| 审批一次性 | 同一审批单不能复用（**已实现**） |
| 摘要绑定 | 批准 A 不能执行 B（**已实现**） |
| 熔断生效 | 连续失败自动 paused |
| 人工接管 | 可取消正在跑的执行 |

**退出条件**：单项目 Assisted 稳定运行，再考虑跨项目（M5）。

---

## 4. 回退方案

| 场景 | 动作 | 影响 |
|---|---|---|
| 误派发 | `launchctl stop` 或 `/api/runtime/actions/kill_switch` | 秒级停止 |
| 执行器异常 | `AGWS_PLATFORM_EXECUTOR=off` 重启 | 派发全部拒绝 |
| 数据损坏 | 恢复 `data/platform.db.bak-*` | 回到备份点 |
| Schema 升级出错 | 迁移链 `--downgrade` | 回退到 v21 |
| Gateway 异常 | 停 Gateway 进程 | 降级为本地直连 |
| 平台整体故障 | LaunchAgent KeepAlive 自动重启（实测 10 秒） | 服务中断 <10s |

**已验证的回退能力**：
- `kill -9` → KeepAlive 10 秒拉起（新 PID 实测）
- 备份 `integrity_check=ok`，可还原
- 执行器 off → `global_enabled: false`（实测）

---

## 5. 停止派发与恢复

**立即停止**（任一即可）：

```bash
# 1. 全局开关
AGWS_PLATFORM_EXECUTOR=off  # 重启生效

# 2. Kill Switch（秒级，无需重启）
curl -X POST /api/runtime/actions/killswitch/engage

# 3. 停服务
launchctl stop com.hualei.agent-platform
```

**恢复**：

```bash
# 1. 确认 Kill Switch 已解除
curl /api/runtime/services

# 2. 确认执行器状态
curl /api/projects/material/dispatch/executor   # 看 global_enabled

# 3. 逐项确认审批规则仍在
sqlite3 data/platform.db "SELECT id, resource_type, action, requires_approval FROM policy_rules;"
```

---

## 6. 通知方案

| 事件 | 当前 | P3-M6 后 |
|---|---|---|
| 任务失败 | UI 轮询（10s） | 推送通知 |
| 服务异常 | UI 轮询 | 推送通知 |
| 待审批 | 无 | 推送通知 |
| 熔断触发 | 日志 | 推送通知 |

**现阶段**：靠 UI 轮询 + 日志。用户单人在本机，可接受。

---

## 7. 风险与责任

| 风险 | 等级 | 责任方 | 缓解 |
|---|---|---|---|
| 试点期间误派发 | 中 | hualei | 执行器默认 off + 审批闸门 |
| 消耗超预期 | 中 | hualei | 观察成本；P3-M7 建预算 |
| 改坏 material 仓库 | 中 | hualei | 工具白名单 + worktree 隔离 + 不自动 commit |
| 数据污染 | 低 | hualei | 平台库与业务库完全独立 |

---

## 8. 批准请求

按 V4.0 门禁，**P3-M0 五份交付物完成后必须暂停等待批准**。

批准后进入 Wave A（P3-M2 Registry + P3-M1 事件扩展，可并行）。

待确认事项（Q1–Q5）见 `P3_M0_BASELINE_AND_READINESS.md` §5，
不妨碍 Wave A 启动。
