# P3 能力证据矩阵

> 交付物：P4-M0 第 1/10 份
> 依据：《多项目 Agent 管理平台全版本完整总文档 V1.0–V6.0》附录 A13（V6.0）§30.2、附录 A10（V4.0）§17/§18
> 状态词表：VERIFIED / PARTIAL / UNVERIFIED / MISSING / BLOCKED（V6.0 §0.4）
> 复核日期：2026-11-06
> 证据方式：只读读取仓库、数据库、运行中进程；未修改任何代码、库、配置

---

## 0. 结论

1. **V4.0 定义的 P3-M1–P3-M8，本仓库没有按该定义建设。** 实际交付的是自行推导的 M3-0～M3-4 五个里程碑，与 V4.0 的里程碑编号相同但内容不同。
2. **P3-M8 无正式验收报告、无验收人签署、无交付物**，状态为 MISSING。
3. 已交付部分（调度、运行时、鉴权、移动端适配）与 V4.0 的 P3-M3/P3-M4/P3-M6 有部分交集，交集部分状态为 PARTIAL。
4. **用户已于 2026-11-06 裁定**：以本汇编为唯一依据，取消此前的门禁争议，直接进入 P4-M0 建设。本文件据此把事实与裁定分开记录（见 §5），事实不因裁定而改写。
5. 第四期不得假设存在 Event Plane、Registry、Gateway、RBAC、Kill Switch 等 V4.0 能力；**这些在本仓库是"首次建设"，不是"重复建设"**（见 §6 的归属裁定）。

---

## 1. 证据来源

| 编号 | 证据 | 位置 |
|---|---|---|
| E-01 | 仓库主干提交 | `git log --oneline -5` → `aeed6d6`（HEAD=main） |
| E-02 | 工作区状态 | `git status --short` → 仅 `?? docs/p4/` |
| E-03 | 代码规模 | `wc -l app/**/*.py app/agents_dashboard.html` → 13,272 行 |
| E-04 | 数据库 | `data/platform.db`，schema_version=3，25 张表，9.5 MB |
| E-05 | 测试 | `.venv/bin/python -m pytest app/tests -q` → **192 passed in 18.55s** |
| E-06 | API 契约 | `openapi.build_openapi()` → 49 paths / 60 operations |
| E-07 | 运行态 | `lsof -iTCP:8099` → PID 71154，仅 `127.0.0.1` |
| E-08 | 审计链 | `audit_logs` 6 行，最近 4 条为 2026-11-06 调度执行 |
| E-09 | 自建验收文档 | `docs/三期验收审计报告.md`、`docs/三期验收报告.md`（2026-11-06，自定范围） |

---

## 2. V4.0 P3-M0–M8 逐项核验

状态判定只认代码 + 测试 + 运行/验收证据；规划文档、页面截图、Agent 自述不作为证据。

