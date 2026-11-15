# P3 架构与 ADR

> 日期：2026-11-15
> 依据：蓝图 §3「P3-M0」第 2 项
> 原则：Repo-first / Reuse-first / Contract-first / Evidence-first

---

## 1. 现状架构（已验证）

```
┌─────────────────────────────────────────────────────────────┐
│  agents_dashboard.html  单文件零构建，hash 路由             │
│   平台级 7 视图 · 项目级 9 Tab · 6 档断点                    │
└────────────────────────┬────────────────────────────────────┘
                         │ JSON over HTTP（本机免 token）
┌────────────────────────▼────────────────────────────────────┐
│  agents_dashboard.py  ThreadingHTTPServer（backlog 128）     │
│   dispatch() 收敛路由 · _policy_guard · _consume_approval    │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  agent_platform/                                             │
│   service.py(3627) 核心 CRUD/状态机                          │
│   sources.py(1280) 采集：TASK_BOARD/status/worktree/transcript│
│   scheduler.py(395)  排程（纯计算，不起进程）                │
│   executor.py(662)   执行（唯一 Popen 模块）                 │
│   executor_policy.py(369) 白名单/拒令                        │
│   approvals.py(新)   审批闭环                                │
│   cost.py / runtime.py / workspace.py / resources.py         │
│   p4/  AgentOps 五域 + 治理                                  │
│     workflow(718) evaluation(480) knowledge(479)             │
│     release(496)  improvement(325) autonomy(284)             │
│     catalog(252)  governance(236) events(150)                │
│     policy(174)   outcome(166)     killswitch(127)           │
└────────────────────────┬────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────┐
│  SQLite WAL · schema v21 · 76 表 · 迁移链 v17→v21            │
└─────────────────────────────────────────────────────────────┘
```

**外部依赖（出站）**：Claude CLI、ccusage、git、launchctl、ps、系统 Chrome。

---

## 2. ADR 清单

### ADR-P3-001 · Event Plane 复用而非新建

**状态**：已采纳（P0 期间确认）

**决策**：在 `p4/events.py` 的 Outbox/Inbox 上扩展，不建第二条总线。

**理由**：
- 已有幂等（`event_inbox (event_id, consumer)`）、DLQ（`status='failed'`）、重放（`replay()`）
- 四条既有事件流（agent_events / dispatch_events / audit_logs / runtime_action_runs）保留，通过 Adapter 汇入
- 新建总线会重蹈「两套真相」的覆辙

**影响**：需补事件目录表、按 event_type 的 schema 版本、退避重试、DLQ 查询 API。

**回滚**：新增字段可空，旧代码忽略即可。

---

### ADR-P3-002 · Scheduler Facade 与旧入口兼容

**状态**：**待实施**（P3-M3）

**决策**：新增统一 Facade，`team.py` 与 `executor.py` 都经它进入；旧入口降级为薄封装。

**理由**：当前有两条独立构造 CLI argv 的路径，`team.py` 绕过 Policy——
这正是蓝图 §2 点名的「旧入口绕过 Facade」。

**约束**：Facade 不改上层签名；`team.py` 对调用方接口保持兼容。

**回滚**：Facade 可用 `mode` 开关，`legacy` 模式直通旧路径。

---

### ADR-P3-003 · Gateway 与 CLI Adapter 边界

**状态**：**已落地（平台侧）**（P3-M4，schema v26）

**决策**：
- **CLI Adapter**：负责 argv 构造、版本兼容、状态映射（CREATED…UNKNOWN）
- **Mac Gateway**：独立进程，设备认证/心跳/撤销，**只允许出站连接**

**落地范围（严格区分）**：

| 项 | 状态 | 落点 |
|---|---|---|
| argv 收敛（不重复实现白名单，全部委托 `executor_policy`） | ✅ 已实现 | `cli_adapter.py` |
| 状态映射 CREATED…UNKNOWN（未知一律 UNKNOWN） | ✅ 已实现 | `cli_adapter.map_status` |
| CLI 版本读取（拿不到返回 None） | ✅ 已实现 | `cli_adapter.cli_version` |
| 设备命令队列：拉取 / 认领 / 终态 / 过期 / 撤销闸门 | ✅ 已实现 | `gateway.py` |
| 设备凭据摘要 + 撤销双写 `registry_runtimes` | ✅ 已实现 | `gateway_devices`（v26） |
| **Gateway 进程本体**（长轮询循环、开机自启、凭据落盘） | ⛔ **未实现** | — |
| **Gateway 相关 HTTP 接口**（平台侧调用面） | ⛔ **未实现** | 本阶段只做库函数，未加路由 |

