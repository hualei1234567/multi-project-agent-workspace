# 运维手册（Runbook）

> 交付物：P4-OPS-006
> 适用范围：本机（Mac）上的多项目 Agent 管理平台，端口 8099
> 最后更新：2026-11-06

---

## 1. 服务拓扑

| 项 | 值 |
|---|---|
| 进程 | `python app/agents_dashboard.py --port 8099` |
| 监听 | 仅 `127.0.0.1:8099`（不绑局域网，除非显式 `--tailscale`） |
| 托管 | launchd `com.ddp.agents-dashboard`（`~/Library/LaunchAgents/`） |
| 数据库 | `data/platform.db`（SQLite，WAL） |
| 日志 | stdout `\tmp\ddp-agents-dashboard.log`、stderr `\tmp\ddp-agents-dashboard.err.log` |
| 健康检查 | `curl -s http://127.0.0.1:8099/healthz` → `{"ok":true,"schema_version":N}` |

---

## 2. 日常操作

### 2.1 启动 / 停止 / 重启

```bash
# 重启（推荐）
launchctl kickstart -k gui/$(id -u)/com.ddp.agents-dashboard

# 停止
launchctl bootout gui/$(id -u)/com.ddp.agents-dashboard

# 重新加载 plist（改过 plist 后）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ddp.agents-dashboard.plist
```

**注意**：启动到开始监听约需 **60 秒**（初始化时会回填项目数据、扫仓库）。
这期间 `curl` 会连接失败，不是卡死。判断卡死的方法：`ps` 看 CPU 是否在动。

### 2.2 健康检查

```bash
curl -s http://127.0.0.1:8099/healthz
sqlite3 data/platform.db "select * from schema_version;"          # 当前 schema 版本
sqlite3 data/platform.db "select max(checked_at) from runtime_checks;"  # 采集心跳，应 < 2 分钟
```

`runtime_checks` 超过 2 分钟没更新 → 后台采集线程挂了，看 stderr 日志。

### 2.3 测试与自检

```bash
.venv/bin/python -m pytest app/tests -q     # 全量测试
.venv/bin/ruff check app/                   # 静态检查
scripts/status.sh                           # 平台自检
```

---

## 3. 数据库

### 3.1 迁移

```bash
# 预演（只算不写）
.venv/bin/python -m app.agent_platform.migrate --dry-run

# 执行（默认自动热备）
.venv/bin/python -m app.agent_platform.migrate
```

- 迁移**只增不减**，`MIGRATIONS` 只允许追加；
- 每次执行都会用 `Connection.backup()` 做 WAL 安全热备，文件名带时间戳；
- 失败整体回滚，不会留下半成品。

### 3.2 备份

```bash
.venv/bin/python -c "
import sys; sys.path.insert(0,'app')
from agent_platform import migrate, store
print(migrate.backup_database(store.default_db_path()))"
```

RPO = 24 小时，RTO = 1 小时（单机 SQLite）。备份文件在 `data/`，保留 14 份后手工清理。

### 3.3 恢复演练（每季度一次）

```bash
.venv/bin/python scripts/p4_backup_drill.py
```

会做：热备 → integrity_check → 逐表行数比对 → 恢复到全新文件 → 再校验 → 撤销一致性检查。
结果写入 `docs/p4/reports/backup-drill.json`。

### 3.4 恢复真实灾难

```bash
launchctl bootout gui/$(id -u)/com.ddp.agents-dashboard
cp data/platform.db data/platform.db.broken-$(date +%Y%m%d%H%M%S)
cp data/platform.db.bak-<时间戳> data/platform.db
rm -f data/platform.db-wal data/platform.db-shm
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ddp.agents-dashboard.plist
curl -s http://127.0.0.1:8099/healthz
```

恢复后**必须重放撤销记录**：确认 `knowledge_versions.status='revoked'` 的版本
在 `knowledge_chunks` / `knowledge_fts` 中没有残留（`p4_backup_drill.py` 会检查）。

---

## 4. 紧急处置

### 4.1 Kill Switch（首选止血手段，秒级生效，不用重启）

```bash
# 全局停
.venv/bin/python -c "
import sys; sys.path.insert(0,'app')
from agent_platform import store
from agent_platform.p4 import killswitch
c = store.connect()
print(killswitch.trigger(c, scope='global', reason='事故处置', triggered_by='oncall'))
"

# 只看不停（列当前生效的开关）
.venv/bin/python -c "
import sys; sys.path.insert(0,'app')
from agent_platform import store
from agent_platform.p4 import killswitch
c = store.connect()
for s in killswitch.active(c): print(s['id'], s['scope'], s['reason'])
"

# 解除
.venv/bin/python -c "
import sys; sys.path.insert(0,'app')
from agent_platform import store
from agent_platform.p4 import killswitch
c = store.connect()
killswitch.release(c, '<switch_id>', released_by='oncall')
"
```

生效范围：`global` / `project` / `project_domain`（如只停某项目的 evaluation）。
进程内缓存 2 秒，所以最迟 2 秒后生效。

### 4.2 停止自动执行

```bash
# 全局总闸（需重启才生效，Kill Switch 是秒级手段）
DDP_AGENT_PLATFORM_EXECUTOR=off
# 项目级
sqlite3 data/platform.db "update agent_projects set settings_json = json_set(settings_json,'$.execution_enabled',0) where id='material';"
```

### 4.3 事故分级

| 级别 | 例子 | 动作 |
|---|---|---|
| P0 | 跨项目数据泄漏、Secret 泄漏、Kill Switch 失效 | 立即全局 Kill Switch + 停服务 + 保留现场 |
| P1 | 未授权发布、审批被绕过 | 项目级 Kill Switch + 冻结发布 |
| P2 | 采集停更、指标陈旧 | 重启采集，不影响业务 |

---

## 5. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 服务起不来，日志 `绑定 100.x.x.x:8099 失败` | plist 带 `--tailscale` 但 Tailscale 未运行 | 去掉该 flag，或先启动 Tailscale |
| `curl` 连接失败但进程在 | 仍在初始化（约 60 秒） | 等待；`ps` 看 CPU 是否在动 |
| API 全部 503 | Kill Switch 生效 | 按 §4.1 解除 |
| 远端全部 403 `not_a_project_member` | 令牌主体没被授权 | 在 `project_members` 显式加成员 |
| `runtime_checks` 停更 | 采集线程异常退出 | 重启服务，看 stderr |
| 迁移报「目标版本未登记」 | 代码比库旧 | 先更新代码，不要强行降级 |

---

## 6. 权限运维

```bash
# 给远端令牌授权某项目的 viewer（只读）
.venv/bin/python -c "
import sys; sys.path.insert(0,'app')
from agent_platform import store
from agent_platform.p4 import governance
c = store.connect()
print(governance.set_member(c, project_id='material', principal_id='remote-token', role='viewer'))
"
sqlite3 data/platform.db "select * from project_members;"   # 核对
```

角色只有四种：`owner` / `operator` / `viewer` / `auditor`。
**默认拒绝**：没有成员关系就是 403，不是 500。

---

## 7. 变更纪律

1. 任何 schema 变更 → 新的 `MigrationStep`，禁止改历史迁移；
2. 任何策略变更 → 写 ADR（`docs/p4/P4_ARCHITECTURE_AND_ADR_REGISTER.md`）；
3. 发布前必跑：pytest 全绿 + ruff 全清 + 真实库 dry-run 迁移；
4. 不可逆操作（删数据、改配置、重启）→ 先备份、先 dry-run、带计数执行。
