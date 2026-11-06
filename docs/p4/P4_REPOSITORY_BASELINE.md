# P4 仓库与运行基线

> 交付物：P4-M0 第 2/10 份
> 依据：V6.0 §24.1（P4-M0 必须测量）、§28.1（Repo-first 审计产物）
> 测量时间：2026-11-06，只读命令，未修改任何状态

---

## 0. 一句话基线

单进程 Python 标准库 HTTP 服务（13,272 行，零第三方运行依赖）＋ 单文件前端（4,468 行，零构建）＋ 单 SQLite 库（25 表 / 9.5 MB / schema v3），192 项测试全绿，仅监听 127.0.0.1:8099，纳管 2 个项目、14 个 Agent 角色、33 个任务。

---

## 1. 仓库与版本

| 项 | 值 | 证据 |
|---|---|---|
| 仓库根 | `<HOME>/Multi-Project-Agent-Workspace` | — |
| 当前分支 | `main` | `git branch --show-current` |
| HEAD | `aeed6d6 docs: 三期验收报告与审计闭环（验收补齐）` | `git log --oneline -1` |
| 远端 | **无**（`git remote -v` 为空） | V6.0 §0.1 声明托管在公司 GitLab，**与仓库现状不一致** |
| 未提交改动 | 仅 `?? docs/p4/`（本次 P4-M0 产出） | `git status --short` |
| `.venv-py39-bak` | 历史备份，不得删除 | CLAUDE.md |

**差距**：无 GitLab remote，V6.0 §27（CI/CD）与 §28.4（Worktree 并行）当前无落地载体。第四期不得假设存在 CI，除非 P4-M0 之后显式补齐。

---

## 2. 技术栈

| 层 | 实现 | 约束 |
|---|---|---|
| 语言 | Python 3.11.17 | `.venv` |
| HTTP | `http.server.ThreadingHTTPServer`，手写路由 | 无框架、无 ASGI、无 WSGI |
| 持久化 | SQLite（WAL，`isolation_level=None`，显式 `BEGIN IMMEDIATE`） | 无 ORM，手写 SQL |
| 前端 | 单 HTML 文件，原生 JS，hash 路由，innerHTML 重渲染 | 无构建、无 npm 运行依赖 |
| 契约 | OpenAPI 3.1 由 `openapi.py` 手工维护 + 测试强制同步 | 双写：`API_PATHS`(60) 与 `_paths()`(60) 必须一致 |
| 进程托管 | launchd `com.agentplatform.dashboard.plist` | Mac 本机 |
| 外部依赖 | 无运行时第三方包；测试用 pytest / ruff | — |

---

## 3. 目录与代码规模

```
app/agent_platform/   领域层 5,848 行（16 个模块）
app/agents_dashboard.py    HTTP 层 1,958 行
app/agents_dashboard.html  UI 4,468 行（单文件）
app/tests/                 17 个 pytest 文件 + 2 个 mjs 冒烟
config/projects.json       纳管清单（ddp / material）
data/platform.db           平台库
docs/                      方案、审计、验收、p4/
scripts/                   start.sh / status.sh / test.sh
```

| 模块 | 行数 | 职责 |
|---|---:|---|
| `service.py` | 2,346 | 项目/任务/交付/文档/团队模板/审计/统计 |
| `sources.py` | 945 | TASK_BOARD 解析、status 自报、worktree 扫描、transcript 游标、ccusage |
| `migrate.py` | 609 | schema 版本链 v1→v2→v3、热备、回滚 |
| `openapi.py` | 482 | 契约定义与校验 |
| `executor.py` | 444 | 唯一允许起外部进程的模块 |
| `scheduler.py` | 395 | 纯计算排程 |
| `runtime.py` | 382 | 6 种探针采集 + 受控动作 |
| `resources.py` | 250 | 共享资源与占用 |
| `executor_policy.py` | 234 | 准入判定（不起进程） |
| `store.py` | 202 | 连接、迁移入口、write_tx、JSON 编码 |
| `templates.py` | 187 | 团队模板 |
| `auth.py` | 138 | 令牌鉴权 |
| `state_cache.py` | 135 | `/api/state` 单飞快照 |
| `config.py` | 90 | 路径与环境 |
| `__init__.py` | 7 | — |

