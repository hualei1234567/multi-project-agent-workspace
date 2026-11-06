# 多项目 Agent 管理平台 · General Owner 总指挥部

本工作区是跨项目的总指挥部：负责纳管项目、维护管理平台本身、汇总与分派任务。**具体业务开发在各项目仓库内进行，不在本工作区写业务代码。**

## 纳管项目

项目清单唯一来源：`config/projects.json`。

| 项目 | 仓库 | team 目录 | 面板 |
|---|---|---|---|
| `material` | `~/vscodeworkspace/MaterialDev` | `~/agent-team/material-team` | 同上，`?project=material` |

新增项目：改 `config/projects.json`（或 `POST /api/projects`），team 目录按 `~/agent-team/<id>-team` 推导。

## 平台本体的目录约定

```
app/agent_platform/  领域层（store / sources / service / templates / migrate / openapi）
app/agents_dashboard.py + .html   HTTP 层与 UI
config/projects.json 纳管清单
data/platform.db     平台库（默认；DDP_AGENT_PLATFORM_DB 可覆盖）
data/state-cache/    /api/state 磁盘快照
data/logs/           运行日志
scripts/{start,status,test}.sh
```

- 启动：`scripts/start.sh [端口] [--background]`，默认 8099，只监听本机。
- 自检：`scripts/status.sh`；测试：`scripts/test.sh`（py_compile + pytest + ruff）。
- 平台库与 DDP 业务库**完全独立**，禁止平台代码读写 DDP 的 PostgreSQL / Alembic 迁移链。
- `.venv` 为工作区自用环境；`.venv-py39-bak` 为历史备份，不要删除除非用户同意。

## 协作硬规则

- **证据优先**：结论可追溯到代码/命令/文件；未验证标「未验证」，规划不得写成已实现。
- **删除/迁移/重启/覆盖先说明**，不可逆操作拆两步：dry-run 列明细 → 用户确认 → 带计数执行 → 先备份。
- **禁止空过滤删除**：`if not filters: rows = []`；模糊前缀只用于 dry-run，实际删除用显式 ID。
- **项目之间不串台**：一个项目的改动不进另一个仓库；跨项目动作在本工作区记录。
- **原始 UniRobot 只读**，DDP 只用独立副本。
- Mac 只做开发/编辑；DDP 的镜像构建、e2e、发布都在 4090 上执行。
- 中文回复，先结论后证据；长任务把状态写进记忆库，保证跨会话可接续。

## 记忆

统一在 `~/.claude/projects/-Users-hualei-Multi-Project-Agent-Workspace/memory/`，先读 `MEMORY.md` 索引。各项目仓库的 `ai-team/`、`CLAUDE.md`、docs 仍是事实来源，本库不复制可从代码推导的内容。

## 各项目的入口

- **DDP**：`ai-team/TASK_BOARD.md`（任务与验收唯一看板）、`docs/095+`、`ai-team/WORKTREES.md`；生产在 4090，发布用 `scripts/release-4090.sh`。
- **Material**：`CLAUDE.md` → `AGENTS.md` → `docs/guides/ENGINEERING_PLAYBOOK.md`；生产是 `materialdev-lan-prod`，发布用 `scripts/deploy-lan.sh`；远端在 GitHub（`gh` 已认证）。
