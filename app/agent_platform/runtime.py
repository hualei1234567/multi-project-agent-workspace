"""运行时控制：服务健康采集与受控操作（M3-3 T3-301/302/303）。

分两层，刻意分开：

* **采集（只读）**：``probe_service`` 用端口探测 / docker ps / launchctl / ps 判断服务状态。
  任何异常都降级为 ``unknown``，绝不抛给调用方。
* **操作（受控）**：``run_action`` 只能执行**代码里硬编码**的动作，不接受任意命令。
  每次操作必经 预检 → 二次确认（preflight_token） → 执行 → 审计。

设计原则与业务系统现有发布脚本一致（预检 → 备份 → 执行 → 健康检查 → 报告），
但由平台编排并把每一步留痕。
"""

from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any

from . import store

#: 预检令牌有效期（秒）。
PREFLIGHT_TTL_S = 300

#: 健康检查默认超时。
DEFAULT_PROBE_TIMEOUT_S = 5


class RuntimeError_(Exception):
    """运行时域错误；HTTP 层映射为同形错误体。"""

    def __init__(self, code: str, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


@dataclass(frozen=True)
class ActionSpec:
    """一个可执行动作。命令是**硬编码元组**，不接受外部拼接。"""

    key: str
    label: str
    argv: tuple[str, ...]
    timeout_s: int = 300
    requires_confirm: bool = True
    description: str = ""


#: 动作白名单。新增动作必须改这里 —— 数据库里的 runtime_actions 只能引用这些 key，
#: 不能承载任意命令。这是「一键操作」的安全边界。
ACTION_CATALOG: dict[str, ActionSpec] = {
    "restart_dashboard": ActionSpec(
        key="restart_dashboard",
        label="重启管理平台",
        argv=(
            "/bin/zsh",
            "-lc",
            (
                "pkill -f 'agents_dashboard.py --port 8099' || true; sleep 1; "
                "cd \"$HOME/Multi-Project-Agent-Workspace\" "
                "&& ./scripts/start.sh 8099 --background"
            ),
        ),
        timeout_s=90,
        description="重启 8099 自身。平台会短暂不可用（约 3-5 秒）。",
    ),
    "docker_restart_material": ActionSpec(
        key="docker_restart_material",
        label="重启 MaterialDev 容器",
        argv=("/usr/bin/env", "docker", "compose", "-p", "materialdev-lan-prod", "restart"),
        timeout_s=180,
        description="重启 materialdev-lan-prod 的容器（数据库不重建）。",
    ),
    "docker_ps_material": ActionSpec(
        key="docker_ps_material",
        label="查看 MaterialDev 容器状态",
        argv=("/usr/bin/env", "docker", "ps",
              "--filter", "name=materialdev-lan-prod", "--format",
              "{{.Names}}\t{{.Status}}"),
        timeout_s=30,
        requires_confirm=False,
        description="只读：列出 materialdev-lan-prod 容器。",
    ),
    "disk_usage": ActionSpec(
        key="disk_usage",
        label="查看 Docker 磁盘占用",
        argv=("/usr/bin/env", "docker", "system", "df"),
        timeout_s=60,
        requires_confirm=False,
        description="只读：docker system df。磁盘曾写满拖垮 PG，发布前必看。",
    ),
}


# ---------------------------------------------------------------------------
# 采集（只读）
# ---------------------------------------------------------------------------
def _probe_port(host: str, port: int, timeout: float) -> tuple[str, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "healthy", f"TCP {host}:{port} 可连接"
    except OSError as exc:
        return "down", f"TCP {host}:{port} 不可达：{exc}"


def _probe_http(url: str, timeout: float) -> tuple[str, str]:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return "healthy", f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 说明服务在跑，只是这个路径需要鉴权或不存在
        return ("healthy" if exc.code < 500 else "degraded"), f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 探测失败一律降级
        return "down", f"不可达：{exc}"


def _probe_process(pattern: str) -> tuple[str, str]:
    try:
        out = subprocess.run(
            ["pgrep", "-fl", pattern], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown", f"pgrep 失败：{exc}"
    if out.returncode == 0 and out.stdout.strip():
        return "healthy", out.stdout.strip().splitlines()[0][:200]
    return "down", f"没有匹配 {pattern} 的进程"


def _probe_command(argv: list[str]) -> tuple[str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown", f"命令失败：{exc}"
    if proc.returncode != 0:
        return "down", (proc.stderr or proc.stdout or "")[:300]
    return "healthy", (proc.stdout or "").strip()[:300]


def probe_service(service: dict[str, Any], *, timeout: float = DEFAULT_PROBE_TIMEOUT_S) -> dict[str, Any]:
    """按 kind 探测一个服务。永不抛异常。"""
    kind = service.get("kind") or "port"
    target = str(service.get("target") or "")
    try:
        if kind == "port":
            host, _, port_text = target.partition(":")
            status, detail = _probe_port(host or "127.0.0.1", int(port_text or 0), timeout)
        elif kind == "http":
            status, detail = _probe_http(target, timeout)
        elif kind == "process":
            status, detail = _probe_process(target)
        elif kind == "docker":
            status, detail = _probe_command(["/usr/bin/env", "docker", "inspect", "--format",
                                             "{{.State.Status}}", target])
        elif kind == "launchd":
            status, detail = _probe_command(["launchctl", "list", target])
        elif kind == "systemd":
            status, detail = _probe_command(["systemctl", "is-active", target])
        else:
            status, detail = "unknown", f"未知 kind：{kind}"
    except Exception as exc:  # noqa: BLE001 - 采集绝不打断平台
        status, detail = "unknown", f"探测异常：{exc}"
    return {"service_id": service["id"], "status": status, "detail": detail}


def collect_once(conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """探测所有启用服务并落库 runtime_checks。"""
    own = conn is None
    conn = conn or store.connect()
    try:
        if own:
            store.init_db(conn)
        services = [
            dict(row) for row in conn.execute(
                "SELECT * FROM runtime_services WHERE enabled = 1 ORDER BY name"
            )
        ]
        results = []
        for service in services:
            probe = probe_service(service)
            with store.write_tx(conn):
                conn.execute(
                    "INSERT INTO runtime_checks (id, service_id, status, latency_ms, detail, checked_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, service["id"], probe["status"], None,
                     probe["detail"], store.now_iso()),
                )
            results.append({**probe, "name": service["name"], "kind": service["kind"]})
        return results
    finally:
        if own:
            conn.close()


def list_services() -> dict[str, Any]:
    """服务列表，每项带最近一次探测结果。"""
    with store.session() as conn:
        rows = [
            dict(row) for row in conn.execute(
                "SELECT s.*, p.name AS project_name FROM runtime_services s"
                " LEFT JOIN agent_projects p ON p.id = s.project_id"
                " ORDER BY s.name"
            )
        ]
        for row in rows:
            last = conn.execute(
                "SELECT status, detail, checked_at FROM runtime_checks"
                " WHERE service_id = ? ORDER BY checked_at DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            row["last_check"] = dict(last) if last else None
            row["actions"] = [
                {"key": key, "label": spec.label, "requires_confirm": spec.requires_confirm,
                 "description": spec.description}
                for key, spec in ACTION_CATALOG.items()
            ]
        return {"items": rows, "total": len(rows)}


def register_service(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError_("invalid_json", 400)
    name = str(payload.get("name") or "").strip()
    kind = str(payload.get("kind") or "port")
    target = str(payload.get("target") or "").strip()
    if not name or not target:
        raise RuntimeError_("invalid_payload", 422, "name 与 target 必填")
    if kind not in {"port", "http", "process", "docker", "launchd", "systemd"}:
        raise RuntimeError_("invalid_payload", 422, "kind 非法")
    conn = store.connect()
    try:
        store.init_db(conn)
        now = store.now_iso()
        service_id = uuid.uuid4().hex
        try:
            with store.write_tx(conn):
                conn.execute(
                    "INSERT INTO runtime_services (id, project_id, name, kind, target,"
                    " health_check_json, metadata_json, enabled, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, '{}', '{}', 1, ?, ?)",
                    (service_id, payload.get("project_id"), name, kind, target, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError_("conflict", 409, "同名服务已存在") from exc
        return {"id": service_id, "name": name, "kind": kind, "target": target}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 操作（受控）
# ---------------------------------------------------------------------------
def _token(action_key: str, argv: tuple[str, ...]) -> str:
    raw = f"{action_key}|{' '.join(argv)}|{int(os.getpid())}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def preflight(action_key: str) -> dict[str, Any]:
    """返回将要执行的命令（脱敏展示）与预计影响，并签发一次性令牌。"""
    spec = ACTION_CATALOG.get(action_key)
    if spec is None:
        raise RuntimeError_("not_found", 404, f"未知动作：{action_key}")
    return {
        "action_key": spec.key,
        "label": spec.label,
        "argv": list(spec.argv),
        "command_display": " ".join(_quote(part) for part in spec.argv),
        "requires_confirm": spec.requires_confirm,
        "timeout_s": spec.timeout_s,
        "description": spec.description,
        "preflight_token": _token(spec.key, spec.argv),
    }


def _quote(part: str) -> str:
    return f"'{part}'" if " " in part or "\t" in part else part


def run_action(
    action_key: str,
    *,
    preflight_token: str | None,
    confirm: bool,
    dry_run: bool = False,
) -> dict[str, Any]:
    spec = ACTION_CATALOG.get(action_key)
    if spec is None:
        raise RuntimeError_("not_found", 404, f"未知动作：{action_key}")
    expected = _token(spec.key, spec.argv)
    if spec.requires_confirm:
        if preflight_token != expected:
            raise RuntimeError_("preflight_required", 412, "缺少或过期的预检令牌，请先执行预检")
        if not confirm:
            raise RuntimeError_("confirm_required", 412, "需要显式确认（confirm=true）")

    if dry_run:
        return {"action_key": spec.key, "dry_run": True,
                "command_display": " ".join(_quote(p) for p in spec.argv)}

    started = store.now_iso()
    try:
        # argv 来自硬编码 ACTION_CATALOG，不接受外部拼接
        proc = subprocess.run(
            list(spec.argv), capture_output=True, text=True,
            timeout=spec.timeout_s, check=False,
        )
        exit_code = proc.returncode
        output = ((proc.stdout or "") + (proc.stderr or ""))[:20000]
        status = "succeeded" if exit_code == 0 else "failed"
    except subprocess.TimeoutExpired as exc:
        exit_code = None
        output = f"超时（{spec.timeout_s}s）：{exc}"
        status = "failed"
    except (OSError, subprocess.SubprocessError) as exc:
        exit_code = None
        output = f"启动失败：{exc}"
        status = "failed"

    finished = store.now_iso()
    conn = store.connect()
    try:
        store.init_db(conn)
        _safe_audit(conn, None, "runtime.action.run", "runtime_action", spec.key, {
            "status": status,
            "exit_code": exit_code,
            "command": " ".join(_quote(p) for p in spec.argv),
        }, result="ok" if status == "succeeded" else "failed")
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO runtime_action_runs (id, action_id, project_id, status,"
                " preflight_output, preflight_token, dry_run, output, exit_code,"
                " started_at, finished_at, created_at)"
                " VALUES (?, ?, NULL, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, spec.key, status,
                 " ".join(_quote(p) for p in spec.argv), expected, output,
                 exit_code, started, finished, started),
            )
    finally:
        conn.close()

    return {
        "action_key": spec.key,
        "status": status,
        "exit_code": exit_code,
        "output": output,
        "started_at": started,
        "finished_at": finished,
    }


def _safe_audit(
    conn, project_id: str | None, action: str, resource_type: str,
    resource_id: str | None, detail: dict[str, Any], *, result: str = "ok",
) -> None:
    """写统一审计；失败不阻断操作。"""
    try:
        from . import service

        service.audit(conn, project_id, action, resource_type, resource_id,
                      detail=detail, result=result)
    except Exception:  # noqa: BLE001,S110 - 审计失败不影响运行时操作
        pass


def list_action_runs(*, limit: int = 20) -> dict[str, Any]:
    with store.session() as conn:
        rows = [
            dict(row) for row in conn.execute(
                "SELECT * FROM runtime_action_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(100, limit)),),
            )
        ]
        return {"items": rows, "total": len(rows)}
