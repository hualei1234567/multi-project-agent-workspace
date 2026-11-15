"""Claude CLI Adapter（P3-M4 / ADR-P3-003）。

**为什么需要它**：平台里有两条路径在构造 ``claude`` 的 argv——``executor.py``
走 Policy + 审批，``team.py`` 直接拼 ``--bg``。ADR-P3-003 要求把「怎么起进程」
收敛成一处，Gateway 与执行器都只从这里拿 argv。

边界（严格）：

* **只拼 argv，不起进程**。真正 ``Popen`` 仍在 ``executor.py``，本模块不改它的行为。
* **白名单不在这里重新实现**。所有校验（可执行文件、工作目录、角色标识、
  prompt 非空、settings 路径、执行模式）一律走 ``executor_policy.build_spec``；
  这里重复实现一份白名单就等于埋了第二套真相。
* **不编造**。``cli_version()`` 拿不到版本就返回 ``None``，不填假的版本号。
* 状态映射是**纯函数**：未知一律 ``UNKNOWN``，绝不猜成 ``SUCCEEDED``。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

from . import executor_policy as policy

#: 契约状态（ADR-P3-003 冻结的取值集合）。顺序即生命周期顺序，末尾是兜底。
CONTRACT_STATUSES: tuple[str, ...] = (
    "CREATED",
    "QUEUED",
    "SENT",
    "ACCEPTED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
    "UNKNOWN",
)

#: 终态：进入后不再接受新的 claim。
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}
)

#: 原始状态 → 契约状态。key 一律小写；未命中即 ``UNKNOWN``。
#:
#: 覆盖三类来源，故意放在一张表里：
#: * ``device_commands``（pending / dispatched / done / failed …）
#: * ``dispatch_items``（queued / leased / running / skipped …）
#: * Claude CLI ``stream-json`` 的 ``subtype``（success / error_max_turns …）
_STATUS_MAP: dict[str, str] = {
    # 已创建、还没进队列
    "created": "CREATED",
    "new": "CREATED",
    "planned": "CREATED",
    # 排队中
    "pending": "QUEUED",
    "queued": "QUEUED",
    "waiting": "QUEUED",
    # 已下发给设备，但设备还没认领
    "sent": "SENT",
    "dispatched": "SENT",
    "delivered": "SENT",
    # 设备已认领（拿到租约），还没开始跑
    "accepted": "ACCEPTED",
    "claimed": "ACCEPTED",
    "leased": "ACCEPTED",
    # 正在跑
    "running": "RUNNING",
    "in_progress": "RUNNING",
    "started": "RUNNING",
    "busy": "RUNNING",
    # 成功
    "succeeded": "SUCCEEDED",
    "success": "SUCCEEDED",
    "done": "SUCCEEDED",
    "completed": "SUCCEEDED",
    "ok": "SUCCEEDED",
    # 失败
    "failed": "FAILED",
    "failure": "FAILED",
    "error": "FAILED",
    "timeout": "FAILED",
    "timed_out": "FAILED",
    "error_max_turns": "FAILED",
    "error_during_execution": "FAILED",
    # 取消 / 跳过
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
    "killed": "CANCELLED",
    "stopped": "CANCELLED",
    "skipped": "CANCELLED",
    # 过期（未在有效期内被执行）
    "expired": "EXPIRED",
    "stale": "EXPIRED",
    # 明确未知
    "unknown": "UNKNOWN",
}

#: ``claude --version`` 输出形如 ``2.1.325 (Claude Code)``。
_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?)\b")


class CliAdapterError(Exception):
    """适配器拒绝原因；HTTP 层可映射为同形错误体。"""

    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def map_status(raw_status: Any) -> str:
    """原始状态 → 契约状态。识别不了就是 ``UNKNOWN``，不猜。"""
    if raw_status is None:
        return "UNKNOWN"
    text = str(raw_status).strip().lower()
    if not text:
        return "UNKNOWN"
    if text in _STATUS_MAP:
        return _STATUS_MAP[text]
    # 允许带前缀的写法（例如 "cmd:queued"、"exec/queued"），只取最后一段。
    tail = re.split(r"[:/]", text)[-1].strip()
    return _STATUS_MAP.get(tail, "UNKNOWN")


def is_terminal(contract_status: str) -> bool:
    return str(contract_status or "").upper() in TERMINAL_STATUSES


def build_execution(
    role_key: str,
    prompt: str,
    *,
    cwd: str | None = None,
    allow_bash: bool = False,
    timeout_s: int | None = None,
    max_turns: int | None = None,
    settings_file: str | None = None,
    project_code: str = "",
    repo_root: str | None = None,
    execution_mode: str = "worktree",
) -> policy.ExecutionSpec:
    """构造并校验执行规格。

    **全部校验委托** ``executor_policy.build_spec``：白名单、工作目录越界、
    执行器开关、可执行文件存在性都只有那一份实现。任一失败抛
    ``executor_policy.ExecutorError``（不是本模块自己编的异常）。

    ``project_code`` 只用于校验 team worktrees 前缀；缺省时仅允许仓库根，
    这比放宽到「任意目录」安全。
    """
    workdir = cwd or repo_root or ""
    if not str(workdir).strip():
        raise policy.ExecutorError("invalid_cwd", 422, "缺少工作目录")
    root = repo_root or workdir
    return policy.build_spec(
        project_code=str(project_code or ""),
        repo_root=root,
        role_key=role_key,
        execution_mode=execution_mode,
        prompt=prompt,
        cwd=workdir,
        max_turns=max_turns,
        timeout_s=timeout_s,
        allow_bash=allow_bash,
        settings_file=settings_file,
    )


def build_argv(
    role_key: str,
    prompt: str,
    *,
    cwd: str | None = None,
    allow_bash: bool = False,
    timeout_s: int | None = None,
    max_turns: int | None = None,
    settings_file: str | None = None,
    project_code: str = "",
    repo_root: str | None = None,
    execution_mode: str = "worktree",
) -> list[str]:
    """返回 ``claude`` 的 argv 列表（``shell=False`` 用，绝不拼成字符串）。

    校验全部来自 ``executor_policy``；这里只负责把它变成 List[str]。
    """
    spec = build_execution(
        role_key,
        prompt,
        cwd=cwd,
        allow_bash=allow_bash,
        timeout_s=timeout_s,
        max_turns=max_turns,
        settings_file=settings_file,
        project_code=project_code,
        repo_root=repo_root,
        execution_mode=execution_mode,
    )
    return spec.argv()


def binary_path() -> str:
    """``claude`` 可执行文件路径；不存在返回空串（不假装存在）。"""
    return shutil.which(policy.ALLOWED_BINARY) or ""


def cli_version(*, timeout_s: float = 10.0) -> str | None:
    """读取 ``claude --version`` 的版本号。

    返回 ``"2.1.325"`` 这样的版本串；二进制缺失、超时、非零退出、输出里
    解析不到版本号时一律返回 ``None``——**不编造版本**。
    """
    executable = binary_path()
    if not executable:
        return None
    try:
        proc = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout_s)),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = _VERSION_RE.search(proc.stdout or "")
    if match is None:
        # 少数版本把版本号写到 stderr；仍然只在能解析出 x.y.z 时采信。
        match = _VERSION_RE.search(proc.stderr or "")
    return match.group(1) if match else None
