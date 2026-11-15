# CF-1 · Event Schema v1（冻结）

> 冻结日期：2026-11-15
> 冻结后规则：**只能加字段，不能改语义**。
> 依据：蓝图 §3「P3-M1：统一事件平面」

---

## 1. 复用决策（ADR-P3-001）

**在 `p4/events.py` 的 Outbox/Inbox 上扩展，不新建第二条总线。**

已有能力（不重复造）：
- `emit()` 在调用方事务内写 outbox（业务状态与事件原子一致）
- `event_inbox (event_id, consumer)` 去重
- `replay()` 重放
- Envelope 按 V6.0 §11.6，含 correlation/causation

缺的（本次补）：
- 事件目录（有哪些 event_type）
- 按 event_type 的 schema 版本
- 退避重试（现在只有 attempts 计数，无间隔）
- DLQ 独立查询（现在只有 `status='failed'`）
- 数据新鲜度

---

## 2. Envelope v1（不变）

```json
{
  "event_id": "evt_<20hex>",
  "event_type": "task.created",
  "schema_version": 1,
  "project_id": "material",
  "resource_type": "task",
  "resource_id": "42224458...",
  "occurred_at": "2026-11-15T07:26:53+00:00",
  "ingested_at": "2026-11-15T07:26:54+00:00",
  "source_type": "p4_eventbus",
  "source_ref": "<outbox row id>",
  "correlation_id": "...",
  "causation_id": "...",
  "payload": {}
}
```

**字段语义冻结**：
- `occurred_at`：业务发生时间，**不是**写入时间
- `ingested_at`：平台收到时间
- `source_ref`：来源系统的记录 ID，用于追溯
- `correlation_id`：同一业务链路的追踪号
- `causation_id`：直接触发本事件的上游 event_id

---

## 3. 事件类型目录

命名规则：`<域>.<动作>`，小写，点分隔。

| event_type | 域 | 说明 |
|---|---|---|
| `task.created` | task | 任务创建 |
| `task.updated` | task | 任务更新 |
| `task.completed` | task | 任务完成 |
| `execution.started` | execution | 执行开始 |
| `execution.finished` | execution | 执行结束 |
| `delivery.created` | delivery | 交付创建 |
| `delivery.reviewed` | delivery | 交付验收 |
| `agent.started` | agent | Agent 启动 |
| `agent.stopped` | agent | Agent 停止 |
| `dispatch.plan_created` | dispatch | 调度计划生成 |
| `dispatch.item_run` | dispatch | 调度项执行 |
| `approval.requested` | approval | 审批申请 |
| `approval.decided` | approval | 审批决定 |
| `registry.changed` | registry | 注册中心变更 |
| `runtime.action` | runtime | 运行时操作 |
| `release.published` | release | 发布 |
| `canary.stopped` | release | Canary 越阈停止 |

**未登记的类型**：允许 emit（不能因为目录没更新就丢事件），但
`dispatch_pending` 会记 warning。目录的作用是**可发现性**，不是强制闸门。

---

## 4. 重试与退避

| 参数 | 值 | 说明 |
|---|---|---|
| `max_attempts` | 5 | 默认 |
| 退避策略 | 指数 | `delay = base * 2^(attempt-1)` |
| `base_delay_s` | 2 | 首次重试 2s，然后 4/8/16/32s |
| DLQ 阈值 | attempts >= max_attempts | 进 `status='failed'` |

**退避在 outbox 行上记录 `next_attempt_at`**，`pending()` 只取
`next_attempt_at <= now` 的行。这样不需要 sleep，投递线程继续跑。

---

## 5. DLQ 查询

```
GET /api/agent-platform/events/dlq          ?project_id=&event_type=&limit=
POST /api/agent-platform/events/{event_id}/replay
```

---

## 6. 数据新鲜度

全局总览与项目状态必须显示数据新鲜度。三态：

| 状态 | 含义 | 判据 |
|---|---|---|
| `FRESH` | 数据新鲜 | 最近事件在 5 分钟内 |
| `STALE` | 数据陈旧 | 最近事件在 5 分钟 ~ 1 小时 |
| `UNKNOWN` | 无数据 | 无任何事件，或来源不可达 |

**不用 0 或 Mock 伪装**——这是 V4.0 的硬要求。

---

## 7. 不做什么

- **不新建**第二条事件总线
- **不合并**既有的 `agent_events` / `dispatch_events` / `audit_logs`（它们是
  不同粒度的来源，通过 Adapter 汇入，不搬家）
- **不改** Envelope 的既有字段语义
- **不缓存** API 响应到 SW（PWA 阶段，见 ADR-P3-005）