| 里程碑 | V4.0 定义 | 本仓库实际 | 证据 | 状态 |
|---|---|---|---|---|
| P3-M0 | 三期准入、复用裁定、DAG、试点建议 | 无对应交付物；直接进入自定 M3-0 | 无文档、无记录 | **MISSING** |
| P3-M1 | 统一事件平面 + 全局指挥中心 | `agent_events`/`dispatch_events`/`audit_logs`/`runtime_action_runs` 四张表各自写入，**无投递、无订阅、无重放、无 outbox** | `service.py` 仅有 `record_event`；`scheduler.py`/`executor.py`/`runtime.py`/`resources.py` 只写各自表 + 调 `service.audit` | **MISSING** |
| P3-M2 | Agent / Skill / Model / Runtime Registry | 只有 `project_agents`（14 行，角色配置）；**无 Skill 注册、无 Model 注册、无 Runtime Registry**；`runtime_services`(5 行) 是本机服务探测清单，不是 Runtime Registry | `.schema project_agents`；`runtime_services` 无版本/能力/健康契约字段 | **MISSING** |
| P3-M3 | 任务规划与智能调度引擎 V1 | 有 `scheduler.plan()`（纯计算）+ `executor.run_item()`（起 `claude -p`）+ 租约 + 唯一部分索引；`dispatch_plans` 2 行、`dispatch_items` 1 行（succeeded, exit 0） | `scheduler.py` 395 行、`executor.py` 444 行、`test_scheduler.py`、`test_executor.py`；`dispatch_items.id=7be7f665…` | **PARTIAL** |
| P3-M4 | Local Agent Gateway + CLI Adapter | 只有 `executor.py` 直接 `subprocess` 调 `claude`；**无 Gateway（无设备注册、无命令队列、无准入令牌、无多 Runtime 路由）** | `executor.py:argv()`；`executor_policy.ALLOWED_BINARY="claude"` | **MISSING**（CLI 适配为 PARTIAL） |
| P3-M5 | 跨项目协作与动态组队 | 有跨项目依赖字段、`shared_resources`/`resource_claims`、协作视图；但 `task_dependencies`=0、`shared_resources`=0、`resource_claims`=0，**零真实使用**；无"动态组队" | `.schema task_dependencies`；行数见 E-04 | **PARTIAL** |
| P3-M6 | Android PWA 移动指挥中心 | 有响应式断点、移动端冒烟 7 项、握手令牌鉴权；**无 PWA（无 manifest/service worker）、无推送、无设备认证、无移动审批**；Tailscale 实际 stopped | `agents_dashboard.html` 断点；`mobile_smoke.mjs`；`auth.py` | **PARTIAL** |
| P3-M7 | GitLab/CI、成本与研发效能中心 | 有 `token_usage_events`(17,812) 采集与成本分摊；**无 GitLab 接入、无 CI 聚合、无质量门禁、无预算/限额** | `sources.py` ccusage 解析；无任何 gitlab/ci 模块 | **MISSING**（Usage 采集为 PARTIAL） |
| P3-M8 | 综合试点、稳定性、安全与规模化验收 | 无试点方案、无容量报告、无安全报告、无故障演练、无 Runbook、无最终验收签署 | `docs/` 下仅有 `三期建设方案.md`、`三期验收审计报告.md`、`三期验收报告.md`，均为自定范围 | **MISSING** |

**P3-M8 判定：MISSING。** 依据 V6.0 §0.4，`ACCEPTED` 必须由用户或授权负责人依据证据正式验收；现有文档为 Claude 自产、无验收人，不构成 ACCEPTED。

---

## 3. 实际交付的 M3-0～M3-4 能力矩阵

这一节回答"本仓库到底有什么"，是第四期唯一可依赖的能力事实。

| 能力 | 实现 | 测试 | 真实数据 | 状态 |
|---|---|---|---|---|
| schema 版本链迁移（v1→v2→v3） | `migrate.py` 609 行，`Connection.backup()` 热备 | `test_migrate_v3.py` 15 项 | schema_version=3 | **VERIFIED** |
| 写路径事务化 | `store.write_tx()`（BEGIN IMMEDIATE） | `test_write_tx.py` 3 项 | — | **VERIFIED** |
| 统一错误体 + 路由收敛 | `agents_dashboard.py` `_error()` | `test_phase2_http.py` | — | **VERIFIED** |
| OpenAPI 3.1 契约双写校验 | `openapi.py`，`API_PATHS` 60 条 = `_paths()` | `test_openapi.py` 强制 | 49/49 路径覆盖 | **VERIFIED** |
| 多项目隔离 | `_slug_matches` 精确匹配 | `test_phase2_isolation.py` 9 项 | 2 项目 | **VERIFIED** |
| 任务 CRUD 与状态机 | `service.py` | 13 项 | 33 任务 | **VERIFIED** |
| 调度排程（纯计算） | `scheduler.plan()` | 12 项 | 2 计划 / 0→1 项 | **VERIFIED** |
| 调度执行（起进程） | `executor.run_item()` | 11 项 | **1 次成功**（exit 0，23s） | **VERIFIED**（单一路径） |
| 执行准入策略 | `executor_policy.py` | 专项用例 | 全局默认 off | **PARTIAL**（见 §4 缺陷 D-3） |
| 跨项目依赖 / 共享资源 | `service.py` / `resources.py` | 6 + 7 项 | 0 行 | **PARTIAL**（能力就绪、零使用） |
| 运行时采集（6 种探针） | `runtime.py` 382 行 | 16 项 | 5 服务 / 5 检查 | **PARTIAL**（采集线程已停，见 D-2） |
| 运行时受控操作 | `ACTION_CATALOG` 硬编码 4 动作 + 预检令牌 | 6 项 | 1 次只读 | **PARTIAL**（仅只读动作验证过） |
| 令牌鉴权（本机免令牌 / 远端 Bearer） | `auth.py` 138 行 | 10 项 | 本机未启用远端 | **PARTIAL**（二值模型，见 D-1） |
| 统一审计接入 | 四模块 `_safe_audit` | `test_audit_coverage.py` 7 项 | 6 条 | **VERIFIED** |
| 移动端响应式 | 单文件断点 + 侧栏抽屉 | `mobile_smoke.mjs` 7 项 | 未实机 | **PARTIAL** |

