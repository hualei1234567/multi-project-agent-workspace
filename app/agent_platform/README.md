# 多项目 Agent 管理平台 · 领域模块（阶段 1 / Track B）

独立于业务库：**独立 SQLite + 本机文件采集 + stdlib HTTP 兼容层**。
依据 `docs/109-多项目Agent平台-阶段1实施方案.md`、`docs/110-多项目Agent管理平台V2.1规范.md`。

## 文件

| 文件 | 职责 |
|---|---|
| `schema.sql` | schema v1（7 张业务表 + `schema_version`，全部 `IF NOT EXISTS`） |
| `store.py` | 连接（`AGWS_PLATFORM_DB`）、建表、时间/ID/JSON 工具 |
| `templates.py` | 内置模板 `standard-1-6`（7 角色）、`lean-1-3`（4 角色） |
| `sources.py` | 任务板 / 自报状态 / worktree / 报告 / session transcript 采集（幂等 + 增量 cursor） |
| `service.py` | 项目 CRUD、模板初始化、统计聚合、隔离校验、回填编排 |

HTTP 在 `scripts/agents_dashboard.py`（旧 `/api/state` 字段与旧 HTML 保持不变）。

## 数据库

- 默认库：`~/.claude/agent-platform/platform.db`
- 覆盖：`AGWS_PLATFORM_DB=/path/to/platform.db`
- 新项目工作区：未传 `repo_root` 时创建 `~/.claude/agent-platform/projects/<code>`（与平台库同目录，测试随 DB 隔离），`session_slugs=[]`，不继承既有业务数据
- 会话目录覆盖（测试用）：`AGWS_PLATFORM_PROJECTS_ROOT`
- 默认仓库覆盖：`AGWS_PLATFORM_REPO`

启动自动建表并写入 `schema_version=1`；重复启动幂等。测试只用临时目录，不碰真实 `~/.claude`。

## API（stdlib）

| Method | Endpoint |
|---|---|
| GET | `/api/projects`（含 stats、`default_project_id`） |
| POST | `/api/projects`（事务创建项目 + 模板角色） |
| GET / PATCH | `/api/projects/{id}` |
| GET | `/api/projects/{id}/tasks?page&page_size&status&q` |
| GET | `/api/projects/{id}/deliveries?page&page_size` |
| GET | `/api/projects/{id}/events?page&page_size` |
| POST | `/api/projects/{id}/backfill` |
| GET | `/api/agent-platform/overview` |
| GET | `/api/state?project={id}`（缺省取配置里的默认项目；旧字段 + `project`） |
| GET | `/api/team-templates`、`/api/team-templates/{key}` |
| GET / POST | `/api/agent/{id}`、`/api/file?project&path`、`/api/open?project&path` |

错误体 `{"ok":false,"error":"<snake_case>"}`：
`project_not_found`(404)、`project_code_exists`(409)、`invalid_project_code`(422)、
`invalid_project_path`(422)、`project_archived`(409)、`invalid_json`(400)。

## 采集口径

- 任务：`ai-team/TASK_BOARD.md`，`task_key=TEAM-xxx`；状态映射
  `BACKLOG/READY→todo`、`IN_PROGRESS→in_progress`、`REVIEW/TESTING→review`、`DONE→done`、`BLOCKED/CANCELLED→blocked`。
- Agent：`ai-team/status/*.json` 优先，worktree 与分支补全；已有记录只补不覆盖。
- 交付：`ai-team/reports/`、`docs/reports/` 文件索引；文件名含 `TEAM-xxx` 时关联任务。
- Token：session `*.jsonl` 中 assistant `message.usage`，按
  `(session_id, message.id, agent_id, model)` 去重；`total = input + output + cache_read + cache_write`；
  `source=transcript`。按文件 byte offset 增量解析（cursor 存 `agent_projects.settings_json`），不反推 ccusage。
- 成本：调用 `ccusage claude session --json`，对 `projectPath` 命中项目 `session_slugs` 且
  `lastActivity` 在近 24h 的会话，按 **token 占比** 分摊：
  `cost_24h += totalCost × (本项目该会话近 24h token / session.totalTokens)`。
  **口径**：会话可能跨天，成本是按窗口 token 占比分摊的近似值，不是精确按日账单；
  无命中/无可分摊 token 返回 `null`。`/api/state` 的 `project_usage.cost_24h` 与
  overview/list 的 `stats.cost_24h` 同源；旧 `usage` 字段不变。

## 运行 / 测试

```bash
python3 scripts/agents_dashboard.py --port 8099      # 启动时自动建库 + 登记配置里的纳管项目
python3 scripts/agents_dashboard.py --once           # 回填后打印旧兼容快照
python3 -m pytest scripts/tests -q                   # 19 用例
```

## 回滚

平台数据独立：停止进程后删除 `~/.claude/agent-platform/platform.db`（或 `AGWS_PLATFORM_DB` 指向的文件）即可恢复原状；
不涉及业务库、Alembic 迁移、`ai-team/` 原始文件或前端资源。
