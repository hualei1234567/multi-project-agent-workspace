"""执行器准入策略：哪些命令、哪个项目、哪个角色允许被自动拉起（M3-1 T3-103）。

本模块**不启动进程**，只做判定。所有判定失败都必须「拒绝」，不能「放宽」。
真正的进程启动在 ``executor.py``，且只接受这里签发的 ``ExecutionSpec``。

安全模型（用户已确认「只读 + 改代码」）：

1. **全局开关**：``AGWS_PLATFORM_EXECUTOR`` 默认 ``off``，必须显式设为 ``on``。
2. **项目开关**：``agent_projects.settings_json.execution_enabled`` 默认 ``false``，
   逐项目开启。
3. **命令白名单**：只能执行 ``claude`` 一个可执行文件，参数由模板拼装，
   不接受任意 shell 字符串；``shell=False``。
4. **工作目录**：必须是项目仓库根，或 ``~/agent-team/<code>-team/worktrees/*`` 下。
5. **工具白名单**：只给读写代码所需的最小集合；Bash 默认禁用。
   项目可在 settings 中显式开启 Bash，开启后仍只放行
   ``ALLOWED_BASH_PREFIXES``，并叠加 ``BASH_DENY_RULES``。
6. **显式禁止**：push / 部署 / 删数据 / 改配置的形态一律拒绝。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import store

#: 全局开关环境变量；默认关。
ENV_SWITCH = "AGWS_PLATFORM_EXECUTOR"

#: 唯一允许的可执行文件。
ALLOWED_BINARY = "claude"

#: 项目铁律：所有 Agent 的思考与输出必须使用简体中文。
#: 通过 --append-system-prompt 注入，不依赖任务描述怎么写。
CHINESE_ONLY_RULE = (
    "【项目铁律·必须遵守】本项目的所有思考过程、工具调用说明、代码注释与最终答复，"
    "一律使用简体中文。禁止用英文回答；技术术语可保留英文原词，但句子必须是中文。"
)

#: 目标仓库没有定义该角色时的回退 agent。
#: 平台角色（project_agents.role_key）是平台概念，Claude Code 的 agent 定义是
#: 仓库概念；两者不一定对齐（例如 MaterialDev 没有 .claude/agents/）。
FALLBACK_AGENT = "general-purpose"

#: 允许的工具（--allowedTools）。
ALLOWED_TOOLS: tuple[str, ...] = (
    "Read", "Glob", "Grep", "Edit", "Write", "TodoWrite", "Skill",
)

#: Bash 命令前缀白名单：只有以这些开头的命令才放行。
#: 故意不含 git add/commit：改动留在 worktree 由人审阅后提交。
ALLOWED_BASH_PREFIXES: tuple[str, ...] = (
    "git status", "git diff", "git log", "git show", "git branch",
    "git worktree list", "git rev-parse", "git ls-files",
    "ls", "cat", "head", "tail", "wc", "find", "grep", "pwd", "sed -n",
    "python -m pytest", "python3 -m pytest", "pytest",
    "python -m py_compile", "python3 -m py_compile",
    "python3.11 -m pytest", "python3.11 -m py_compile",
    ".venv/bin/python", ".venv/bin/pytest", ".venv/bin/ruff",
    "backend/.venv/bin/python", "backend/.venv/bin/pytest", "backend/.venv/bin/ruff",
    "ruff check", "ruff format --check",
    "npm run build", "npm run lint", "npm run test", "npm ci",
    "npm --prefix apps/web run", "npm --prefix apps/web test", "npm --prefix apps/web exec",
    "npx tsc", "npx vitest run",
    "go build", "go test",
)

#: 显式禁止的命令形态（即便前缀白名单漏了也要挡）。
DENIED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bgit\s+push\b",
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+clean\b",
        r"\bgit\s+checkout\s+--\s*\.",
        r"\brm\s+-[rf]",
        r"\bdocker\b",
        r"\bkubectl\b",
        r"\bssh\b",
        r"\bscp\b",
        r"\bcurl\b",
        r"\bwget\b",
        r"\bsudo\b",
        r"\bdeploy\b",
        r"\brelease-4\d+",
        r"\beligible\b",
        r"\bmigrate\b",
        r"\bdrop\s+table\b",
        r"\btruncate\b",
        r"\bdelete\s+from\b",
        r"\bchmod\b",
        r"\bchown\b",
        r">\s*/dev/",
        r"\b\.\./\.\.",  # 疑似越界路径
    )
)

#: 项目级开启 Bash 时，传给 Claude Code 的「允许」规则。
#: Claude Code 的权限规则语法：Bash(<前缀>:*)。
BASH_ALLOW_RULES: tuple[str, ...] = tuple(
    f"Bash({prefix}:*)" for prefix in ALLOWED_BASH_PREFIXES
)

#: 项目级开启 Bash 时仍然显式拒绝的高危命令（deny 优先于 allow）。
BASH_DENY_RULES: tuple[str, ...] = (
    "Bash(git push:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(git checkout --:*)",
    "Bash(rm:*)",
    "Bash(docker:*)",
    "Bash(kubectl:*)",
    "Bash(ssh:*)",
    "Bash(scp:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(sudo:*)",
    "Bash(chmod:*)",
    "Bash(chown:*)",
    "Bash(migrate:*)",
    "Bash(psql:*)",
    "Bash(mysql:*)",
)

#: 允许的执行模式（与 scheduler.EXECUTABLE_MODES 一致）。
EXECUTABLE_MODES = frozenset({"worktree", "subagent"})

#: 单次执行默认上限。
DEFAULT_MAX_TURNS = 50
DEFAULT_TIMEOUT_S = 1800

#: 日志截断：单条与单次执行总量。
MAX_LOG_CHUNK = 4096
MAX_LOG_TOTAL = 2 * 1024 * 1024


class ExecutorError(Exception):
    """执行器拒绝原因；HTTP 层映射为同形错误体。"""

    def __init__(self, code: str, status: int = 403, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


@dataclass
class ExecutionSpec:
    """一次执行的完整参数。构造即校验，不合法直接抛 ExecutorError。"""

    project_code: str
    role_key: str
    prompt: str
    cwd: Path
    max_turns: int = DEFAULT_MAX_TURNS
    timeout_s: int = DEFAULT_TIMEOUT_S
    allowed_tools: list[str] = field(default_factory=lambda: list(ALLOWED_TOOLS))
    #: 实际传给 --agent 的值（可能与 role_key 不同）
    agent_arg: str = ""
    #: 项目级开关：是否允许 Bash（仍受前缀白名单与 deny 规则约束）。
    allow_bash: bool = False
    #: 额外的 Claude Code settings 文件（仅平台调度时注入，主会话不加载）。
    settings_file: str = ""

    def argv(self) -> list[str]:
        """拼装 argv（shell=False，不接受任意字符串）。"""
        allowed = list(self.allowed_tools)
        disallowed: list[str] = []

        if self.allow_bash:
            if "Bash" not in allowed:
                allowed.append("Bash")
            allowed.extend(BASH_ALLOW_RULES)
            disallowed.extend(BASH_DENY_RULES)
        else:
            disallowed.append("Bash")

        argv = [
            ALLOWED_BINARY,
            "-p", self.prompt,
            "--agent", self.agent_arg or self.role_key,
            "--output-format", "stream-json",
            "--verbose",  # stream-json 在 --print 模式下必须配 --verbose
            "--max-turns", str(self.max_turns),
            "--allowedTools", *allowed,
        ]
        if self.settings_file:
            argv.extend(["--settings", self.settings_file])
        if disallowed:
            argv.extend(["--disallowedTools", *disallowed])
        argv.extend(["--append-system-prompt", CHINESE_ONLY_RULE])
        return argv


def executor_enabled() -> bool:
    return os.environ.get(ENV_SWITCH, "off").strip().lower() in {"on", "1", "true", "yes"}


def project_execution_enabled(conn, project_id: str) -> bool:
    row = conn.execute(
        "SELECT settings_json FROM agent_projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return False
    settings = store.loads_json(row["settings_json"], {}) or {}
    return bool(settings.get("execution_enabled"))


def check_bash_allowed(command: str) -> tuple[bool, str]:
    """Bash 命令是否被允许。返回 (允许, 拒绝原因)。

    先过禁止模式，再过前缀白名单：任何一项不满足即拒绝。
    """
    text = (command or "").strip()
    if not text:
        return False, "空命令"
    for pattern in DENIED_PATTERNS:
        if pattern.search(text):
            return False, f"命中禁止模式 {pattern.pattern}"
    first = text.split("&&")[0].split(";")[0].strip()
    if not any(first.startswith(prefix) for prefix in ALLOWED_BASH_PREFIXES):
        return False, "不在 Bash 命令白名单内"
    return True, ""


def resolve_agent_arg(cwd: Path | str, role_key: str) -> str:
    """目标仓库若定义了该角色的 agent 文件则用角色名，否则回退。

    Claude Code 的 ``--agent`` 只认仓库内 ``.claude/agents/<name>.md`` 或内置 agent。
    平台角色（如 frontend-engineer）在没有该定义的仓库里传进去会直接报
    ``--agent 'x' not found`` 并退出，因此必须在启动前决定。
    """
    agent_file = Path(cwd).expanduser().resolve() / ".claude" / "agents" / f"{role_key}.md"
    return role_key if agent_file.is_file() else FALLBACK_AGENT


def validate_cwd(project_code: str, repo_root: Path | str, cwd: Path | str) -> Path:
    """工作目录必须落在项目仓库根或其 team worktrees 下。"""
    repo = Path(repo_root).expanduser().resolve()
    target = Path(cwd).expanduser().resolve()
    allowed_roots = [repo]
    team_worktrees = Path.home() / "agent-team" / f"{project_code}-team" / "worktrees"
    if team_worktrees.is_dir():
        allowed_roots.append(team_worktrees.resolve())
    for root in allowed_roots:
        try:
            target.relative_to(root)
            return target
        except ValueError:
            continue
    raise ExecutorError("invalid_cwd", 422, f"工作目录不在允许范围内：{target}")


def build_spec(
    *,
    project_code: str,
    repo_root: Path | str,
    role_key: str,
    execution_mode: str,
    prompt: str,
    cwd: Path | str | None = None,
    max_turns: int | None = None,
    timeout_s: int | None = None,
    allow_bash: bool = False,
    settings_file: str | None = None,
) -> ExecutionSpec:
    """构造并校验执行规格。任一校验失败抛 ExecutorError。"""
    if not executor_enabled():
        raise ExecutorError(
            "executor_disabled", 403,
            f"执行器未启用（设置 {ENV_SWITCH}=on 后重启）",
        )
    if not role_key or not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", role_key):
        raise ExecutorError("invalid_role", 422, "角色标识非法")
    if execution_mode not in EXECUTABLE_MODES:
        raise ExecutorError(
            "role_not_executable", 403,
            f"角色执行模式 {execution_mode} 不允许自动执行",
        )
    text = (prompt or "").strip()
    if not text:
        raise ExecutorError("invalid_prompt", 422, "任务提示为空")
    if shutil.which(ALLOWED_BINARY) is None:
        raise ExecutorError("binary_missing", 500, "找不到 claude 可执行文件")

    workdir = validate_cwd(project_code, repo_root, cwd or repo_root)
    settings_path = ""
    if settings_file:
        candidate = Path(settings_file).expanduser().resolve()
        if not candidate.is_file():
            raise ExecutorError("settings_missing", 422, f"settings 文件不存在：{candidate}")
        try:
            candidate.relative_to(Path(repo_root).expanduser().resolve())
        except ValueError as exc:
            raise ExecutorError("invalid_settings_path", 422, "settings 必须位于项目仓库内") from exc
        settings_path = str(candidate)

    return ExecutionSpec(
        project_code=project_code,
        role_key=role_key,
        prompt=text,
        cwd=workdir,
        agent_arg=resolve_agent_arg(workdir, role_key),
        max_turns=max(1, min(200, int(max_turns or DEFAULT_MAX_TURNS))),
        timeout_s=max(60, min(7200, int(timeout_s or DEFAULT_TIMEOUT_S))),
        allow_bash=bool(allow_bash),
        settings_file=settings_path,
    )
