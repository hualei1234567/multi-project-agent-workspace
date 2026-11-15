# P3-M0 · 基线与就绪评估

> 日期：2026-11-15
> 依据：`P3_V4_第三期缺口补齐实施蓝图.md` §3「P3-M0：基线冻结」
> 结论分类口径：**已验证事实 / 用户确认事实 / 设计建议 / 待确认事项**

---

## 1. 仓库与运行环境（已验证事实）

| 项 | 值 | 取证方式 |
|---|---|---|
| 仓库 | `haluei1234567/multi-project-agent-workspace`（公开） | `gh repo view` |
| 分支 | `main` | `git branch --show-current` |
| 基线 Commit | `382a0f4` | `git rev-parse --short HEAD` |
| Schema 版本 | v21 | `SELECT version FROM schema_version` |
| 表数量 | 76 | `acceptance.sh` 第 4 阶段 |
| API 操作 | 161（133 路径） | 同上 |
| 技术栈 | Python 3 stdlib `ThreadingHTTPServer` + SQLite(WAL) + 单文件零构建前端 | 代码 |
| 测试 | pytest 417 / UI 157 / acceptance 8 | `./scripts/test.sh` 等 |
| CI | GitHub Actions，`CI Gate` 为 Required Check | run `43690737301` |
| 运行时 | LaunchAgent `com.hualei.agent-platform`，8099，KeepAlive | `launchctl list` |

---

## 2. 各能力域现状（已验证事实）

### 2.1 Identity / RBAC / Project Scope

| 项 | 现状 | 证据 |
|---|---|---|
| 主体 | `principals` 表，2 条：`local-owner`(platform_owner) / `remote-token` | SQL 查询 |
| 角色 | 4 类：owner / operator / viewer / auditor | `governance.ROLE_PERMISSIONS` |
| 项目成员 | `project_members`：material ↔ local-owner(owner) | SQL 查询 |
| 默认策略 | Deny by Default（无 allow 规则时按 RBAC） | `policy.py:decide` |
| 鉴权 | 本机免 token；远端必须 Bearer；`/api/open` 仅回环 | `auth.py` 148 行 |
| 项目隔离 | slug 精确匹配 + `test_project_isolation` | 测试 |

### 2.2 Task / Execution / Worktree / Usage / Delivery

| 项 | 现状 | 证据 |
|---|---|---|
| Task | 项目内任务，`project_id NOT NULL`，状态机 todo→in_progress→review→done | `tasks` 表 |
| 跨项目依赖 | 已支持（M3-2），含环检测 | `scheduler._dependency_map` |
| Execution | `executions` 表，含 attempt 隔离（v21） | `executor._record_execution` |
| Worktree | `worktree_path` + 独立 worktree 支持 | `project_agents` |
| Usage | `token_usage_events`，四维 token | `ccusage` 集成 |
| Delivery | `deliveries` + review/validate/resubmit | `service.py` |

### 2.3 Event 来源

| 来源 | 表 | 投递 |
|---|---|---|
| 平台业务事件 | `event_outbox` / `event_inbox` | 后台线程 5s 轮询 |
| 项目事件 | `agent_events` | 只写不投 |
| 调度事件 | `dispatch_events` | 只写不投 |
| 审计 | `audit_logs` | 只写不投 |
| 运行时动作 | `runtime_action_runs` | 只写不投 |

**Event Envelope 已按 V6.0 §11.6 实现**（`p4/events.py`），含 correlation/causation。
缺：事件目录、按 event_type 的 schema 版本、退避重试、独立 DLQ 表与查询 API。

### 2.4 Scheduler / Executor 调用链

```
POST /dispatch/plan → scheduler.create_plan()   纯计算+落库，不起进程
POST /dispatch/items/{id}/run
  → _policy_guard(risk=R3)
  → _consume_approval()                          ← P0 新增
  → executor.run_item() → Popen(claude ...)
```

**第二条路径（未收敛）**：`team.py:288` 直接构造 CLI argv，不过 Policy。

