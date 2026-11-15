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

import json
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

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
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

        settings = store.loads_json(project["settings_json"], {}) or {}
        allow_bash = bool(settings.get("execution_bash_enabled"))
        agent_settings = Path(project["repo_root"]) / ".claude" / "agent-settings.json"
        try:
            spec = policy.build_spec(
                project_code=project["code"],
                repo_root=project["repo_root"],
                role_key=agent["role_key"],
                execution_mode=agent["execution_mode"],
                prompt=prompt,
                cwd=agent["worktree_path"] or project["repo_root"],
                # 项目可通过 settings 缩短单次执行，验收/试跑时很有用
                max_turns=settings.get("dispatch_max_turns"),
                timeout_s=settings.get("dispatch_timeout_s"),
                # 项目级 Bash 开关：默认关闭；开启后仍只放行前缀白名单。
                allow_bash=allow_bash,
                # 仅平台调度时注入仓库守卫 hook；主会话不加载该文件。
                settings_file=str(agent_settings)
                if allow_bash and agent_settings.is_file()
                else None,
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
            " started_at=?, finished_at=NULL, exit_code=NULL, error_code=NULL,"
            " error_summary=NULL, updated_at=? WHERE id=?",
            (started, started, item_id),
        )
        _set_agent_status(conn, item["agent_id"], "working", item["task_id"])
        _event(conn, item_id, "started", "leased", "running",
               {"role": spec.role_key, "cwd": str(spec.cwd)})

    seq = 0
    total = 0
    tail: list[str] = []
    # R2 / D-2：Claude CLI 在 stream-json 的 result 记录里给出 total_cost_usd，
    # 这是 provider 侧算好的金额，属于真实账单而非本地估算，不能丢。
    cli_cost: dict[str, Any] | None = None
    cost_lock = threading.Lock()
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
        nonlocal seq, total, cli_cost
        # 每个读线程用自己的连接：主线程的 sqlite 连接不能跨线程使用
        # （check_same_thread 默认 True），否则日志会被静默丢弃。
        log_conn = store.connect()
        try:
            store.init_db(log_conn)
            for line in iter(stream.readline, ""):
                chunk = line[: policy.MAX_LOG_CHUNK]
                if total >= policy.MAX_LOG_TOTAL:
                    continue
                total += len(chunk)
                seq += 1
                _append_log(log_conn, item_id, name, seq, chunk)
                tail.append(chunk.rstrip())
                del tail[:-20]
                if name == "stdout" and '"total_cost_usd"' in chunk:
                    parsed = _parse_result_cost(chunk)
                    if parsed is not None:
                        with cost_lock:
                            cli_cost = parsed
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001,S110 - 关闭失败的流无需处理
                pass
            try:
                log_conn.close()
            except Exception:  # noqa: BLE001,S110
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
            extra={"tail": tail[-10:], "remaining_budget_s": remaining,
                   "cli_cost": cli_cost})
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


def _set_agent_status(conn, agent_id: str | None, status: str,
                      task_id: str | None) -> None:
    """同步 Agent 状态与心跳，供资源池/看板展示。

    执行器不写这三列时，调度过的 Agent 会一直停留在 offline，底部资源池
    永远显示 0。这里只更新调度相关字段，不触碰团队配置。
    """
    if not agent_id:
        return
    now = store.now_iso()
    conn.execute(
        "UPDATE project_agents SET status=?, current_task_id=?,"
        " last_heartbeat_at=?, last_seen_at=? WHERE id=?",
        (status, task_id, now, now, agent_id),
    )


