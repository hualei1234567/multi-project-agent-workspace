# CF-5 · 预算与效能（冻结）

> 冻结日期：2026-11-15
> 依据：蓝图 §3「P3-M7」+ 用户决定「要 Full Auto」（预算是不带人值守时的刹车）

---

## 1. 预算模型

三个层级，逐级收紧：

| 层级 | 作用域 | 用途 |
|---|---|---|
| `platform` | 全平台 | 总闸，防止整体超支 |
| `project` | 单项目 | 主控粒度 |
| `task` | 单任务 | 特殊任务限额（可选） |

**生效规则**：任一层级超限即拦截。取最严格的那个。

---

## 2. 时间窗口

| 窗口 | 语义 |
|---|---|
| `daily` | 当日 00:00 起的累计 |
| `monthly` | 当月 1 日起的累计 |
| `total` | 不限时间（试点的硬上限） |

**跨窗口取最严格**：日预算 80% 但月预算 95% → 按 95% 判定。

---

## 3. 超支行为（关键决策）

| 状态 | 阈值 | 行为 |
|---|---|---|
| `ok` | < 预警线 | 正常 |
| `warning` | ≥ warning_ratio（默认 0.8） | 允许，但 UI 标黄 + 事件 |
| `exceeded` | ≥ 1.0 | **暂停新任务，不强杀运行中的** |

**为什么不强杀运行中的**：V4.0 明文要求「预算耗尽不强杀运行中任务、
不丢状态」。正在跑的任务杀掉会留下半成品工作树和不确定状态，
代价比多花一点钱大。

**暂停新任务的实现点**：`scheduler.schedulable()` 里加硬过滤——
超限项目的任务不进候选。这和 Registry 撤销角色的过滤是同一层。

---

## 4. 成本口径（复用已有三档）

不重复造。`cost.py` 已有 ACTUAL / ESTIMATED / LEGACY / ZERO / UNKNOWN。

**预算判定用哪个口径**：

```
优先 actual；没有 actual 用 estimated；都没有则跳过（不计入，但要报出来）
```

**必须暴露"有多少金额是估的"**——否则预算数字看起来精确但不可信。

---

## 5. 效能指标

| 指标 | 定义 | 数据来源 |
|---|---|---|
| Lead Time | 任务创建 → 完成 | `tasks.created/updated` |
| Cycle Time | 开始 → 完成 | `tasks.started_at/completed_at` |
| 一次通过率 | 首次执行即成功 | `dispatch_items.attempt=1 & succeeded` |
| 重试率 | attempt > 1 的占比 | `dispatch_items.attempt` |
| 人工介入率 | 需要审批/接管的比例 | `approvals` + `dispatch_events` |

**样本不足时显示 UNKNOWN，不算 0。** 与新鲜度同一原则。

---

## 6. 数据模型（schema v25）

```sql
CREATE TABLE budgets (
  id             TEXT PRIMARY KEY,
  scope          TEXT NOT NULL,        -- platform / project / task
  scope_id       TEXT,                 -- project_id 或 task_id；platform 为 NULL
  window         TEXT NOT NULL,        -- daily / monthly / total
  limit_usd      REAL NOT NULL,
  warning_ratio  REAL NOT NULL DEFAULT 0.8,
  enabled        INTEGER NOT NULL DEFAULT 1,
  note           TEXT,
  created_by     TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  UNIQUE(scope, scope_id, window)
);
```

**UNIQUE 约束**：同一作用域+窗口只能有一条预算。改预算用 UPDATE，不叠加。

### 预算事件（可追溯）

```sql
CREATE TABLE budget_events (
  id          TEXT PRIMARY KEY,
  budget_id   TEXT NOT NULL REFERENCES budgets(id),
  event_type  TEXT NOT NULL,        -- warning / exceeded / recovered
  spent_usd   REAL NOT NULL,
  limit_usd   REAL NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL
);
```

**只在状态跃迁时写**（ok→warning、warning→exceeded、exceeded→ok），
不是每次都写——否则一天几千条。

---

## 7. 不做什么

- **不做**预算耗尽时自动停服（只暂停新任务）
- **不做**跨项目预算池（当前只有两个项目，没有分摊场景）
- **不改** `cost.py` 的既有口径
- **不做**预测性预算（"照这个速度会超"）——先有基础判定

---

## 8. 与 Full Auto 的关系

Full Auto（M9）必须建立在本契约之上：

| Full Auto 需要 | 本契约提供 |
|---|---|
| 超支自动停 | `exceeded` → 暂停新任务 |
| 预警可观测 | `budget_events` + UI |
| 成本可信 | 三档口径 + 估算占比暴露 |

没有预算的 Full Auto 等于没有刹车的车。