### 2.5 CLI / Gateway

| 项 | 现状 |
|---|---|
| Claude CLI | 真实集成，`claude -p --output-format stream-json` |
| 工具白名单 | Read/Glob/Grep/Edit/Write/TodoWrite/Skill |
| Bash | 默认关闭；开启后仍受前缀白名单 + `--disallowedTools` 双重约束 |
| **Mac Gateway** | **不存在**。`team.py` 与 `executor.py` 各自构造 argv |

### 2.6 GitLab / CI

| 项 | 现状 |
|---|---|
| 平台仓库 | GitHub（用户已确认），Actions CI 通过 |
| GitLab 接入 | **无** |
| DDP / MaterialDev | GitHub（用户已确认，见记忆 `material-github-cli-configured`） |

### 2.7 PWA

| 资源 | 状态 |
|---|---|
| `/manifest.json` | **404** |
| `/sw.js` | **404** |
| `app/static/` | 不存在 |
| 响应式 | 已实现，移动视口冒烟 7/7 |

### 2.8 兼容性矩阵

| 组件 | 版本/约束 | 兼容性 |
|---|---|---|
| Python | 3.9+（`.venv-py39-bak` 为历史备份） | 代码用 `from __future__ import annotations` |
| SQLite | 3.35+（需要 `json_extract`、部分索引） | 实测可用 |
| Claude CLI | 需支持 `-p --output-format stream-json --disallowedTools` | R3 实测通过 |
| Node | 24.x（UI 冒烟 Playwright） | 实测通过 |
| Chrome | 系统 Chrome（Playwright 缓存已清） | 冒烟用 `executablePath` |

---

## 3. 用户确认事实

| 项 | 内容 | 来源 |
|---|---|---|
| 单用户系统 | 「该系统只我一个人使用」 | 会话 |
| 不做压力测试 | 「只测功能是否正常使用即可」 | 会话 |
| 平台仓库用 GitHub | 不在公司 GitLab | 会话 |
| 原始 UniRobot 只读 | 改动只用独立副本 | 记忆 |
| 验收 ACCEPTED | 「好，全部通过」（2026-11-15） | 会话 |

---

## 4. 设计建议（待批准）

| # | 建议 | 理由 |
|---|---|---|
| S1 | Registry 优先于其他里程碑 | 六层里它 30%，且被调度/Gateway/治理共同依赖 |
| S2 | Global Task 独立建模，不放宽 `tasks.project_id` | 放宽会破坏 35 项隔离测试 |
| S3 | Registry 与 `capability_listings` 语义分离 | 前者是「存在什么」，后者是「授权用什么」 |
| S4 | Gateway 只允许出站连接 | 消除公网任意入站控制的攻击面 |
| S5 | 事件平面扩展而非新建总线 | 已有 Outbox/Inbox 幂等，方向正确 |

---

## 5. 待确认事项（需用户决定）

| # | 事项 | 影响 |
|---|---|---|
| Q1 | 是否建 GitLab 接入？平台在 GitHub，公司 GitLab 是否确需对接 | 决定 P3-M7 范围 |
| Q2 | 是否要 Full Auto（无人值守连续调度） | V4.0 明确需独立立项 |
| Q3 | Registry 首期覆盖范围：只 Agent/Skill 还是含 Model/Runtime | 决定工作量 |
| Q4 | PWA 是否需要推送通知（要 HTTPS + 推送服务） | 决定 M6 复杂度 |
| Q5 | 跨项目委派是否真有多项目场景（当前 material + ddp） | 决定 M5 优先级 |

---

## 6. 就绪结论

**就绪度：可进入 P3-M1。**

- 地基（身份/策略/审计/熔断/事件/成本）**已验证可用**
- 安全止损已完成（审批闸门 + 执行器默认关闭）
- 无阻塞项；Q1–Q5 可在对应里程碑前回答，不妨碍 M1/M2 启动

**前置条件**：本文件及另外四份 P3-M0 交付物需用户批准后方可进入 P3-M1。