def _finish(conn, item_id: str, status: str, exit_code: int | None,            error_code: str | None, error_summary: str | None, task,
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
        row = conn.execute(
            "SELECT project_id, task_id, role_key, attempt, agent_id FROM dispatch_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is not None:
            # 执行结束后回到 idle，资源池才能把它算作「可用」。
            _set_agent_status(conn, row["agent_id"], "idle", None)
            _record_execution(conn, item_id, row, status, exit_code,
                              cost=(extra or {}).get("cli_cost"))
            _safe_audit(
                conn, row["project_id"], "dispatch.item.finish", "dispatch_item", item_id,
                {
                    "task_id": row["task_id"],
                    "role_key": row["role_key"],
                    "attempt": row["attempt"],
                    "status": status,
                    "exit_code": exit_code,
                    "error_code": error_code,
                },
                result="ok" if status == "succeeded" else "failed",
            )
    # 事务结束后再动任务状态：service 会自己开连接，放在事务里会互相等锁。
    row = conn.execute(
        "SELECT project_id, task_id FROM dispatch_items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is not None and row["task_id"]:
        _sync_task_status(conn, row["project_id"], row["task_id"], status == "succeeded")


def _sync_task_status(conn, project_id: str, task_id: str, ok: bool) -> None:
    """把执行结果写回任务状态：成功 → review，失败 → blocked。

    状态机是 todo → in_progress → review → done，不能跳级，所以逐级推进。
    """
    try:
        from . import service

        def _advance(to_status: str, note: str | None = None) -> bool:
            row = conn.execute(
                "SELECT status, version FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return False
            payload = {"to_status": to_status, "expected_version": row["version"]}
            if note:
                payload["blocked_reason"] = note
            try:
                service.transition_task(project_id, task_id, payload)
                return True
            except Exception:  # noqa: BLE001 - 状态不合法就跳过，不阻断调度结果
                return False

        if ok:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            current = row["status"] if row is not None else None
            # 状态机是 todo → in_progress → review，blocked 只能回到 in_progress。
            # 重试成功时必须把中间态补齐，否则任务会永久卡在 blocked：
            # 「执行其实成功了，任务却显示阻塞」比不更新更误导人。
            if current == "blocked":
                _advance("in_progress", None)
            elif current == "todo":
                _advance("in_progress")
            _advance("review")
        else:
            _advance("blocked", "执行器运行失败，见调度项日志")
    except Exception:  # noqa: BLE001,S110 - 状态回写失败不影响执行结果
        pass


def _parse_result_cost(line: str) -> dict[str, Any] | None:
    """从 stream-json 的一行里取 provider 侧成本；取不到返回 None。

    只接受带有 ``total_cost_usd`` 的 result 记录。缺失、非数字、负数一律丢弃，
    宁可标 UNKNOWN 也不猜一个数字出来。
    """
    text = line.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        rec = json.loads(text)
    except ValueError:
        return None
    if not isinstance(rec, dict):
        return None
    raw = rec.get("total_cost_usd")
    if raw is None:
        return None
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return None
    if amount < 0:
        return None
    models: dict[str, Any] = {}
    for name, info in (rec.get("modelUsage") or {}).items():
        if not isinstance(info, dict):
            continue
        models[str(name)] = {
            "costUSD": info.get("costUSD"),
            "costBasis": info.get("costBasis"),
            "canonicalModel": info.get("canonicalModel"),
        }
    return {
        "total_cost_usd": amount,
        "model_usage": models,
        "subtype": rec.get("subtype"),
    }


def _record_execution(conn, item_id: str, row, status: str, exit_code: int | None,
                      *, cost: dict[str, Any] | None = None) -> None:
    """把调度执行写入 executions（source='dispatcher'）。

    第三期原有 executions 只有 source='transcript'；不补这条记录，执行事实就
    分散在两套表里，评估与成本归因都接不上（P4-R23）。

    ``cost`` 是 CLI 给出的 ``total_cost_usd``：provider 计算的金额，写进
    ``actual_cost_usd``。拿不到就整列留空，``cost_quality='unknown'``，
    绝不用价格表算一个数冒充账单。
    """
    actual: float | None = None
    quality = "unknown"
    if isinstance(cost, dict):
        raw = cost.get("total_cost_usd")
        try:
            if raw is not None and float(raw) >= 0:
                actual = float(raw)
                quality = "actual"
        except (TypeError, ValueError):
            actual = None
    try:
        item = conn.execute(
            "SELECT id, project_id, task_id, session_id, started_at, finished_at,"
            " role_key, correlation_id FROM dispatch_items WHERE id = ?", (item_id,)
        ).fetchone()
        if item is None or not item["started_at"]:
            return
        session_id = item["session_id"] or f"dispatch:{item_id}"
        started = store.iso_utc(item["started_at"])
        finished = store.iso_utc(item["finished_at"])
        duration_ms = None
        if started and finished:
            from datetime import datetime

            try:
                duration_ms = int(
                    (datetime.fromisoformat(finished) - datetime.fromisoformat(started))
                    .total_seconds() * 1000
                )
            except ValueError:
                duration_ms = None
        # 不能用 INSERT OR IGNORE：同一调度项重跑时旧记录会挡住新结果，
        # 于是「第一次失败、第二次成功」在库里永远显示失败。
        # 按 id 做 upsert，让执行记录反映**最近一次**的真实结果。
        conn.execute(
            "INSERT INTO executions (id, project_id, agent_id, task_id,"
            " session_id, status, started_at, finished_at, duration_ms, summary,"
            " total_tokens, source, correlation_id,"
            " actual_cost_usd, cost_quality, link_quality, first_seen_source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'dispatcher', ?, ?, ?, ?, 'dispatcher')"
            " ON CONFLICT(id) DO UPDATE SET"
            "   status = excluded.status,"
            "   started_at = excluded.started_at,"
            "   finished_at = excluded.finished_at,"
            "   duration_ms = excluded.duration_ms,"
            "   summary = excluded.summary,"
            "   correlation_id = excluded.correlation_id,"
            "   actual_cost_usd = COALESCE(excluded.actual_cost_usd, executions.actual_cost_usd),"
            "   cost_quality = excluded.cost_quality,"
            "   link_quality = excluded.link_quality,"
            "   task_id = COALESCE(excluded.task_id, executions.task_id)",
            (
                store.stable_id("dispatch", item_id), item["project_id"], item["role_key"],
                item["task_id"], session_id,
                "succeeded" if status == "succeeded" else status,
                started, finished, duration_ms, f"dispatch_item={item_id}",
                item["correlation_id"], actual, quality,
                "linked" if item["task_id"] else "dispatcher_missing_task",
            ),
        )
    except Exception:  # noqa: BLE001,S110 - 执行记录失败不应回滚调度结果
        pass


def _fail(conn, item_id: str, error_code: str, message: str) -> None:
    _finish(conn, item_id, "failed", None, error_code, message, None)


def _safe_audit(
    conn, project_id: str | None, action: str, resource_type: str,
    resource_id: str | None, detail: dict[str, Any], *, result: str = "ok",
) -> None:
    """写统一审计；失败不阻断执行。"""
    try:
        from . import service

        service.audit(conn, project_id, action, resource_type, resource_id,
                      detail=detail, result=result)
    except Exception:  # noqa: BLE001,S110 - 审计失败不影响执行
        pass


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
        "【铁律】思考、说明与答复一律用简体中文；技术术语可保留英文原词。",
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
        "- 全程使用简体中文；禁止用英文作答。",
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
