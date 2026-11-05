"""平台状态快照缓存（stale-while-revalidate）。

背景：``/api/state`` 需要扫描本机 transcript / git / 看板文件，冷启动约 10s。
本模块提供「内存 TTL + 磁盘快照 + 后台单飞刷新」：

- 内存命中且未过期 → 直接返回；
- 内存过期或进程刚启动（磁盘快照存在）→ 立即返回旧快照，并在后台刷新；
- 完全没有缓存 → 同步构建（首次请求）；
- ``force`` → 同步重建（供 ?refresh=1）；
- 刷新失败保留旧快照，不向上抛错。

缓存键由调用方给出（当前为项目 code）。磁盘目录默认位于平台库同级 ``state-cache/``。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class StateCache:
    def __init__(
        self,
        cache_dir: Path | str,
        builder: Callable[[str], dict[str, Any]],
        *,
        ttl: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.builder = builder
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._memory: dict[str, dict[str, Any]] = {}
        self._inflight: set[str] = set()

    # ------------------------------------------------------------------ public
    def get(self, key: str, *, force: bool = False) -> tuple[dict[str, Any], bool]:
        """返回 (payload, stale)。builder 抛错时：有旧快照返回旧快照，否则继续抛。"""
        if force:
            payload = self._build(key)
            return payload, False

        now = self._clock()
        with self._lock:
            entry = self._memory.get(key)
            if entry is None:
                entry = self._load_disk(key)
                if entry is not None:
                    self._memory[key] = entry
            fresh = entry is not None and (now - entry["computed_at"]) < self.ttl
            if fresh:
                return entry["payload"], False
            had_cache = entry is not None
            payload = entry["payload"] if entry else None

        if had_cache:
            self._refresh_async(key)
            return payload, True

        payload = self._build(key)
        return payload, False

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._memory.clear()
            else:
                self._memory.pop(key, None)

    # ----------------------------------------------------------------- internal
    def _build(self, key: str) -> dict[str, Any]:
        payload = self.builder(key)
        entry = {"payload": payload, "computed_at": self._clock()}
        with self._lock:
            self._memory[key] = entry
        self._write_disk(key, entry)
        return payload

    def _refresh_async(self, key: str) -> None:
        with self._lock:
            if key in self._inflight:
                return
            self._inflight.add(key)

        def run() -> None:
            try:
                payload = self.builder(key)
                entry = {"payload": payload, "computed_at": self._clock()}
                with self._lock:
                    self._memory[key] = entry
                self._write_disk(key, entry)
            except Exception:
                LOGGER.warning("state refresh failed for %s", key, exc_info=True)
            finally:
                with self._lock:
                    self._inflight.discard(key)

        threading.Thread(target=run, name=f"state-refresh-{key}", daemon=True).start()

    def _path(self, key: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_.:@" else "_" for ch in key)
        return self.cache_dir / f"{safe}.json"

    def _load_disk(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        payload = data.get("payload")
        computed_at = data.get("computed_at")
        if not isinstance(payload, dict) or not isinstance(computed_at, (int, float)):
            return None
        return {"payload": payload, "computed_at": float(computed_at)}

    def _write_disk(self, key: str, entry: dict[str, Any]) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(entry, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, path)
        except OSError:
            pass
