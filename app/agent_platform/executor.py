"""执行器：真正启动 ``claude`` 进程执行调度项（M3-1 T3-103）。

**本模块是整个平台唯一允许起外部进程的地方。** 所有参数都必须来自
``executor_policy`` 校验过的 ``ExecutionSpec``，不接受任意命令字符串。

生命周期（每项）：

    queued --claim--> leased --start--> running --exit 0--> succeeded
                                              --exit!=0--> failed
                                              --timeout--> failed(timeout)

租约（lease）用于防双跑：``lease_owner`` + ``lease_expires_at``。
同一任务的活跃项由 ``dispatch_items`` 的唯一部分索引兜底。

设计取舍：
* ``shell=False``，argv 是列表，prompt 作为独立参数，不经过 shell 解析。
* 超时用进程组 kill（``start_new_session=True`` + ``os.killpg``），
  避免子进程残留。
* 日志截断：单条 ≤4KB、单次 ≤2MB，防止一个话痨任务打爆数据库。
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import executor_policy as policy
from . import store

#: 租约默认时长（秒）。超时后可被其它执行者抢占。
LEASE_SECONDS = 3600

#: 判定执行成功的退出码。
SUCCESS_EXIT_CODES = (0,)


class ExecutorError(Exception):
    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


# ---------------------------------------------------------------------------
# 租约与状态机
# ---------------------------------------------------------------------------
def claim_item(conn, item_id: str, owner: str, *, lease_seconds: int = LEASE_SECONDS) -> dict[str, Any]:
    """把 queued 项租给自己。已被租用且未过期时拒绝。"""
    now = store.now_iso()
    deadline = _iso_after(lease_seconds)
    with store.write_tx(conn):
        row = conn.execute("SELECT * FROM dispatch_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ExecutorError("not_found", 404, "调度项不存在")
        status = row["status"]
        if status == "leased" and (row["lease_expires_at"] or "") > now:
            raise ExecutorError("already_leased", 409, "调度项已被其它执行者租用")
        if status not in {"queued", "leased", "failed"}:
            raise ExecutorError("invalid_state", 409, f"当前状态 {status} 不可领取")
        if status == "failed" and int(row["attempt"] or 0) >= int(row["max_attempts"] or 1):
            raise ExecutorError("attempts_exhausted", 409, "重试次数已达上限")
        conn.execute(
            "UPDATE dispatch_items SET status='leased', lease_owner=?, lease_expires_at=?,"
            " updated_at=? WHERE id=?",
            (owner, deadline, now, item_id),
        )
        _event(conn, item_id, "claimed", status, "leased", {"owner": owner})
    return dict(conn.execute("SELECT * FROM dispatch_items WHERE id=?", (item_id,)).fetchone())


def release_lease(conn, item_id: str) -> None:
    with store.write_tx(conn):
        conn.execute(
            "UPDATE dispatch_items SET status='queued', lease_owner=NULL,"
            " lease_expires_at=NULL, updated_at=? WHERE id=? AND status='leased'",
            (store.now_iso(), item_id),
        )


def _event(conn, item_id: str, event_type: str, from_status: str | None,
           to_status: str | None, detail: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO dispatch_events (id, item_id, event_type, from_status, to_status,"
        " detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, item_id, event_type, from_status, to_status,
         store.dumps_json(detail), store.now_iso()),
    )


def _iso_after(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(  # noqa: UP017
        timespec="seconds"
    )


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def run_item(item_id: str, *, owner: str = "local", dry_run: bool = False) -> dict[str, Any]:
    """执行一个调度项。done 之后返回执行结果。

    ``dry_run=True`` 只做全部校验并返回将要执行的 argv，不真正起进程。
    """
    conn = store.connect()
    try:
        store.init_db(conn)
        item = claim_item(conn, item_id, owner)

        plan = conn.execute(
            "SELECT * FROM dispatch_plans WHERE id = ?", (item["plan_id"],)
        ).fetchone()
        if plan is None:
            _fail(conn, item_id, "plan_missing", "计划不存在")
            raise ExecutorError("not_found", 404, "计划不存在")
        project = conn.execute(
            "SELECT * FROM agent_projects WHERE id = ?", (plan["project_id"],)
        ).fetchone()
        if project is None:
            _fail(conn, item_id, "project_missing", "项目不存在")
            raise ExecutorError("not_found", 404, "项目不存在")

        if not policy.project_execution_enabled(conn, project["id"]):
            _fail(conn, item_id, "project_disabled", "项目未开启执行")
            raise ExecutorError("project_disabled", 403,
                                f"项目 {project['code']} 未开启执行（settings.execution_enabled）")

        agent = conn.execute(
            "SELECT * FROM project_agents WHERE id = ?", (item["agent_id"],)
        ).fetchone()
        if agent is None:
            _fail(conn, item_id, "agent_missing", "执行角色不存在")
            raise ExecutorError("not_found", 404, "执行角色不存在")

        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (item["task_id"],)
        ).fetchone() if item["task_id"] else None
        prompt = _build_prompt(project, agent, task)

        try:
            spec = policy.build_spec(
                project_code=project["code"],
                repo_root=project["repo_root"],
                role_key=agent["role_key"],
                execution_mode=agent["execution_mode"],
                prompt=prompt,
                cwd=agent["worktree_path"] or project["repo_root"],
            )
        except policy.ExecutorError as exc:
            _fail(conn, item_id, exc.code, exc.message)
            raise ExecutorError(exc.code, exc.status, exc.message) from exc

        argv = spec.argv()
        if dry_run:
            release_lease(conn, item_id)
            return {"item_id": item_id, "dry_run": True, "argv": argv, "cwd": str(spec.cwd)}

        return _spawn(conn, item, spec, argv, task)
    finally:
        conn.close()


def _spawn(conn, item, spec: policy.ExecutionSpec, argv: list[str], task) -> dict[str, Any]:
    item_id = item["id"]
    started = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "UPDATE dispatch_items SET status='running', attempt=attempt+1,"
            " started_at=?, updated_at=? WHERE id=?",
            (started, started, item_id),
        )
        _event(conn, item_id, "started", "leased", "running",
               {"role": spec.role_key, "cwd": str(spec.cwd)})

    seq = 0
    total = 0
    tail: list[str] = []
    exit_code: int | None = None
    error_code: str | None = None
    error_summary: str | None = None

    try:
        # argv 来自白名单校验，shell=False
        proc = subprocess.Popen(
            argv,
            cwd=str(spec.cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,  # 独立进程组，便于整组 kill
            env=_child_env(),
        )
    except OSError as exc:
        _finish(conn, item_id, "failed", None, "spawn_failed", str(exc)[:500], task)
        raise ExecutorError("spawn_failed", 500, f"启动失败：{exc}") from exc

    deadline = time.monotonic() + spec.timeout_s

    def _reader(stream, name: str) -> None:
        nonlocal seq, total
        try:
            for line in iter(stream.readline, ""):
                chunk = line[: policy.MAX_LOG_CHUNK]
                if total >= policy.MAX_LOG_TOTAL:
                    continue
                total += len(chunk)
                seq += 1
                _append_log(conn, item_id, name, seq, chunk)
                tail.append(chunk.rstrip())
                del tail[:-20]
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001,S110 - 关闭失败的流无需处理
                pass

    threads = [
        threading.Thread(target=_reader, args=(proc.stdout, "stdout"), daemon=True),
        threading.Thread(target=_reader, args=(proc.stderr, "stderr"), daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    try:
        exit_code = proc.wait(timeout=spec.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            exit_code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            exit_code = None
    finally:
        for thread in threads:
            thread.join(timeout=5)
        remaining = max(0, int(deadline - time.monotonic()))

    if timed_out:
        status, error_code = "failed", "timeout"
        error_summary = f"执行超时（{spec.timeout_s}s），已终止进程组"
    elif exit_code in SUCCESS_EXIT_CODES:
        status = "succeeded"
    else:
        status = "failed"
        error_code = "nonzero_exit"
        error_summary = f"退出码 {exit_code}"

    _finish(conn, item_id, status, exit_code, error_code, error_summary, task,
            extra={"tail": tail[-10:], "remaining_budget_s": remaining})
    return {
        "item_id": item_id,
        "status": status,
        "exit_code": exit_code,
        "error_code": error_code,
        "error_summary": error_summary,
        "log_lines": seq,
    }


def _child_env() -> dict[str, str]:
    """子进程环境：剔除执行器开关本身，避免嵌套授权。"""
    env = {k: v for k, v in os.environ.items() if k != policy.ENV_SWITCH}
    return env


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except OSError:
            return
    time.sleep(3)
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


def _append_log(conn, item_id: str, stream: str, seq: int, content: str) -> None:
    try:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO dispatch_logs (id, item_id, stream, seq, content, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, item_id, stream, seq, content, store.now_iso()),
            )
    except Exception:  # noqa: BLE001,S110 - 日志写失败不能影响执行
        pass


def _finish(conn, item_id: str, status: str, exit_code: int | None,
            error_code: str | None, error_summary: str | None, task,
            extra: dict[str, Any] | None = None) -> None:
    now = store.now_iso()
    with store.write_tx(conn):
        conn.execute(
            "UPDATE dispatch_items SET status=?, finished_at=?, exit_code=?, error_code=?,"
            " error_summary=?, lease_owner=NULL, lease_expires_at=NULL,"
            " result_json=?, updated_at=? WHERE id=?",
            (status, now, exit_code, error_code, error_summary,
             store.dumps_json(extra or {}), now, item_id),
        )
        _event(conn, item_id, "finished", "running", status, {
            "exit_code": exit_code, "error_code": error_code,
        })
        _refresh_plan_status(conn, item_id)


def _fail(conn, item_id: str, error_code: str, message: str) -> None:
    _finish(conn, item_id, "failed", None, error_code, message, None)


def _refresh_plan_status(conn, item_id: str) -> None:
    row = conn.execute("SELECT plan_id FROM dispatch_items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        return
    plan_id = row["plan_id"]
    counts = {
        item["status"]: item["n"]
        for item in conn.execute(
            "SELECT status, COUNT(*) AS n FROM dispatch_items WHERE plan_id=? GROUP BY status",
            (plan_id,),
        )
    }
    active = {"queued", "leased", "running"}
    if any(counts.get(status) for status in active):
        return
    conn.execute(
        "UPDATE dispatch_plans SET status='done', finished_at=? WHERE id=?",
        (store.now_iso(), plan_id),
    )


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
def _build_prompt(project, agent, task) -> str:
    """把任务信息拼成给 Agent 的提示。任务缺失时给出显式说明。"""
    lines = [
        f"你是 {project['name']} 项目的 {agent['display_name']}（{agent['role_key']}）。",
        "",
    ]
    if task is not None:
        lines.extend([
            f"## 任务 {task['task_key']}",
            f"标题：{task['title']}",
            f"优先级：{task['priority']}",
        ])
        if task["description"]:
            lines.extend(["", "描述：", task["description"]])
        criteria = store.loads_json(task["acceptance_criteria_json"], [])
        if criteria:
            lines.append("")
            lines.append("验收标准：")
            lines.extend(f"- {item}" for item in criteria)
        if task["blocked_reason"]:
            lines.extend(["", f"备注：{task['blocked_reason']}"])
    else:
        lines.append("（本次调度项没有关联任务，请按角色职责处理当前项目待办。）")

    lines.extend([
        "",
        "## 工作约束",
        "- 只在本工作目录内改动；不要执行 git push、部署、删除数据或修改 .claude 配置。",
        "- 改动完成后不要自行提交，把变更留在工作区供人审阅。",
        "- 结束时用一段话说明：改了什么、验证命令与结果、还有什么没做。",
    ])
    return "\n".join(lines)


def task_brief(task_id: str) -> dict[str, Any]:
    """给 UI 用的任务摘要（不涉执行）。"""
    conn = store.connect()
    try:
        store.init_db(conn)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def executable_cwd(project, agent) -> Path:
    """该 Agent 的推荐工作目录（worktree 优先）。"""
    raw = agent["worktree_path"] or project["repo_root"]
    return Path(raw).expanduser()
