# P3-M3 自主执行安全审计

> 日期：2026-11-15
> 性质：只读审计（未改任何代码/配置/数据库）
> 对应蓝图 §2「首要风险：M3 自主调度必须先受控」

---

## 结论摘要

**平台当前处于「可真实自主派发」状态，但缺少 V4.0 要求的三重控制。**

| V4.0 要求 | 现状 | 判定 |
|---|---|---|
| SHADOW 模式（绝不派发） | 不存在 | **MISSING** |
| RECOMMEND 为平台默认模式 | 不存在 | **MISSING** |
| ASSISTED 未审批不派发 | 只有 RBAC，`policy_rules` 表为空 → 不拦截 | **不达标** |
| 旧入口不得绕过 Facade | 无 Facade，`team.py` 与 `executor.py` 各自为政 | **不达标** |
| 先硬过滤再评分 | 有硬过滤（状态/依赖/租约），无评分 | **PARTIAL** |
| 决策可解释 | 只有 `dispatch_events` 状态流转 | **PARTIAL** |

**没有发现已经发生的越权执行。** 但控制缺失是真实的。

---

## 一、真实调用链（已追踪验证）

```
POST /api/projects/{id}/dispatch/plan
  → scheduler.create_plan()            [纯计算 + 落库，不起进程]  ✅
  → 生成 dispatch_plans + dispatch_items (status=queued)

POST /api/projects/{id}/dispatch/items/{item_id}/run
  → _policy_guard(project_id, "dispatch_item", "run", risk="R3")   ← 唯一闸门
  → executor.run_item()
      → claim_item()                    [租约，防双跑]  ✅
      → project_execution_enabled()     [项目开关]     ✅
      → policy.build_spec()             [工具白名单]   ✅
      → _spawn() → subprocess.Popen(argv, shell=False)  ← 真实起进程
```

**第二条路径（绕过点）**：
```
team.py:288  executor_policy.bg_policy_args()
  → 直接构造 claude CLI argv
  → 不经过 policy.decide()，不经过任何审批
```
这是蓝图 §2 明确点名的「旧入口绕过 Facade」。

---

## 二、核心缺陷：审批闸门是空的

### 2.1 代码看起来有闸门

```python
# agents_dashboard.py:2467-2471
if len(rest) == 4 and rest[:2] == ["dispatch", "items"] and rest[3] in ("run", "dry-run"):
    dry = rest[3] == "dry-run"
    if not dry and not self._policy_guard(project_id, "dispatch_item", "run", risk="R3"):
        return
```

`_policy_guard` 调 `policy.decide()`，`decide()` 里确实有审批分支：
```python
if allow_needs_approval:          # policy.py:143
    return Decision(allowed=True, requires_approval=allow_needs_approval, ...)
```
`requires_approval=True` 时 `_policy_guard` 返回 412。

### 2.2 但规则表是空的

```
$ sqlite3 data/platform.db "SELECT COUNT(*) FROM policy_rules;"
0
$ sqlite3 data/platform.db "SELECT COUNT(*) FROM policy_rules WHERE resource_type IN ('dispatch_item','*');"
0
```

`decide()` 的流程：
1. Kill Switch → 未触发
2. 主体有效 → `local-owner` active ✅
3. **deny 规则遍历 → 0 条命中**
4. **RBAC → `is_platform_owner=1` → 直接 `return True`**（governance.py:200）
5. **allow 规则 → 0 条，`allow_needs_approval` 恒为 False**
6. 返回 `Decision(allowed=True, requires_approval=False)`

**结果：任何本机请求点 run，都无审批直接起 claude 进程。**

这不是代码 bug——是**配置缺失**。V4.0 要求「Assisted 未审批不派发」，
但平台从未初始化过任何 `requires_approval` 规则。

### 2.3 当前开关状态（实测）

```
$ ps eww -p 34072 | grep AGWS_PLATFORM_EXECUTOR
AGWS_PLATFORM_EXECUTOR=on          ← 全局开关已打开

$ sqlite3 data/platform.db "SELECT code, json_extract(settings_json,'$.execution_enabled') FROM agent_projects;"
material|1                          ← material 项目开关已打开
ddp|                                ← ddp 未开启
```

