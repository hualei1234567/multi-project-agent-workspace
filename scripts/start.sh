#!/usr/bin/env bash
# 启动多项目 Agent 管理平台（只监听本机）。
# 用法：scripts/start.sh [端口] [--background]
set -euo pipefail

WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8099}"
MODE="${2:-}"
# 固定用工作区 venv（launchd 的 PATH 没有用户环境，系统 python 会是 3.9）
PY="$(command -v python3)"
if [ -x "$WS/.venv/bin/python" ]; then PY="$WS/.venv/bin/python"; fi

# 统一用工作区自己的库与配置，避免误读旧路径
export AGWS_PLATFORM_DB="${AGWS_PLATFORM_DB:-$WS/data/platform.db}"
export AGWS_PLATFORM_WORKSPACE="$WS"
# 执行器默认关闭（与 executor_policy.py 的 Deny-by-Default 一致）。
# 需要平台真实派发时显式传 AGWS_PLATFORM_EXECUTOR=on。
# 理由：默认开启意味着「计划 + 点一下」就能起 claude 进程，而审批规则为空时
# 这一步没有任何闸门。先默认关，需要时再开。
export AGWS_PLATFORM_EXECUTOR="${AGWS_PLATFORM_EXECUTOR:-off}"
# tailnet 监听：手机要访问必须监听 tailnet 接口（只监听 127.0.0.1 手机连不上）。
# 默认开——平台本身有 token 鉴权（auth.py），远端必须带 Bearer。
# 需要完全隔离时传 AGWS_PLATFORM_TAILSCALE=off。
export AGWS_PLATFORM_TAILSCALE="${AGWS_PLATFORM_TAILSCALE:-on}"

# 组装额外参数
EXTRA_ARGS=()
if [ "$AGWS_PLATFORM_TAILSCALE" = "on" ]; then
  EXTRA_ARGS+=(--tailscale)
fi

cd "$WS/app"
if [ "$MODE" = "--background" ]; then
  mkdir -p "$WS/data/logs"
  nohup "$PY" agents_dashboard.py --port "$PORT" "${EXTRA_ARGS[@]}" \
    >>"$WS/data/logs/dashboard-$PORT.log" 2>&1 &
  echo "[start] 后台启动，PID=$!，日志 $WS/data/logs/dashboard-$PORT.log"
  sleep 1
  for _ in $(seq 1 15); do
    if curl -sf -m 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      echo "[start] ✅ http://127.0.0.1:$PORT （执行器：${AGWS_PLATFORM_EXECUTOR}，tailnet：${AGWS_PLATFORM_TAILSCALE}）"
      exit 0
    fi
    sleep 1
  done
  echo "[start] ⚠ 15 秒内未就绪，查看 data/logs/dashboard-$PORT.log"
else
  echo "[start] http://127.0.0.1:$PORT （执行器：${AGWS_PLATFORM_EXECUTOR}，Ctrl+C 停止）"
  exec "$PY" agents_dashboard.py --port "$PORT" "${EXTRA_ARGS[@]}"
fi
