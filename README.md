# Multi-Project Agent Workspace

多项目 Agent 管理平台（本地只读看板 + 项目库）。**不属于任何单个项目的 Git 仓库**，独立维护。

## 目录

```
~/Multi-Project-Agent-Workspace/
├── app/                       # 平台代码
│   ├── agents_dashboard.py    # HTTP 服务（127.0.0.1:8099）
│   ├── agents_dashboard.html  # 单页前端
│   ├── agent_platform/        # 配置/存储/服务/缓存/OpenAPI
│   └── tests/                 # pytest + 两个 Playwright 冒烟
├── config/projects.json       # 纳管项目清单（唯一事实源）
├── data/                      # platform.db + state-cache/ + logs/
└── scripts/                   # start.sh / status.sh / test.sh

~/agent-team/<code>-team/      # 每个项目的独立团队目录
└── worktrees/ runner/ backups/

~/vscodeworkspace/<项目>/      # 各项目代码仓库
```

## 日常

```bash
cd ~/Multi-Project-Agent-Workspace
./scripts/start.sh 8099 --background   # 启动（默认库 data/platform.db）
./scripts/status.sh                     # 自检：库/进程/接口/项目数
./scripts/test.sh                       # py_compile + pytest + ruff
python3 scripts/agent_status.py ...     # 已随项目迁移，见下
```

打开 http://127.0.0.1:8099 ，左侧栏按 `config/projects.json` 列出项目；
每个项目的花名册、任务板、worktree、报告、用量都从**该项目仓库自己的**目录扫描。

## 环境变量

| 变量 | 作用 |
|---|---|
| `DDP_AGENT_PLATFORM_DB` | 库路径（默认 `data/platform.db`） |
| `DDP_AGENT_PLATFORM_WORKSPACE` | 工作区根（默认本目录） |
| `DDP_AGENT_PLATFORM_REPO` | 覆盖默认项目仓库根 |
| `PLAYWRIGHT_MODULE` | 冒烟测试的 playwright 路径 |

## 新增一个项目

1. 代码放 `~/vscodeworkspace/<repo>/`，团队目录 `~/agent-team/<code>-team/`。
2. 在 `config/projects.json` 加一条（`id/name/repo_root/team_dir/session_slugs`）。
3. 重启平台；项目会自动登记（`service.bootstrap` 读配置）。

## 项目纳管模式

详见 [docs/项目纳管约定.md](docs/项目纳管约定.md)：有 `ai-team/` 的项目（DDP）以仓库为事实来源，
没有的项目（Material）以平台库为事实来源。

## 现状（2026-11-05）

- [IMPLEMENTED] `/api/state` **会话扫描按项目精确隔离**：`build_state` 把项目仓库传给 `scan_agents`，
  `_prefixes_for` 改为「配置登记的精确 slug 名单」，不再用前缀匹配。修复前 material 面板显示的是
  DDP 的 Owner 与 72 个 agent。回归用例 `test_agent_scan_is_scoped_to_project_slugs`，59 passed。
- [IMPLEMENTED] 工作区已纳入 git（`.venv*/`、`data/platform.db`、`state-cache/`、`logs/` 不入库）。

## 现状（2026-11-04）

- [IMPLEMENTED] 平台代码已整体移出 DDP 仓库（DDP 侧提交 `b24bce6`），库在 `data/platform.db`，ddp + material 双项目独立渲染。
- [IMPLEMENTED] `scan_agents/scan_usage/scan_worktrees/parse_task_board/scan_reports/safe_repo_path` 全部按项目仓库参数化，slug 前缀按项目区分。
- [IMPLEMENTED] `/api/state` 的项目元信息不走缓存，改名/归档立即生效。
- [IMPLEMENTED] 验证：`./scripts/test.sh` 58 passed + ruff；UI 冒烟 **48/48**（夹具自动创建、收尾自动清理）。
- [KNOWN] 项目级 Token 有两条口径：`project_usage.token_24h`（平台库 token_usage_events，回填口径）与 `usage.total_tokens`（实时扫 transcript，含本轮未回填的部分），差异属预期，UI 项目概览用前者。
- [TODO] `scripts/agent_status.py` 仍留在 DDP 仓库（写 `ai-team/status/*.json`），功能不受影响。