**两把锁都是开的。** 且 `dry_run` 路径完全不校验项目开关（`dry_run` 只走
`build_spec` + 返回 argv，不 spawn，风险低，可接受）。

---

## 三、V4.0 三模式缺口

| 模式 | 应行为 | 现状 |
|---|---|---|
| SHADOW | 计算候选/评分/Policy 结果并记录；**绝不派发** | 无此模式。`create_plan()` 必然产出可执行的 `queued` items |
| RECOMMEND | 展示 DAG/候选 Agent/匹配理由；**不执行**；**平台默认** | 无此模式。默认就是可执行态 |
| ASSISTED | 人工确认 **且** 审批通过后派发 | 有人工确认（点按钮），**无审批**（规则表空） |

**当前实际模式 ≈ ASSISTED 的退化版**：人工点一下就执行，没有审批环节。

---

## 四、其他已确认的安全事实（正面）

这些是真实有效、不应被上面的缺口抹掉的：

| 控制 | 证据 | 状态 |
|---|---|---|
| 工具白名单 | `executor_policy.ALLOWED_TOOLS` = Read/Glob/Grep/Edit/Write/TodoWrite/Skill | ✅ |
| 禁 push/部署/删数据 | `DENIED_BASH_PATTERNS`；`--disallowedTools`（`--bg` 下有效） | ✅ |
| **Bash 默认关闭** | `settings.execution_bash_enabled` 默认无 → `allow_bash=False` | ✅ |
| 租约防双跑 | `claim_item` + DB 唯一部分索引 | ✅ |
| 重试上限 | `attempt >= max_attempts` → `attempts_exhausted` | ✅ |
| 超时强杀 | `spec.timeout_s` + 进程组 kill | ✅ |
| 项目开关默认关 | `project_execution_enabled()` 默认 False | ✅（但 material 被开） |
| 全局开关默认 off | `executor_enabled()` 默认 "off" | ✅（但本机进程设了 on） |
| Kill Switch fail-closed | 无库返 503 而非放行（CI 实测） | ✅ |
| 项目隔离 | slug 精确匹配 + `test_project_isolation` | ✅ |
| 远端需 token | `auth.check_request`；`/api/open` 仅回环 | ✅ |
| 路径约束 | `cwd` 限 `repo_root` / worktree | ✅ |
| 执行不留 git 改动 | R3 实测 material `git status` 干净 | ✅ |

**结论：执行器的「起进程之后」是安全的；不安全的是「起进程之前缺审批」。**

---

## 五、V4.0 交付要求对照

| 要求 | 状态 |
|---|---|
| Shadow 无副作用 | NOT RUN（模式不存在） |
| Recommend 不创建命令 | NOT RUN（模式不存在） |
| Assisted 未审批不派发 | **不达标**（规则表空） |
| DAG 无环 | VERIFIED（`_dependency_cycle`） |
| 每次决策可解释 | PARTIAL（只有状态流转，无候选评分） |
| 项目权限与预算为硬约束 | 权限 VERIFIED / 预算 MISSING |
| 幂等、有限重试、人工接管 | 重试+租约 VERIFIED / 幂等 PARTIAL / 接管 MISSING |

---

## 六、建议的收敛动作（待批准，本轮不执行）

按 V4.0「先安全后功能」的顺序：

1. **立刻**：把 `AGWS_PLATFORM_EXECUTOR` 从启动脚本移除（默认 off 即恢复）。
   → 一行改动，零风险，立刻消除「无意中自主派发」的可能。
2. **P3-M3-a**：`scheduler.create_plan(mode=...)`，三模式枚举，默认 `recommend`。
3. **P3-M3-b**：初始化 `policy_rules`——`dispatch_item/run` 加
   `requires_approval=1` 的 allow 规则（scope=project，risk<=R3）。
4. **P3-M3-c**：Scheduler Facade，把 `team.py` 的 CLI 构造也收进 Facade，
   旧入口降级为 Facade 的薄封装。
5. **P3-M3-d**：决策记录表 `dispatch_decisions`（候选+评分+Policy 结果+理由）。

> 这些属于 P3-M3 范围，需先完成 P3-M0 并获批准后实施。
> 但第 1 项是安全止损，建议不等 P3-M0。
