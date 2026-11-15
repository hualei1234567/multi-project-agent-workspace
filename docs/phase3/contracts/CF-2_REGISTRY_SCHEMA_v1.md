# CF-2 · Registry Schema v1（冻结）

> 冻结日期：2026-11-15
> 修订：2026-11-15 补 `main_session`（生产库实测存在的第四种模式，冻结时枚举遗漏）
> 冻结后规则：**只能加字段，不能改语义**。改语义需升 v2 并写迁移。
> 依据：蓝图 §3「P3-M2：资源注册中心」

---

## 1. 五层实体分离（关键）

V4.0 明确要求区分五个概念。混在一起是这类系统的典型失败模式：

| 实体 | 回答的问题 | 表 |
|---|---|---|
| **Agent Role Definition** | 系统里有哪些「角色」？ | `registry_agent_roles` |
| **Capability Profile** | 这个角色具备什么能力？ | `registry_agent_capabilities` |
| **Project Agent Instance** | 某项目里在跑哪个角色？ | `project_agents`（**既有，不改**） |
| **Skill Definition** | 系统里有哪些「技能」？ | `registry_skills` |
| **Runtime Instance** | 有哪些机器能跑？ | `runtime_services`（**既有**）+ `registry_runtimes`（扩展） |

**为什么不复用 `project_agents`**：它是「实例」（某项目某角色某 worktree），
Registry 是「定义」（角色本身）。一个角色可实例化到多个项目。

**为什么不复用 `capability_listings`**：它回答「某项目被授权用什么」，
Registry 回答「系统里存在什么」。语义不同的两张表强行合并会重蹈
R5「多来源数据混在一张表」的覆辙。

---

## 2. 表定义

### 2.1 `registry_agent_roles` — 角色定义

```sql
CREATE TABLE registry_agent_roles (
  id              TEXT PRIMARY KEY,          -- role_<slug>
  role_key        TEXT NOT NULL UNIQUE,      -- 如 'qa-engineer'
  display_name    TEXT NOT NULL,
  description     TEXT,
  execution_mode  TEXT NOT NULL DEFAULT 'readonly',  -- readonly / worktree / subagent / main_session
  default_model   TEXT,                       -- 引用 registry_models.model_key
  source          TEXT NOT NULL DEFAULT 'builtin',   -- builtin / project / user
  version         INTEGER NOT NULL DEFAULT 1,
  status          TEXT NOT NULL DEFAULT 'active',    -- active / deprecated / revoked
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
```

**唯一性**：`role_key` 全局唯一。`id` 不可变。

**版本规则**：`version` 是整数，只增不减。破坏性变更（改 `execution_mode`）
必须升版并记 `registry_changes`。

### 2.2 `registry_agent_capabilities` — 能力画像

```sql
CREATE TABLE registry_agent_capabilities (
  id            TEXT PRIMARY KEY,
  role_id       TEXT NOT NULL REFERENCES registry_agent_roles(id),
  capability    TEXT NOT NULL,        -- 如 'code-review' / 'test-writing'
  proficiency   INTEGER NOT NULL DEFAULT 1,   -- 1..5
  evidence      TEXT,                 -- 凭什么说具备（不能为空则是编的）
  created_at    TEXT NOT NULL,
  UNIQUE(role_id, capability)
);
```

**`evidence` 不是装饰**：V4.0 要求「能力声明必须有依据」。
没有 evidence 的能力，调度评分时权重打折（见 registry.py）。

### 2.3 `registry_skills` — 技能定义

```sql
CREATE TABLE registry_skills (
  id            TEXT PRIMARY KEY,
  skill_key     TEXT NOT NULL UNIQUE,
  display_name  TEXT NOT NULL,
  description   TEXT,
  source        TEXT NOT NULL DEFAULT 'builtin',  -- builtin / project / user / plugin
  source_ref    TEXT,                 -- 插件路径或仓库地址
  version       TEXT NOT NULL DEFAULT '1.0.0',     -- semver
  risk_level    TEXT NOT NULL DEFAULT 'R1',        -- R0..R4
  requires_approval INTEGER NOT NULL DEFAULT 0,
  scan_status   TEXT NOT NULL DEFAULT 'unscanned', -- unscanned / passed / failed
  scan_detail   TEXT,
  status        TEXT NOT NULL DEFAULT 'active',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
```

**`scan_status`**：V4.0 要求 Skill 有「扫描」。未扫描（`unscanned`）的 Skill
**不允许**进入 ASSISTED 派发——这是硬约束，在 Policy 层拦截。

### 2.4 `registry_skill_grants` — 角色↔技能授权

```sql
CREATE TABLE registry_skill_grants (
  id            TEXT PRIMARY KEY,
  skill_id      TEXT NOT NULL REFERENCES registry_skills(id),
  role_id       TEXT NOT NULL REFERENCES registry_agent_roles(id),
  granted_by    TEXT NOT NULL,
  note          TEXT,
  created_at    TEXT NOT NULL,
  revoked_at    TEXT,
  UNIQUE(skill_id, role_id)
);
```