---

## 4. 启动、自检与测试

| 动作 | 命令 | 当前结果 |
|---|---|---|
| 启动 | `scripts/start.sh [端口] [--background]`，默认 8099，仅本机 | 运行中 |
| 执行器开关 | `DDP_AGENT_PLATFORM_EXECUTOR=on`（默认 off） | off |
| 鉴权开关 | `DDP_AGENT_PLATFORM_AUTH`（默认 auto，实际强制远端校验） | 本机免令牌 |
| 自检 | `scripts/status.sh` | — |
| 测试 | `scripts/test.sh` = py_compile + pytest + ruff | — |
| pytest | `.venv/bin/python -m pytest app/tests -q` | **192 passed in 18.55s** |
| UI 冒烟 | `node app/tests/ui_smoke.mjs` | 65 项（历史） |
| 移动端冒烟 | `node app/tests/mobile_smoke.mjs` | 7 项（历史） |

**测试覆盖分布**（按文件）：`test_agent_platform` / `test_phase2_service` / `test_phase2_http` / `test_phase2_isolation` / `test_migrate_v3` / `test_write_tx` / `test_scheduler` / `test_executor` / `test_executor_policy` / `test_runtime` / `test_cross_project` / `test_auth` / `test_audit_coverage` / `test_openapi` / `test_state_cache` + 2 个 mjs。

---

## 5. 数据库基线

| 项 | 值 |
|---|---|
| 文件 | `data/platform.db`（9,977,856 B ≈ 9.5 MB，含 -wal/-shm） |
| schema_version | **3**（applied 2026-11-05T11:38:45Z） |
| 表数 | 25 |
| 显式索引 | 16 |
| 备份 | `platform.db.bak-20261105T101420…`、`…113845…` 两份 |

逐表行数（2026-11-06 实测）：

| 分组 | 表 | 行数 |
|---|---|---|
| 项目 | `agent_projects` / `project_agents` / `agent_team_templates` | 2 / 14 / 2 |
| 任务 | `tasks` / `task_dependencies` | **33** / **0** |
| 交付执行 | `deliveries` / `executions` | 17 / 44 |
| 事件审计 | `agent_events` / `audit_logs` | 178 / **6** |
| 用量 | `token_usage_events` | **17,812**（覆盖 2026-10-28 → 2026-11-06，2 个项目） |
| 调度 | `dispatch_plans` / `dispatch_items` / `dispatch_logs` / `dispatch_events` | 2 / **1** / 178 / 10 |
| 跨项目 | `shared_resources` / `resource_claims` | 0 / 0 |
| 运行时 | `runtime_services` / `runtime_checks` / `runtime_actions` / `runtime_action_runs` | 5 / 5 / 0 / 1 |
| 其它 | `project_documents` / `project_favorites` / `project_recent_visits` / `idempotency_keys` | 0 / 1 / 2 / 0 |

**判读**：真正的业务体量只有三项——33 个任务、44 条执行、17,812 条 token 记录。第三期新增的调度与跨项目能力合计只有 3 条记录。

---

## 6. API 基线

| 项 | 值 |
|---|---|
| 唯一路径 | 49 |
| 操作数 | 60（GET 28 / POST 24 / PATCH 3 / DELETE 4 / PUT 1） |
| 契约覆盖 | 49/49，`test_openapi.py` 强制 |
| 鉴权 | 5 个动词全部过 `_authorized`；`/api/open` 仅回环；`/healthz` 已被 `auth.PUBLIC_PATHS` 白名单但**服务端未实现**（D-6） |

---

## 7. 前端基线