**合计：VERIFIED 8 / PARTIAL 7 / 无 MISSING（在 M3-0～M3-4 范围内）。**

---

## 4. 只读核验中新发现的缺陷

| ID | 缺陷 | 证据 | 影响 | 严重度 |
|---|---|---|---|---|
| D-1 | 权限只有"本机/远端"二值，无主体、无角色、无项目级授权 | `auth.check_request()` 仅比较 loopback 与单一令牌 | V6.0 §32.1「未授权跨项目读取/写入=0」**当前无法证明** | **P0 阻断** |
| D-2 | 运行时采集线程已停，`runtime_checks` 最新为 2026-11-05T11:41，落后 1 天 | `runtime_checks` 全表 5 行，同一时间戳 | 平台显示的服务状态可能是陈旧数据，违反 V6.0 §32.2「不展示虚假实时状态」 | 高 |
| D-3 | `--disallowedTools Bash` 与 `ALLOWED_BASH_PREFIXES` 自相矛盾：白名单定义了 30+ 前缀但 Bash 工具被整体禁用 | `executor_policy.py:122-133` vs `:46-55` | 执行器无法跑 pytest/ruff，与方案 §3 T3-103 表述不一致；白名单成为死代码 | 中 |
| D-4 | 调度执行未写入 `executions` 表，`source='dispatcher'` 一条都没有 | `select source,count(*) from executions` → 全部 44 行 `transcript` | 执行事实分散在两套表，第四期 Evaluation/Runtime 无法统一引用 | 中 |
| D-5 | 无预算机制：`token_usage_events` 有 17,812 条成本数据但无任何限额、无超支处置 | 全库无 budget 表/字段 | V6.0 §24.3 成本保护缺失 | 中 |
| D-6 | `auth.PUBLIC_PATHS` 白名单了 `/healthz`，但服务端未实现该路径（实测返回 `not_found`） | `curl /healthz` → `{"ok": false, "error": "not_found"}` | 健康探针不可用，外部监控无法判断存活 | 低 |
| D-7 | `data/token` 不存在，远端鉴权令牌将在首次远端访问时生成 | `ls data/token` → No such file | 本机使用无影响；开放 tailnet 前必须先固化 | 低 |
| D-8 | 调度、共享资源、运行时三类操作虽写 `audit_logs`，但**事件无 correlation_id**，无法串起一次跨域链路 | `audit_logs` 无该列 | V6.0 §23.1 统一关联标识、§11.6 Event Envelope 缺字段 | 中 |

D-1、D-2 是 P4-M0 验收门禁「无未处理的 P0 安全阻断项」的直接证据来源，已进入 `P4_RISK_REGISTER.md` 的 P4-R19/R20。

---

## 5. 用户裁定记录（按时间顺序，事实与裁定分开）

| 时间 | 事件 |
|---|---|
| 2026-11-05 | 用户下达三期规则：一期二期已完成、三期唯一阶段、保留现有系统、升级为自主调度/跨项目/运行时/移动端 |
| 2026-11-06 上午 | 按用户指令完成 M3-0～M3-4 合并主干、审计接入、真实执行 ACC-001、产出 `docs/三期验收报告.md`（PASS WITH RISKS） |
| 2026-11-06 下午 | 只读核验发现：V4.0 的 P3-M1–M8 与实际交付不一致，P3-M8 无验收证据 → 提交四份差距文档并停止 |
| **2026-11-06** | **用户明确指示：「上述全面取消，唯一根据我最新的这个文档（V1.0–V6.0 全版本完整总文档），包括了所有的相关的版本，开始建设执行」** |

