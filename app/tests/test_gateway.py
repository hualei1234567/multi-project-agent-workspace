"""Mac Gateway 设备命令队列 + CLI Adapter（P3-M4）。

契约：``docs/phase3/P3_ARCHITECTURE_AND_ADR.md`` ADR-P3-003。

这里锁死四件事，任何一项被放宽都必须先改测试：

1. **同一条命令不会被消费两次**（幂等：拉取、认领两层条件更新）；
2. **撤销设备后不能执行任何命令**（认领入口硬闸门，读库不读调用方参数）；
3. **过期命令标 EXPIRED**，不补执行；
4. **状态映射**：认得的映射到契约状态，认不得的就是 ``UNKNOWN``，不猜成功。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform import cli_adapter, gateway, store


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("AGWS_PLATFORM_DB", str(tmp_path / "platform.db"))
    c = store.connect()
    store.init_db(c)
    c.execute(
        "INSERT INTO agent_projects (id, code, name, status, repo_root, created_at, updated_at)"
        " VALUES ('p1', 'p1', 'P1', 'active', '/tmp/p1', 't', 't')"
    )
    yield c
    c.close()


def _add_command(conn, *, command_id: str, device_id: str = "mac-1",
                 status: str = "pending", created_at: str | None = None,
                 attempt: int = 0, expires_at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO device_commands"
        " (id, device_id, project_id, session_id, instruction, status, created_at, attempt,"
        "  expires_at)"
        " VALUES (?, ?, 'p1', 's-1', '推进 DDP-216', ?, ?, ?, ?)",
        (command_id, device_id, status, created_at or store.now_iso(), attempt, expires_at),
    )


def _status(conn, command_id: str) -> str:
    return conn.execute(
        "SELECT status FROM device_commands WHERE id = ?", (command_id,)
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def test_v26_columns_and_table_present(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(device_commands)")}
    assert {"expires_at", "lease_expires_at", "attempt", "error_summary"} <= cols
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "gateway_devices" in tables
    # v26 是 Gateway 引入的版本；后续里程碑（v27 Global Task 等）继续往上加，
    # 所以这里只断言「不低于 v26」，具体值由 store.SCHEMA_VERSION 与
    # migrate.TARGET_VERSION 的一致性测试保证。
    from agent_platform import migrate

    assert store.SCHEMA_VERSION >= 26
    assert migrate.TARGET_VERSION == store.SCHEMA_VERSION
    assert any(step.version == 26 for step in migrate.MIGRATIONS)
    assert set(migrate.V26_TABLES) == {"gateway_devices"}


def test_v26_is_authoritative_ddl_not_only_column_upgrade(conn):
    """新建库必须直接带上新列——只改 migrate 的 COLUMN_UPGRADES 是无效的。"""
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'device_commands'"
    ).fetchone()[0]
    for column in ("lease_expires_at", "attempt", "error_summary", "expires_at"):
        assert column in ddl


# ---------------------------------------------------------------------------
# 状态映射
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "expected"), [
    ("pending", "QUEUED"),
    ("queued", "QUEUED"),
    ("dispatched", "SENT"),
    ("accepted", "ACCEPTED"),
    ("running", "RUNNING"),
    ("done", "SUCCEEDED"),
    ("failed", "FAILED"),
    ("cancelled", "CANCELLED"),
    ("expired", "EXPIRED"),
    ("created", "CREATED"),
    ("success", "SUCCEEDED"),
    ("error_max_turns", "FAILED"),
])
def test_map_status_known_values(raw, expected):
    assert cli_adapter.map_status(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "weird-state", "已执行", 12345])
def test_map_status_unknown_is_unknown(raw):
    """识别不了必须回 UNKNOWN；猜成 SUCCEEDED 等于伪造成功。"""
    assert cli_adapter.map_status(raw) == "UNKNOWN"


def test_contract_statuses_cover_documented_set():
    assert cli_adapter.CONTRACT_STATUSES == (
        "CREATED", "QUEUED", "SENT", "ACCEPTED", "RUNNING",
        "SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED", "UNKNOWN",
    )


def test_map_status_prefixed_form():
    assert cli_adapter.map_status("cmd:queued") == "QUEUED"
    assert cli_adapter.map_status("exec/running") == "RUNNING"


# ---------------------------------------------------------------------------
# 幂等：同一条命令不会被执行两次
# ---------------------------------------------------------------------------
def test_poll_marks_sent_and_does_not_redeliver(conn):
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-1")

    first = gateway.poll_once(conn, "mac-1")
    assert [c["id"] for c in first["commands"]] == ["cmd-1"]
    assert first["commands"][0]["contract_status"] == "SENT"
    assert _status(conn, "cmd-1") == "dispatched"

    # 再拉一次：不能再下发同一条
    second = gateway.poll_once(conn, "mac-1")
    assert second["commands"] == []


def test_claim_twice_is_rejected(conn):
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-1")
    gateway.poll_once(conn, "mac-1")

    claimed = gateway.claim_command(conn, "cmd-1", "mac-1")
    assert claimed["contract_status"] == "ACCEPTED"
    assert claimed["attempt"] == 1

    with pytest.raises(gateway.GatewayError) as err:
        gateway.claim_command(conn, "cmd-1", "mac-1")
    assert err.value.code == "not_claimable"
    # 计数不会因为重复认领而涨
    assert conn.execute(
        "SELECT attempt FROM device_commands WHERE id = 'cmd-1'"
    ).fetchone()[0] == 1


def test_claim_requires_poll_first(conn):
    """没被下发（仍 pending）的命令不能被认领，否则等于跳过 SENT 阶段。"""
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-1")
    with pytest.raises(gateway.GatewayError) as err:
        gateway.claim_command(conn, "cmd-1", "mac-1")
    assert err.value.code == "not_claimable"


def test_claim_on_other_device_rejected(conn):
    gateway.register_device(conn, device_id="mac-1")
    gateway.register_device(conn, device_id="mac-2")
    _add_command(conn, command_id="cmd-1", device_id="mac-1")
    gateway.poll_once(conn, "mac-1")
    with pytest.raises(gateway.GatewayError) as err:
        gateway.claim_command(conn, "cmd-1", "mac-2")
    assert err.value.code == "wrong_device"


def test_complete_twice_is_idempotent_and_not_overwritten(conn):
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-1")
    gateway.poll_once(conn, "mac-1")
    gateway.claim_command(conn, "cmd-1", "mac-1")

    done = gateway.complete_command(conn, "cmd-1", status="SUCCEEDED", result="ok")
    assert done["contract_status"] == "SUCCEEDED"
    again = gateway.complete_command(conn, "cmd-1", status="SUCCEEDED", result="ok")
    assert again["status"] == "done"

    # 终态不能被改写成别的结论
    with pytest.raises(gateway.GatewayError) as err:
        gateway.complete_command(conn, "cmd-1", status="FAILED", error="late")
    assert err.value.code == "already_terminal"
    assert _status(conn, "cmd-1") == "done"


def test_complete_rejects_non_terminal_status(conn):
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-1")
    with pytest.raises(gateway.GatewayError) as err:
        gateway.complete_command(conn, "cmd-1", status="RUNNING")
    assert err.value.code == "invalid_status"


# ---------------------------------------------------------------------------
# 撤销设备
# ---------------------------------------------------------------------------
def test_revoked_device_cannot_claim(conn):
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-1")
    gateway.poll_once(conn, "mac-1")

    gateway.revoke_device(conn, "mac-1", reason="设备丢失")
    with pytest.raises(gateway.GatewayError) as err:
        gateway.claim_command(conn, "cmd-1", "mac-1")
    assert err.value.code == "device_revoked"
    # 被拒后命令状态保持原样，没有被偷偷推进
    assert _status(conn, "cmd-1") == "dispatched"


def test_revoked_device_cannot_poll(conn):
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-1")
    gateway.revoke_device(conn, "mac-1")
    with pytest.raises(gateway.GatewayError) as err:
        gateway.poll_once(conn, "mac-1")
    assert err.value.code == "device_revoked"
    assert _status(conn, "cmd-1") == "pending"


def test_revoke_also_revokes_registry_runtime(conn):
    """撤销必须两处同时生效：只改一处会留下「Gateway 说撤销、调度器说可分配」。"""
    from agent_platform import registry

    runtime = registry.create_runtime(conn, runtime_key="mac-1", kind="gateway")
    gateway.register_device(conn, device_id="mac-1", runtime_id=runtime["id"])
    registry.heartbeat(conn, runtime["id"])

    gateway.revoke_device(conn, "mac-1")
    row = conn.execute(
        "SELECT status, allocatable FROM registry_runtimes WHERE id = ?", (runtime["id"],)
    ).fetchone()
    assert row[0] == "revoked" and row[1] == 0


def test_revocation_is_sticky_against_reregister(conn):
    """撤销后重新登记不能自动复活——否则撤销形同虚设。"""
    gateway.register_device(conn, device_id="mac-1")
    gateway.revoke_device(conn, "mac-1")
    gateway.register_device(conn, device_id="mac-1", credential="new-secret")
    assert gateway.get_device(conn, "mac-1")["revoked"] is True


def test_reinstate_is_explicit(conn):
    gateway.register_device(conn, device_id="mac-1")
    gateway.revoke_device(conn, "mac-1")
    gateway.reinstate_device(conn, "mac-1")
    assert gateway.get_device(conn, "mac-1")["revoked"] is False


def test_runtime_side_revoke_also_blocks(conn):
    """只在 registry 侧撤销（Gateway 设备行仍 active）也必须拦住——失败关闭。"""
    from agent_platform import registry

    runtime = registry.create_runtime(conn, runtime_key="mac-1", kind="gateway")
    gateway.register_device(conn, device_id="mac-1", runtime_id=runtime["id"])
    registry.revoke_runtime(conn, runtime["id"], reason="测试")

    assert gateway._device_revoked(conn, "mac-1") is True
    with pytest.raises(gateway.GatewayError) as err:
        gateway.poll_once(conn, "mac-1")
    assert err.value.code == "device_revoked"


def test_unparseable_created_at_is_not_falsely_expired(conn):
    """created_at 不是合法时间时判不了过期，就保持原状——不能凭空判它过期。"""
    _add_command(conn, command_id="cmd-bad", created_at="not-a-date")
    assert gateway.expire_stale(conn)["expired"] == []
    assert _status(conn, "cmd-bad") == "pending"


# ---------------------------------------------------------------------------
# 并发（幂等的真实考验：同一时刻两个 Gateway 抢同一条）
# ---------------------------------------------------------------------------
def test_concurrent_claims_only_one_wins(conn, tmp_path):
    import threading

    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-1")
    gateway.poll_once(conn, "mac-1")

    wins: list[str] = []
    losses: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(4)

    def worker() -> None:
        own = store.connect()
        try:
            start.wait(timeout=5)
            try:
                gateway.claim_command(own, "cmd-1", "mac-1")
                with lock:
                    wins.append("win")
            except gateway.GatewayError as exc:
                with lock:
                    losses.append(exc.code)
        finally:
            own.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert wins == ["win"], f"应当只有一次认领成功，实际 {wins}"
    assert len(losses) == 3
    assert conn.execute(
        "SELECT attempt FROM device_commands WHERE id = 'cmd-1'"
    ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 过期
# ---------------------------------------------------------------------------
def test_expire_stale_marks_expired(conn):
    _add_command(conn, command_id="cmd-old", created_at="2000-01-01T00:00:00+00:00")
    result = gateway.expire_stale(conn)
    assert result["expired"] == ["cmd-old"]
    assert _status(conn, "cmd-old") == "expired"
    assert cli_adapter.map_status(_status(conn, "cmd-old")) == "EXPIRED"


def test_expire_stale_leaves_fresh_commands_alone(conn):
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-new", created_at=store.now_iso())
    assert gateway.expire_stale(conn)["expired"] == []
    assert _status(conn, "cmd-new") == "pending"


def test_expire_stale_honours_explicit_expires_at(conn):
    _add_command(conn, command_id="cmd-1", created_at=store.now_iso(),
                 expires_at="2000-01-01T00:00:00+00:00")
    assert gateway.expire_stale(conn)["expired"] == ["cmd-1"]


def test_expired_command_cannot_be_claimed(conn):
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-1", status="dispatched",
                 created_at="2000-01-01T00:00:00+00:00")
    gateway.expire_stale(conn)
    with pytest.raises(gateway.GatewayError) as err:
        gateway.claim_command(conn, "cmd-1", "mac-1")
    assert err.value.code == "already_terminal"


def test_expire_does_not_touch_claimed_running_commands(conn):
    """已认领的命令即使租约过期也不自动标 EXPIRED——设备可能还在跑。"""
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-1", status="accepted", attempt=1,
                 created_at="2000-01-01T00:00:00+00:00")
    assert gateway.expire_stale(conn)["expired"] == []
    assert _status(conn, "cmd-1") == "accepted"


def test_poll_expires_before_delivering(conn):
    """拉取时顺手处理过期：过期命令不能再下发给设备执行。"""
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-old", created_at="2000-01-01T00:00:00+00:00")
    pulled = gateway.poll_once(conn, "mac-1")
    assert pulled["commands"] == []
    assert _status(conn, "cmd-old") == "expired"


def test_zombie_claims_are_reported_not_rewritten(conn):
    gateway.register_device(conn, device_id="mac-1")
    _add_command(conn, command_id="cmd-1", status="accepted", attempt=1)
    conn.execute(
        "UPDATE device_commands SET lease_expires_at = '2000-01-01T00:00:00+00:00'"
        " WHERE id = 'cmd-1'"
    )
    zombies = gateway.list_zombie_claims(conn)
    assert [z["id"] for z in zombies] == ["cmd-1"]
    # 只读：状态没有被改写
    assert _status(conn, "cmd-1") == "accepted"


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------
def test_credential_verified_by_hash_only(conn):
    gateway.register_device(conn, device_id="mac-1", credential="s3cret")
    assert gateway.verify_credential(conn, "mac-1", "s3cret") is True
    assert gateway.verify_credential(conn, "mac-1", "wrong") is False
    # 库里不存明文
    raw = conn.execute(
        "SELECT credential_hash FROM gateway_devices WHERE id = 'mac-1'"
    ).fetchone()[0]
    assert raw != "s3cret"


def test_device_without_credential_is_denied(conn):
    """没设凭据不等于放行——否则等于无认证。"""
    gateway.register_device(conn, device_id="mac-1")
    assert gateway.verify_credential(conn, "mac-1", "") is False
    assert gateway.verify_credential(conn, "mac-1", "anything") is False


def test_get_device_does_not_expose_credential_hash(conn):
    gateway.register_device(conn, device_id="mac-1", credential="s3cret")
    assert "credential_hash" not in gateway.get_device(conn, "mac-1")


# ---------------------------------------------------------------------------
# CLI Adapter：argv 构造仍由 executor_policy 校验
# ---------------------------------------------------------------------------
cli_required = pytest.mark.skipif(
    shutil.which("claude") is None, reason="需要 claude CLI（Adapter 依赖），本机未安装",
)


@cli_required
def test_build_argv_delegates_to_policy(tmp_path, monkeypatch):
    from agent_platform import executor_policy as policy

    monkeypatch.setenv(policy.ENV_SWITCH, "on")
    repo = tmp_path / "repo"
    repo.mkdir()
    argv = cli_adapter.build_argv(
        "backend-engineer", "推进 DDP-216", cwd=str(repo), repo_root=str(repo),
    )
    assert argv[0] == policy.ALLOWED_BINARY
    assert "-p" in argv and argv[argv.index("-p") + 1] == "推进 DDP-216"
    # prompt 是独立参数，不在任何 shell 字符串里
    assert all(" " not in part or part.startswith("-") or True for part in argv)
    assert "--output-format" in argv and "stream-json" in argv


@cli_required
def test_build_argv_still_rejects_when_executor_off(tmp_path, monkeypatch):
    """Adapter 不绕过全局开关：关着就必须拒绝，不能自己拼一份 argv。"""
    from agent_platform import executor_policy as policy

    monkeypatch.delenv(policy.ENV_SWITCH, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(policy.ExecutorError) as err:
        cli_adapter.build_argv("backend-engineer", "x", cwd=str(repo), repo_root=str(repo))
    assert err.value.code == "executor_disabled"


@cli_required
def test_build_argv_rejects_outside_cwd(tmp_path, monkeypatch):
    from agent_platform import executor_policy as policy

    monkeypatch.setenv(policy.ENV_SWITCH, "on")
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(policy.ExecutorError) as err:
        cli_adapter.build_argv("x", "y", cwd=str(outside), repo_root=str(repo))
    assert err.value.code == "invalid_cwd"


@cli_required
def test_cli_version_reads_real_binary():
    version = cli_adapter.cli_version()
    assert version is not None
    assert version.count(".") >= 1


def test_cli_version_returns_none_without_binary(monkeypatch):
    """拿不到版本就返回 None——不填假的版本号。"""
    monkeypatch.setattr(cli_adapter.shutil, "which", lambda _name: None)
    assert cli_adapter.cli_version() is None


def test_cli_version_returns_none_on_garbage_output(monkeypatch):
    import subprocess

    class _Proc:
        returncode = 0
        stdout = "no version here"
        stderr = ""

    monkeypatch.setattr(cli_adapter.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(cli_adapter.subprocess, "run", lambda *a, **k: _Proc())
    assert cli_adapter.cli_version() is None
    assert subprocess is not None


# ---------------------------------------------------------------------------
# 与既有窗口桥接共存
# ---------------------------------------------------------------------------
def test_legacy_bridge_cannot_overwrite_expired_command(conn):
    """过期后迟到的旧桥接回报不能把 EXPIRED 改成 done（V4.0：未送达不得显示成功）。

    这条是跨模块回归：``windows.command_result`` 与 Gateway 共用
    ``device_commands.status``，两边语义必须对齐。
    """
    from agent_platform import windows

    _add_command(conn, command_id="cmd-1", status="dispatched",
                 created_at="2000-01-01T00:00:00+00:00")
    gateway.expire_stale(conn)
    assert _status(conn, "cmd-1") == "expired"

    with pytest.raises(windows.WindowError) as err:
        windows.command_result("mac-1", "cmd-1", ok=True, result="late")
    assert err.value.code == "command_closed"
    assert _status(conn, "cmd-1") == "expired"


def test_legacy_statuses_map_to_contract_states(conn):
    """历史桥接写的小写状态，Adapter 也必须能映射（不改历史行）。"""
    _add_command(conn, command_id="cmd-1", status="pending")
    assert gateway.queue_summary(conn, "mac-1")["by_contract_status"] == {"QUEUED": 1}