**撤销**：`revoked_at` 非空即失效，不删行（保留审计痕迹）。

### 2.5 `registry_models` — 模型画像

```sql
CREATE TABLE registry_models (
  id                TEXT PRIMARY KEY,
  model_key         TEXT NOT NULL UNIQUE,   -- 如 'deepseek-v4.1-flash-1017'
  provider          TEXT NOT NULL,          -- 如 'deepseek' / 'anthropic'
  protocol          TEXT NOT NULL DEFAULT 'openai',  -- openai / anthropic
  context_window    INTEGER,                -- 上下文上限（token）
  max_output        INTEGER,
  supports_tools    INTEGER NOT NULL DEFAULT 1,
  supports_vision   INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL DEFAULT 'active',
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
```

**与 `model_price_versions` 的关系**：价格是独立表（按生效区间），
本表只描述**能力**。两者用 `model_key` 关联，不合并——价格会随时间变，
能力不会。

### 2.6 `registry_runtimes` — 运行时实例（扩展 `runtime_services`）

```sql
CREATE TABLE registry_runtimes (
  id                TEXT PRIMARY KEY,
  runtime_key       TEXT NOT NULL UNIQUE,
  service_id        TEXT REFERENCES runtime_services(id),
  kind              TEXT NOT NULL,      -- local / remote-gateway
  device_name       TEXT,
  capabilities      TEXT,               -- JSON 数组
  max_concurrency   INTEGER NOT NULL DEFAULT 1,
  heartbeat_at      TEXT,
  heartbeat_ttl_s   INTEGER NOT NULL DEFAULT 120,
  allocatable       INTEGER NOT NULL DEFAULT 1,
  status            TEXT NOT NULL DEFAULT 'unknown', -- unknown/healthy/degraded/offline/revoked
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
```

**心跳超时判定**：`heartbeat_at + heartbeat_ttl_s < now` → `offline`。
不是靠轮询改状态，而是**读取时计算**（避免采集线程挂掉就永久 healthy）。

### 2.7 `registry_runtime_leases` — 运行时租约

```sql
CREATE TABLE registry_runtime_leases (
  id            TEXT PRIMARY KEY,
  runtime_id    TEXT NOT NULL REFERENCES registry_runtimes(id),
  project_id    TEXT NOT NULL,
  holder        TEXT NOT NULL,        -- dispatch_item id 或 session id
  slots         INTEGER NOT NULL DEFAULT 1,
  acquired_at   TEXT NOT NULL,
  expires_at    TEXT NOT NULL,
  released_at   TEXT
);
```

**并发上限**：`SUM(slots) WHERE released_at IS NULL AND expires_at > now`
不得超过 `max_concurrency`。

### 2.8 `registry_compat_matrix` — 兼容矩阵

```sql
CREATE TABLE registry_compat_matrix (
  id            TEXT PRIMARY KEY,
  subject_type  TEXT NOT NULL,   -- agent_role / skill / model / runtime
  subject_id    TEXT NOT NULL,
  target_type   TEXT NOT NULL,
  target_id     TEXT NOT NULL,
  compatible    INTEGER NOT NULL DEFAULT 1,
  reason        TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE(subject_type, subject_id, target_type, target_id)
);
```

**默认策略**：没有显式记录的组合视为**兼容**（宽松）。
有显式 `compatible=0` 的才拦截。理由：全矩阵记录不现实，且默认拒绝会让
新资源无法使用。**这是显式决策，不是疏忽。**

### 2.9 `registry_changes` — 变更审计

```sql
CREATE TABLE registry_changes (
  id            TEXT PRIMARY KEY,
  entity_type   TEXT NOT NULL,
  entity_id     TEXT NOT NULL,
  action        TEXT NOT NULL,   -- create / update / deprecate / revoke / restore
  from_version  INTEGER,
  to_version    INTEGER,
  diff_json     TEXT,
  changed_by    TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
```

---

## 3. 生命周期与撤回

| 状态 | 含义 | 可否调度 |
|---|---|---|
| `active` | 正常 | ✅ |
| `deprecated` | 不推荐新用，已用的继续 | ⚠️ 新计划不选，已有计划可跑完 |
| `revoked` | 立即失效 | ❌ |

**撤回语义**：`status='revoked'` 立即生效，无需重启。
调度器在 `plan()` 时硬过滤。

---

## 4. 契约约束（测试强制）

1. `role_key` / `skill_key` / `model_key` / `runtime_key` 全局唯一
2. 撤销用 `status`/`revoked_at`，**不删行**
3. `registry_agent_capabilities.evidence` 为空的能力，评分权重 ×0.5
4. `registry_skills.scan_status != 'passed'` 不允许 ASSISTED 派发
5. 兼容矩阵无记录 = 兼容（显式宽松决策）
6. 所有变更写 `registry_changes`

---

## 5. 不做什么

- **不做**资源自动发现（Agent 角色是人定义的，不是扫出来的）
- **不做**跨项目共享 Registry（Registry 是平台级，授权是项目级）
- **不改** `project_agents`（实例表保持原样）
- **不合并** `model_price_versions` 进 `registry_models`