| 项 | 值 |
|---|---|
| 文件 | 单文件 4,468 行，零构建 |
| 路由 | hash 路由；平台级 6 视图 + 项目级 7～8 Tab（含调度） |
| 轮询 | 15s（列表）/ 10s（详情），trace 抽屉 1.5s |
| 响应式 | 6 档断点 + 移动端侧栏抽屉 |
| 状态管理 | 无框架，innerHTML 重渲染 |
| 可测试性 | Playwright 冒烟（`ui_smoke.mjs` 65 项、`mobile_smoke.mjs` 7 项、`file_viewer_smoke.mjs` 14 项） |

---

## 8. 运行态基线

| 服务 | 端口 | 状态 | 证据 |
|---|---|---|---|
| 管理平台 | 8099 | 运行中，**仅 127.0.0.1** | `lsof` PID 71154 |
| 采集：runtime_checks | — | **最新 2026-11-05T11:41，已停更** | D-2 |
| launchd | — | `com.agentplatform.dashboard` 已加载；同目录另有 `com.ddp.gitlab-runner-2`、`com.ddp.tailscale-watchdog` | `~/Library/LaunchAgents/` |
| Tailscale | — | stopped | 三期审计 §3.4 |
| DDP 管理平台 8080 | 8080 | 本机不可达 | 三期审计 §3.4 |

---

## 9. V6.0 §24.1 要求测量的 12 项

| 要求 | 当前值 | 状态 |
|---|---|---|
| 用户数 | **0**（无身份体系，只有 loopback/令牌二值） | BLOCKED |
| 项目数 | 2（ddp、material） | VERIFIED |
| Agent / Runtime 并发 | 每项目 1（`max_concurrency` 默认）；实际历史最大并发 1 | PARTIAL |
| Task / Execution / Event 量 | 33 任务 / 44 执行 / 178 事件 / 17,812 usage | VERIFIED |
| Evaluation Dataset 样本量 | 不存在 | MISSING |
| 单次评估耗时与 Token | 无评估体系；单次执行 23s / $0.1715（ACC-001） | MISSING |
| Knowledge 文档规模与索引耗时 | 不存在（`project_documents`=0） | MISSING |
| Retrieval 延迟 | 不存在 | MISSING |
| Workflow 节点数与并发 | 不存在 | MISSING |
| Release / Canary 规模 | 不存在 | MISSING |
| 数据库容量与慢查询 | 9.5 MB；无慢查询监控，无 query log | PARTIAL |
| 外部 Provider 限流 | 经 TeamoRouter → `api.teamorouter.com`；无限流记录 | BLOCKED |
| 现有故障恢复时间 | 无记录；launchd KeepAlive 重启未计时 | NOT RUN |

**结论：12 项中仅 3 项有可信数值。** 这是 P4-M0 价值基线只能"部分冻结"的原因，也是 P4-M1 必须先建可观测性的原因。

---

## 10. 配置与凭据

| 项 | 位置 | 状态 |
|---|---|---|
| 纳管清单 | `config/projects.json`（ddp / material，repo_root + team_dir） | 有效 |
| 平台库路径 | 默认 `data/platform.db`，`DDP_AGENT_PLATFORM_DB` 可覆盖 | 有效 |
| 执行器令牌文件 | `data/token` | **不存在**（D-7） |
| API Key / 中转站凭据 | 本机环境变量，不落库 | 未读取、未打印 |
| `.claude/settings.local.json` | 工作区级权限配置 | 存在，本期不改 |

---

## 11. 基线冻结声明

以下数值在本文件中冻结，作为 P4-M1 起所有对比的参照：

```
HEAD                 aeed6d6
schema_version       3
tables               25
api_paths            49 / 60 operations
pytest               192 passed
tasks                33
executions           44
dispatch_items       1（succeeded）
token_usage_events   17812
db_size              9,977,856 B
listen               127.0.0.1:8099
```
