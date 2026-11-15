# CF-4 · 三模式语义（冻结）

> 冻结日期：2026-11-15
> 冻结后规则：**不改行为定义**。
> 依据：蓝图 §2「首要风险：M3 自主调度必须先受控」+ §3「P3-M3」

---

## 1. 三种模式的硬定义

| 模式 | 计算候选 | 落库计划 | 创建 dispatch_item | 起进程 | 默认 |
|---|---|---|---|---|---|
| `shadow` | ✅ | ✅ | ❌ | ❌ | 否 |
| `recommend` | ✅ | ✅ | ❌ | ❌ | **是** |
| `assisted` | ✅ | ✅ | ✅（queued） | 需审批 | 否 |

**核心区别在「是否创建 dispatch_item」**：

- SHADOW / RECOMMEND 只写 `dispatch_plan_candidates`（候选表），
  **不写 `dispatch_items`**。所以执行器根本看不到它们——
  这是结构性保证，不是靠"记得别调执行器"。
- ASSISTED 写 `dispatch_items`，走审批闸门后执行。

**RECOMMEND 是平台默认。** SHADOW 只在显式要求评估时用。

---

## 2. 为什么 SHADOW 和 RECOMMEND 要分开

| | SHADOW | RECOMMEND |
|---|---|---|
| 目的 | 校准算法，不打扰人 | 给人看，等人决策 |
| 输出 | 评分 + 理由，写日志 | 候选集 + 匹配理由，给 UI |
| 生命周期 | 算完即弃 | 保留供人挑选 |
| 使用时机 | 调参期 | 日常 |

如果只有一种"不执行"模式，调参和日常使用会互相干扰。

---

## 3. 候选评分（可解释）

每个候选必须能回答「**为什么推荐它**」。评分维度：

| 维度 | 权重 | 说明 |
|---|---|---|
| 优先级 | 40 | P0=40, P1=30, P2=20, P3=10 |
| 依赖就绪度 | 20 | 依赖越少越靠前（已有硬过滤，这里做排序） |
| 角色能力匹配 | 25 | 来自 Registry 的 `capability_score` |
| 等待时长 | 15 | 越久越靠前（避免饿死） |

**Registry 能力匹配为 0 时**：不减分，只标记 `capability_unknown`。
不能因为没登记能力就永远排在最后——那是数据缺失惩罚功能。

评分与理由都落 `dispatch_plan_candidates`，供回放。

---

## 4. Policy 硬过滤先于评分

顺序不可颠倒：

```
1. 硬过滤（schedulable + Registry 状态 + 租约 + 依赖）  ← 任一不过，不出现在候选
2. 打分排序
3. 按 max_items 截断
```

**Policy 拒绝不能被评分覆盖。** 这是 V4.0 §2 的明文要求。

---

## 5. 模式升级路径

```
shadow  →  recommend  →  assisted  →  (full_auto，M9，独立立项)
```

**禁止跳级**：没有在 recommend 下被人工确认过若干次的角色/项目，
不应直接进 assisted。

**当前状态**：`assisted` 等价于现有行为（加审批闸门），
`shadow` / `recommend` 是新增的收敛态。

---

## 6. 数据模型

新表 `dispatch_plan_candidates`（v24）：

```sql
CREATE TABLE dispatch_plan_candidates (
  id             TEXT PRIMARY KEY,
  plan_id        TEXT NOT NULL REFERENCES dispatch_plans(id),
  project_id     TEXT NOT NULL,
  task_id        TEXT,
  agent_id       TEXT,
  role_key       TEXT,
  score          REAL NOT NULL DEFAULT 0,
  score_detail   TEXT,          -- JSON：各维度分值
  reason         TEXT,          -- 人能看懂的一句话
  rank           INTEGER,
  created_at     TEXT NOT NULL
);
```

`dispatch_plans` 加列 `mode`（v24）：

```sql
ALTER TABLE dispatch_plans ADD COLUMN mode TEXT NOT NULL DEFAULT 'assisted';
```

**默认值为什么是 `assisted`**：历史计划都是能执行的形态，
标成 `recommend` 会撒谎。新计划由代码显式传 `mode`，默认 `recommend`。

---

## 7. 不做什么

- **不做**自动模式切换（需要人显式指定或按配置）
- **不做**跨项目候选（M5 缩水版，暂不涉及）
- **不改** `dispatch_items` 既有语义
- **不删**旧计划的 `queued` items（历史数据保持原样）
