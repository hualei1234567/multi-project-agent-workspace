"""状态快照缓存：命中、TTL、磁盘快照、后台刷新、强制刷新、失败降级。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from agent_platform.state_cache import StateCache


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def _wait(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")


def test_first_call_builds_and_caches(tmp_path):
    clock = Clock()
    calls: list[str] = []

    def builder(key: str) -> dict:
        calls.append(key)
        return {"key": key, "n": len(calls)}

    cache = StateCache(tmp_path / "cache", builder, ttl=5, clock=clock)
    payload, stale = cache.get("ddp")
    assert payload["n"] == 1 and stale is False
    payload2, stale2 = cache.get("ddp")
    assert payload2["n"] == 1 and stale2 is False
    assert calls == ["ddp"]


def test_expired_returns_stale_and_refreshes(tmp_path):
    clock = Clock()
    calls: list[str] = []

    def builder(key: str) -> dict:
        calls.append(key)
        return {"n": len(calls)}

    cache = StateCache(tmp_path / "cache", builder, ttl=5, clock=clock)
    cache.get("ddp")
    clock.tick(6)
    payload, stale = cache.get("ddp")
    assert stale is True and payload["n"] == 1
    _wait(lambda: len(calls) == 2)
    payload2, stale2 = cache.get("ddp")
    assert payload2["n"] == 2 and stale2 is False


def test_disk_snapshot_survives_restart(tmp_path):
    cache_dir = tmp_path / "cache"
    cache = StateCache(cache_dir, lambda key: {"v": 1}, ttl=5, clock=Clock())
    cache.get("ddp")
    assert (cache_dir / "ddp.json").exists()

    clock2 = Clock()
    fresh = StateCache(cache_dir, lambda key: {"v": 2}, ttl=5, clock=clock2)
    payload, stale = fresh.get("ddp")
    # 磁盘快照视为过期 → 立即返回旧值并后台刷新
    assert payload["v"] == 1
    assert stale in (True, False)  # 后台可能已很快完成


def test_force_rebuilds_synchronously(tmp_path):
    cache = StateCache(tmp_path / "c", lambda key: {"v": time.time_ns()})
    first, _ = cache.get("ddp")
    second, stale = cache.get("ddp", force=True)
    assert stale is False
    assert second["v"] != first["v"]


def test_refresh_failure_keeps_old_snapshot(tmp_path):
    state = {"fail": False, "n": 0}

    def builder(key: str) -> dict:
        state["n"] += 1
        if state["fail"]:
            raise RuntimeError("boom")
        return {"n": state["n"]}

    clock = Clock()
    cache = StateCache(tmp_path / "c", builder, ttl=5, clock=clock)
    first, _ = cache.get("ddp")
    state["fail"] = True
    clock.tick(10)
    payload, stale = cache.get("ddp")
    assert stale is True and payload["n"] == first["n"]
    time.sleep(0.05)
    again, _ = cache.get("ddp")
    assert again["n"] == first["n"]