**裁定解释（据此执行）：**

- 「上述全面取消」= 取消前一轮基于 P3-M8 缺失而设置的停止点与四份阻断文档的阻断效力。
- 「唯一根据本汇编」= 执行依据只剩本汇编：第 0 章为最高规则，A13(V6.0) 为第四期唯一规范，A10(V4.0) 为第三期唯一阶段规范。
- 「开始建设执行」= 进入 P4-M0 完整交付，不再以"证据不足"为由停止。

**风险归属**：P3-M8 验收缺失这一事实仍然存在（§2），本裁定由用户承担其后果。P4 的第一项工程约束因此是"补齐 P3 缺失的控制面能力"而非"假设它们存在"。

---

## 6. 第四期对第三期能力的复用准入判定

V6.0 §3.2 要求复用必须同时具备：代码 / API / 契约 / 权限 / 测试 / Owner。逐项判定：

| 能力 | 代码 | API | 契约 | 权限 | 测试 | Owner | 判定 |
|---|---|---|---|---|---|---|---|
| 项目 / 任务 / 执行 | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | **EXISTING_PARTIAL** |
| 调度 + 执行器 | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | **EXISTING_PARTIAL** |
| 审计 | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | **EXISTING_PARTIAL** |
| Usage 采集 | ✅ | ✅ | ✅ | n/a | ✅ | ✅ | **EXISTING_PARTIAL** |
| 运行时探测 | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | **EXISTING_PARTIAL** |
| 统一事件平面 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | **MISSING** |
| Agent/Skill/Model/Runtime Registry | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | **MISSING** |
| Local Agent Gateway / CLI Adapter | ❌（仅直调 CLI） | ❌ | ❌ | ❌ | ❌ | ❌ | **MISSING** |
| Policy Engine | ❌（仅硬编码白名单） | ❌ | ❌ | ❌ | 部分 | ❌ | **MISSING** |
| Kill Switch | ❌（仅环境变量重启） | ❌ | ❌ | ❌ | ❌ | ❌ | **MISSING** |
| Identity / RBAC / Project Scope | ❌ | ❌ | ❌ | ❌ | 部分 | ❌ | **MISSING** |
| Approval / 高风险审批 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | **MISSING** |
| GitLab / CI | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | **MISSING** |
| 预算 / 限额 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | **MISSING** |
| PWA / Notification / Device Mgmt | ❌ | ❌ | ❌ | ❌ | 部分 | ❌ | **MISSING** |

**共同缺口**：除 Usage 外，全部能力缺"权限"一列——因为没有主体身份体系，任何权限判定都无从附着。这是 ADR-P4-002 必须首先解决的问题。

**归属裁定（写入 ADR-P4-002）**：V6.0 §3.3 禁止"第二套 Identity/RBAC/Event Plane/Registry/Gateway/Policy/Kill Switch"。本仓库中这些能力**不存在第一套**，因此第四期建设它们属于**首次建设**，不构成重复建设；但必须建在第三期 Control Plane 的位置上（同一进程、同一 SQLite、同一 HTTP 层、同一审计），不得开第二套进程或第二个库。

---

## 7. 与第四期的直接接口

| 第四期需要 | 第三期提供 | 差距 | 处理 |
|---|---|---|---|
| Evaluation Run 执行 | `executor.run_item()` + `dispatch_items` | 无 `executions(source=dispatcher)`（D-4） | P4-M1 先补齐执行事实写入 |
| 事件投递 | 无 Event Plane | 全部缺失 | P4-M0 冻结 Envelope，P4-M1 建 Outbox |
| 资源授权 | 无主体 | 全部缺失 | P4-M1 前置，ADR-P4-003 |
| 成本对账 | `token_usage_events` 17,812 条 | 无预算、无归因规则 | P4-M6 使用同一账本，禁止重复记账 |
| 发布回滚 | 无 | 全部缺失 | P4-M4 |