出站约束如何被保证：平台侧**只提供查询/认领/回报函数**，没有监听端口；
Gateway 进程要做的全部是「主动连平台」。本轮未实现进程本体，因此也没有引入入站面。

**理由**：把「怎么起进程」与「在哪台机器起」解耦；出站连接消除公网入站攻击面。

**回滚**：Gateway 未部署时，Adapter 可本地直连（等同现状）。`gateway.py` 不被调用即无效，
v26 只加表加列，回滚无数据副作用。

---

### ADR-P3-004 · Git Provider Adapter

**状态**：**待确认**（Q1）

**决策**：按项目配置 provider，不写死单一平台。
- 平台仓库 → GitHub（用户已确认）
- MaterialDev → GitHub（用户已确认）
- 公司 GitLab → **是否接入待确认**

**理由**：V4.0 要求「不把全平台写死为 GitLab」，同时用户明确平台仓库不迁 GitLab。

---

### ADR-P3-005 · PWA 静态资源与 Service Worker scope

**状态**：**已实施**（2026-11-15，P3-M6）

**决策**：
- `/manifest.webmanifest` + `/sw.js` + `/offline.html` 由 `agents_dashboard.py` 的
  `do_GET` 分支提供，资源在 `app/agent_platform/pwa.py` 里生成（纯 stdlib，无构建步骤）
- 图标不引用外部文件：`/icons/icon-{192,512}.png` 与 `maskable-512` 由
  `pwa.py` 现场用 zlib+struct 生成真实 PNG，`theme_color` 直接读页面深色主题的 `--bg`
- SW scope = `/`，缓存策略：静态资源 Cache-First，API 一律 Network-Only（不缓存业务数据）
- 离线降级页 `offline.html`：只回答「没网」；服务端 404/500 原样返回，不冒充离线
- PWA 外壳路径进 `auth.PUBLIC_PREFIXES/PUBLIC_PATHS` 白名单：SW 的 `install`
  预缓存不带 `Authorization` 头，否则远端 HTTPS 下预缓存整批 401

**理由**：API 缓存会导致「离线看到陈旧数据」，比白屏更危险。
V4.0 要求「离线或未送达命令不得显示成功」——Network-Only 是最直接的保证。

**回滚**：删 SW 注册即可，无数据副作用。

**已知限制**：远端开启 `AGWS_PLATFORM_AUTH=on` 时令牌存在 localStorage，
SW 读不到，`/` 的外壳预缓存会 401；离线时仍能显示 offline.html。

---

### ADR-P3-006 · Schema / API / Migration 兼容策略

**状态**：已采纳

**决策**：
- 只加表/加列，不改既有列语义
- 迁移链单向递增，绝不倒写版本号
- 新增 API 一律先在 `openapi.py` 登记，`test_route_contract.py` 强制同步
- 契约冻结后才可并行开发

**理由**：现有 417 测试 + 161 API 操作，任何破坏性变更成本极高。

---

## 3. 契约冻结点

进入 Wave A 之前必须冻结：

| 契约 | 内容 | 冻结时机 |
|---|---|---|
| `P3_EVENT_SCHEMA_v1` | event_type 目录、必填字段、版本策略 | M1 开工前 |
| `P3_REGISTRY_SCHEMA_v1` | 资源身份唯一键、版本规则、兼容判定 | M2 开工前 |
| `P3_DELEGATION_STATE_MACHINE_v1` | 状态与转移条件 | M5 开工前 |

---

## 4. 明确不做的事

- 不引入 Web 框架（保持 stdlib）
- 不引入 ORM（保持手写 SQL）
- 不拆前端（保持单文件零构建）
- 不改 MaterialDev / DDP 业务代码
- 不推倒 M3-x 既有实现
