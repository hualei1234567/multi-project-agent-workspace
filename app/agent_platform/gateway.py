"""Mac Gateway 设备命令队列（P3-M4 / ADR-P3-003）。

**定位**：平台侧的命令队列消费逻辑。Gateway 是**独立进程、只做出站连接**的长轮询
客户端（不开放公网入站），它按 ``device_id`` 周期性地：

1. ``poll_once`` 拉取 queued 命令 → 标记 SENT（"发给你了，还没认领"）；
2. ``claim_command`` 认领 → ACCEPTED（拿租约，此后才能执行）；
3. 本地跑完 → ``complete_command`` 写终态；
4. 平台侧另有 ``expire_stale`` 把过期未执行的标 EXPIRED。

**幂等为什么靠 SQL 条件更新**：设备可能重试、可能同时开两个 Gateway。
把「命令是不是已经被认领」的判断写进 ``UPDATE ... WHERE status = 'sent'``
的 where 里，只有一个线程/进程的 rowcount 会等于 1。先查后改在并发下必翻车。

**设备撤销检查是硬闸门**：撤销后不许执行任何命令。检查点放在 ``claim_command``
（唯一进入执行的入口）里，且读的是 ``registry_runtimes.status`` / Gateway 设备表，
不是调用方传进来的布尔值。

状态口径（``cli_adapter.CONTRACT_STATUSES``）：
CREATED / QUEUED / SENT / ACCEPTED / RUNNING / SUCCEEDED / FAILED / CANCELLED / EXPIRED / UNKNOWN。

**历史兼容**：``windows.py`` 的桥接仍用 ``pending → dispatched → done/failed``。
``device_commands.status`` 是同一列，因此这里把两套写法都认：
``pending`` 视为 QUEUED、``dispatched`` 视为 SENT、``done`` 视为 SUCCEEDED。
迁移既有行**不做**，因为改历史状态等于伪造证据。
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import cli_adapter, store

#: 默认命令有效期：超过仍未执行即 EXPIRED。2 小时够一次正常派发往返。
DEFAULT_COMMAND_TTL_S = 7200

#: 默认认领租约：设备认领后必须在此时限内回报结果，否则视为僵尸认领。
DEFAULT_LEASE_S = 900

#: ``poll_once`` 单次最多拉多少条（防止一次拉爆）。
MAX_POLL = 50

#: 允许作为终态写入的状态。只允许这四种——``complete_command`` 不能把命令改回运行中，
#: 否则「已结束的命令被复活」会成为可执行路径。
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}
)

#: 契约状态 → ``device_commands.status`` 的存量取值。
#:
#: 为什么不直接用大写契约名写库：表里已有 ``pending/dispatched/done/failed`` 的历史行，
#: 也还有桩接在按这些值查询。写库统一用小写词汇，读出去再映射成契约状态。
_DB_STATUS = {
    "CREATED": "created",
    "QUEUED": "pending",
    "SENT": "dispatched",
    "ACCEPTED": "accepted",
    "RUNNING": "running",
    "SUCCEEDED": "done",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
    "EXPIRED": "expired",
    "UNKNOWN": "unknown",
}

#: 可以「被拉取」的库内状态（还没有设备认领）。
_POLLABLE_DB_STATUSES: tuple[str, ...] = ("pending", "queued")

#: 可以被认领的库内状态。**只含「已下发但没被认领过」**——
#: ``accepted`` 故意不在里面：认领过的命令再认领一次就等于执行两次。
_CLAIMABLE_DB_STATUSES: tuple[str, ...] = ("dispatched", "sent")


class GatewayError(Exception):
    """Gateway 操作拒绝原因；HTTP 层可映射为同形错误体。"""

    def __init__(self, code: str, status: int = 409, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status
        self.message = message or code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _shift(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))
            ).isoformat(timespec="seconds")


def _row(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 设备登记与撤销
# ---------------------------------------------------------------------------
def hash_credential(credential: str) -> str:
    """凭据摘要。只存摘要，不存明文——泄漏了也还原不出来。"""
    return hashlib.sha256(str(credential or "").encode("utf-8")).hexdigest()


def register_device(conn: sqlite3.Connection, *, device_id: str, credential: str | None = None,
                    runtime_id: str | None = None, cli_version: str | None = None,
                    changed_by: str = "local") -> dict[str, Any]:
    """登记/更新 Gateway 设备。已存在则更新凭据与运行时时，不重置撤销状态。

    **撤销是粘性的**：撤销过的设备重新登记不会自动复活，必须显式 ``revoke_device``
    的反向操作（``reinstate_device``）——否则撤销形同虚设。
    """
    key = str(device_id or "").strip()
    if not key:
        raise GatewayError("invalid_device", 400, "缺少 device_id")
    now = _now()
    existing = _row(conn, "SELECT * FROM gateway_devices WHERE id = ?", (key,))
    if existing is None:
        with store.write_tx(conn):
            conn.execute(
                "INSERT INTO gateway_devices (id, runtime_id, credential_hash,"
                " credential_issued_at, last_seen_at, last_seen_ip, cli_version,"
                " status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, NULL, NULL, ?, 'active', ?, ?)",
                (key, runtime_id, hash_credential(credential) if credential else None,
                 now if credential else None, cli_version, now, now),
            )
    else:
        # 已撤销的设备：只更新上报字段，绝不改 status。
        with store.write_tx(conn):
            sets = ["updated_at = ?"]
            args: list[Any] = [now]
            if credential:
                sets += ["credential_hash = ?", "credential_issued_at = ?"]
                args += [hash_credential(credential), now]
            if runtime_id:
                sets.append("runtime_id = ?")
                args.append(runtime_id)
            if cli_version is not None:
                sets.append("cli_version = ?")
                args.append(cli_version)
            args.append(key)
            conn.execute(f"UPDATE gateway_devices SET {', '.join(sets)} WHERE id = ?",
                         tuple(args))
    return get_device(conn, key)


def get_device(conn: sqlite3.Connection, device_id: str) -> dict[str, Any]:
    row = _row(conn, "SELECT * FROM gateway_devices WHERE id = ?", (str(device_id or ""),))
    if row is None:
        raise GatewayError("device_not_found", 404, "设备未登记")
    # 凭据摘要不外泄给调用方（HTTP 层会原样返给前端）。
    row.pop("credential_hash", None)
    row["revoked"] = _device_revoked(conn, str(device_id), row=row)
    return row


def revoke_device(conn: sqlite3.Connection, device_id: str, *, reason: str = "",
                  changed_by: str = "local") -> dict[str, Any]:
    """撤销设备：Gateway 表与 ``registry_runtimes`` 同时置为不可用。

    两处都改是刻意的——只改一处就会出现「Gateway 认为已撤销、调度器认为可分配」，
    或者反过来。撤销后 ``claim_command`` 一律拒绝。
    """
    key = str(device_id or "").strip()
    if not key:
        raise GatewayError("invalid_device", 400, "缺少 device_id")
    now = _now()
    device = _row(conn, "SELECT * FROM gateway_devices WHERE id = ?", (key,))
    runtime_id = str((device or {}).get("runtime_id") or "")
    with store.write_tx(conn):
        if device is not None:
            conn.execute(
                "UPDATE gateway_devices SET status = 'revoked', updated_at = ? WHERE id = ?",
                (now, key),
            )
        if runtime_id:
            conn.execute(
                "UPDATE registry_runtimes SET status = 'revoked', allocatable = 0,"
                " updated_at = ? WHERE id = ?", (now, runtime_id),
            )
    if device is None and not runtime_id:
        raise GatewayError("device_not_found", 404, "设备未登记")
    return get_device(conn, key) if device is not None else {
        "id": key, "runtime_id": runtime_id, "status": "revoked", "revoked": True,
    }


def reinstate_device(conn: sqlite3.Connection, device_id: str, *,
                     changed_by: str = "local") -> dict[str, Any]:
    """解除撤销（显式动作）。运行时恢复为 ``unknown``，由下一次心跳推成 healthy。"""
    key = str(device_id or "").strip()
    device = _row(conn, "SELECT * FROM gateway_devices WHERE id = ?", (key,))
    if device is None:
        raise GatewayError("device_not_found", 404, "设备未登记")
    now = _now()
    runtime_id = str(device.get("runtime_id") or "")
    with store.write_tx(conn):
        conn.execute(
            "UPDATE gateway_devices SET status = 'active', updated_at = ? WHERE id = ?",
            (now, key),
        )
        if runtime_id:
            conn.execute(
                "UPDATE registry_runtimes SET status = 'unknown', allocatable = 1,"
                " updated_at = ? WHERE id = ?", (now, runtime_id),
            )
    return get_device(conn, key)


def _device_revoked(conn: sqlite3.Connection, device_id: str,
                    *, row: dict[str, Any] | None = None) -> bool:
    """设备是否已撤销。任一侧撤销即视为撤销（失败关闭）。"""
    device = row if row is not None else _row(
        conn, "SELECT * FROM gateway_devices WHERE id = ?", (device_id,)
    )
    if device is not None and str(device.get("status") or "") == "revoked":
        return True
    runtime_id = str((device or {}).get("runtime_id") or "")
    if not runtime_id:
        return False
    rt = _row(conn, "SELECT status FROM registry_runtimes WHERE id = ?", (runtime_id,))
    return bool(rt is not None and str(rt.get("status") or "") == "revoked")


def verify_credential(conn: sqlite3.Connection, device_id: str, credential: str) -> bool:
    """校验凭据。未登记凭据的设备一律拒绝（不因「没设过密码」就放行）。"""
    row = _row(conn, "SELECT credential_hash FROM gateway_devices WHERE id = ?",
               (str(device_id or ""),))
    if row is None:
        return False
    expected = str(row.get("credential_hash") or "")
    if not expected:
        return False
    return hashlib.sha256(str(credential or "").encode("utf-8")).hexdigest() == expected


def touch_device(conn: sqlite3.Connection, device_id: str, *,
                 ip: str | None = None, cli_version: str | None = None) -> None:
    """记录一次出站拉取（运维可见性）。设备不存在时静默跳过，不自动建号。"""
    sets = ["last_seen_at = ?", "updated_at = ?"]
    args: list[Any] = [_now(), _now()]
    if ip:
        sets.append("last_seen_ip = ?")
        args.append(ip)
    if cli_version is not None:
        sets.append("cli_version = ?")
        args.append(cli_version)
    args.append(str(device_id or ""))
    with store.write_tx(conn):
        conn.execute(f"UPDATE gateway_devices SET {', '.join(sets)} WHERE id = ?", tuple(args))


# ---------------------------------------------------------------------------
# 命令队列：拉取 / 认领 / 完成 / 过期
# ---------------------------------------------------------------------------
def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    """补充契约字段，但不删原始列（排查时还要看 status / error）。"""
    out = dict(row)
    out["contract_status"] = cli_adapter.map_status(row.get("status"))
    return out


def poll_once(conn: sqlite3.Connection, device_id: str, *,
              limit: int = MAX_POLL, lease_seconds: int = DEFAULT_LEASE_S) -> dict[str, Any]:
    """拉取该设备 queued 命令并标记 SENT。

    **撤销设备不能拉取**：撤销后连命令内容都不该继续下发。
    过期命令在拉取时顺手标 EXPIRED，不再下发。
    """
    key = str(device_id or "").strip()
    if not key:
        raise GatewayError("invalid_device", 400, "缺少 device_id")
    if _device_revoked(conn, key):
        raise GatewayError("device_revoked", 409, "设备已撤销，不能拉取命令")

    limit = max(1, min(int(limit or MAX_POLL), MAX_POLL))
    now = _now()
    expire_stale(conn, device_id=key)
    placeholders = ", ".join("?" * len(_POLLABLE_DB_STATUSES))
    rows = conn.execute(
        f"SELECT * FROM device_commands WHERE device_id = ? AND status IN ({placeholders})"
        " ORDER BY created_at ASC LIMIT ?",
        (key, *_POLLABLE_DB_STATUSES, limit),
    ).fetchall()

    taken: list[dict[str, Any]] = []
    sent_status = _DB_STATUS["SENT"]
    for row in rows:
        with store.write_tx(conn):
            # 条件更新：只有仍是可拉取状态时才改成 SENT，防止两个 Gateway 抢同一条。
            changed = conn.execute(
                "UPDATE device_commands SET status = ?, started_at = COALESCE(started_at, ?),"
                " lease_expires_at = ? WHERE id = ? AND status IN"
                f" ({placeholders})",
                (sent_status, now, _shift(lease_seconds), row["id"],
                 *_POLLABLE_DB_STATUSES),
            ).rowcount
        if changed == 1:
            fresh = _row(conn, "SELECT * FROM device_commands WHERE id = ?", (row["id"],))
            taken.append(_decorate(fresh or dict(row)))
    touch_device(conn, key)
    return {"device_id": key, "commands": taken, "count": len(taken)}


def claim_command(conn: sqlite3.Connection, command_id: str, device_id: str, *,
                  lease_seconds: int = DEFAULT_LEASE_S) -> dict[str, Any]:
    """认领命令 → ACCEPTED。**唯一进入执行的入口**。

    幂等：只有 SENT 状态才能认领成功（``UPDATE ... WHERE status='dispatched'``）。
    第二次认领同一条命令会得到 ``already_claimed``，不会执行两次。

    撤销检查放在最前面且不看调用方参数——设备撤销后必须拒绝。
    """
    cid = str(command_id or "").strip()
    did = str(device_id or "").strip()
    if not cid or not did:
        raise GatewayError("invalid_payload", 400, "缺少 command_id 或 device_id")
    if _device_revoked(conn, did):
        # 注意：这里不写库。命令保持原状，等平台侧统一处理。
        raise GatewayError("device_revoked", 409, "设备已撤销，禁止执行命令")

    row = _row(conn, "SELECT * FROM device_commands WHERE id = ?", (cid,))
    if row is None:
        raise GatewayError("not_found", 404, "命令不存在")
    if str(row.get("device_id") or "") != did:
        # 命令是发给别的设备的：既不认领也不泄露内容。
        raise GatewayError("wrong_device", 409, "该命令不属于本设备")

    current = str(row.get("status") or "")
    if current not in _CLAIMABLE_DB_STATUSES:
        contract = cli_adapter.map_status(current)
        if cli_adapter.is_terminal(contract):
            raise GatewayError("already_terminal", 409,
                               f"命令已处于终态 {contract}，不能再次认领")
        raise GatewayError("not_claimable", 409, f"命令当前状态 {contract} 不可认领")

    accepted_status = _DB_STATUS["ACCEPTED"]
    now = _now()
    placeholders = ", ".join("?" * len(_CLAIMABLE_DB_STATUSES))
    with store.write_tx(conn):
        changed = conn.execute(
            "UPDATE device_commands SET status = ?, started_at = COALESCE(started_at, ?),"
            " lease_expires_at = ?, attempt = attempt + 1"
            f" WHERE id = ? AND device_id = ? AND status IN ({placeholders})",
            (accepted_status, now, _shift(lease_seconds), cid, did,
             *_CLAIMABLE_DB_STATUSES),
        ).rowcount
    if changed != 1:
        # 并发下被别的实例抢先认领。
        fresh = _row(conn, "SELECT * FROM device_commands WHERE id = ?", (cid,))
        raise GatewayError(
            "already_claimed", 409,
            f"命令已被认领（当前 {cli_adapter.map_status((fresh or {}).get('status'))}）",
        )
    fresh = _row(conn, "SELECT * FROM device_commands WHERE id = ?", (cid,))
    return _decorate(fresh or {})


def complete_command(conn: sqlite3.Connection, command_id: str, *,
                    status: str, result: str | None = None,
                    error: str | None = None,
                    device_id: str | None = None) -> dict[str, Any]:
    """写终态。只接受 SUCCEEDED / FAILED / CANCELLED / EXPIRED。

    **终态不可改**：已经是终态的命令再调一次直接返回原样（幂等），
    不覆盖第一次的真实结果——覆盖等于篡改证据。
    """
    cid = str(command_id or "").strip()
    if not cid:
        raise GatewayError("invalid_payload", 400, "缺少 command_id")
    contract = str(status or "").strip().upper()
    if contract not in TERMINAL_STATUSES:
        raise GatewayError(
            "invalid_status", 400,
            f"complete_command 只接受终态 {sorted(TERMINAL_STATUSES)}，收到 {status}",
        )
    row = _row(conn, "SELECT * FROM device_commands WHERE id = ?", (cid,))
    if row is None:
        raise GatewayError("not_found", 404, "命令不存在")
    if device_id and str(row.get("device_id") or "") != str(device_id).strip():
        raise GatewayError("wrong_device", 409, "该命令不属于本设备")

    current_contract = cli_adapter.map_status(row.get("status"))
    if current_contract in cli_adapter.TERMINAL_STATUSES:
        # 幂等返回；若结论不一致，如实报冲突，不静默改库。
        if current_contract != contract:
            raise GatewayError(
                "already_terminal", 409,
                f"命令已终结为 {current_contract}，不能改写为 {contract}",
            )
        return _decorate(row)

    db_status = _DB_STATUS[contract]
    now = _now()
    # 只有非终态的库内状态才能被写成终态（并发下防止覆盖别人的终态）。
    non_terminal = [v for k, v in _DB_STATUS.items()
                    if k not in cli_adapter.TERMINAL_STATUSES and k != "UNKNOWN"]
    placeholders = ", ".join("?" * len(non_terminal))
    with store.write_tx(conn):
        changed = conn.execute(
            "UPDATE device_commands SET status = ?, finished_at = ?,"
            " result = COALESCE(?, result), error = COALESCE(?, error),"
            " error_summary = ?, lease_expires_at = NULL"
            f" WHERE id = ? AND status IN ({placeholders})",
            (db_status, now,
             (result or "")[:4000] or None,
             (error or "")[:2000] or None,
             (error or "")[:200] or None,
             cid, *non_terminal),
        ).rowcount
    if changed != 1:
        fresh = _row(conn, "SELECT * FROM device_commands WHERE id = ?", (cid,))
        raise GatewayError(
            "already_terminal", 409,
            f"命令已终结为 {cli_adapter.map_status((fresh or {}).get('status'))}",
        )
    fresh = _row(conn, "SELECT * FROM device_commands WHERE id = ?", (cid,))
    return _decorate(fresh or {})


def expire_stale(conn: sqlite3.Connection, *, device_id: str | None = None,
                 now: str | None = None) -> dict[str, Any]:
    """把超过有效期仍未执行的命令标 EXPIRED。

    「未执行」的口径：还停在可拉取状态（pending/queued）**或**已下发（dispatched）
    但从未被认领（``attempt = 0``）。已经认领过的命令即使租约过期也不在这里改——
    设备可能正在跑，把它标成 EXPIRED 会与真实执行结果打架；那类僵尸认领留给
    ``list_zombie_claims`` 暴露给运维，不自动改状态。
    """
    now = now or _now()
    expired_status = _DB_STATUS["EXPIRED"]
    sql = (
        "SELECT * FROM device_commands WHERE finished_at IS NULL"
        " AND (status IN ('pending', 'queued', 'created')"
        "      OR (status IN ('dispatched', 'sent') AND COALESCE(attempt, 0) = 0))"
        # expires_at 为空时按 created_at + TTL 判定
        # 两侧都过 sqlite datetime()：created_at/expires_at 带 +00:00 时区后缀，
        # 直接字符串比较会因为后缀不同而误判。
        " AND datetime(COALESCE(expires_at, datetime(created_at, ?))) <= datetime(?)"
    )
    args: list[Any] = [f"+{int(DEFAULT_COMMAND_TTL_S)} seconds", now]
    if device_id:
        sql += " AND device_id = ?"
        args.append(str(device_id))
    rows = conn.execute(sql, tuple(args)).fetchall()
    expired: list[str] = []
    for row in rows:
        with store.write_tx(conn):
            changed = conn.execute(
                "UPDATE device_commands SET status = ?, finished_at = ?,"
                " error_summary = COALESCE(error_summary, ?)"
                " WHERE id = ? AND finished_at IS NULL",
                (expired_status, now, "超过有效期未执行，已过期", row["id"]),
            ).rowcount
        if changed == 1:
            expired.append(row["id"])
    return {"expired": expired, "count": len(expired), "at": now}


def list_zombie_claims(conn: sqlite3.Connection, *, now: str | None = None,
                       device_id: str | None = None) -> list[dict[str, Any]]:
    """认领后租约过期但仍未回报的命令（僵尸认领）。**只读，不改状态**。

    为什么不自动改：设备可能只是回报慢。贸然标 EXPIRED 会把真实执行结果盖掉，
    而平台看不到执行证据。这里交给运维/上层决定是否强制终结。
    """
    now = now or _now()
    sql = (
        "SELECT * FROM device_commands WHERE finished_at IS NULL"
        " AND status IN ('accepted', 'running')"
        " AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?"
    )
    args: list[Any] = [now]
    if device_id:
        sql += " AND device_id = ?"
        args.append(str(device_id))
    sql += " ORDER BY lease_expires_at ASC"
    return [_decorate(dict(row)) for row in conn.execute(sql, tuple(args))]


def queue_summary(conn: sqlite3.Connection, device_id: str) -> dict[str, Any]:
    """队列概览：按库内状态计数 + 契约状态计数。"""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM device_commands WHERE device_id = ?"
        " GROUP BY status", (str(device_id or ""),)
    ).fetchall()
    by_db = {str(r["status"]): int(r["n"]) for r in rows}
    by_contract: dict[str, int] = {}
    for db_status, count in by_db.items():
        contract = cli_adapter.map_status(db_status)
        by_contract[contract] = by_contract.get(contract, 0) + count
    return {"device_id": str(device_id or ""), "by_status": by_db,
            "by_contract_status": by_contract}
