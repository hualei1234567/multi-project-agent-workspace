#!/usr/bin/env bash
# 启动多项目 Agent 管理平台（只监听本机）。
# 用法：scripts/start.sh [端口] [--background]
set -euo pipefail

WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8099}"
MODE="${2:-}"

# 统一用工作区自己的库与配置，避免误读旧路径
export DDP_AGENT_PLATFORM_DB="${DDP_AGENT_PLATFORM_DB:-$WS/data/platform.db}"
export DDP_AGENT_PLATFORM_WORKSPACE="$WS"

cd "$WS/app"
if [ "$MODE" = "--background" ]; then
  mkdir -p "$WS/data/logs"
  nohup python3 agents_dashboard.py --port "$PORT" >>"$WS/data/logs/dashboard-$PORT.log" 2>&1 &
  echo "[start] 后台启动，PID=$!，日志 $WS/data/logs/dashboard-$PORT.log"
  sleep 1
  curl -sf "http://127.0.0.1:$PORT/api/platform/meta" >/dev/null 2>&1 \
    && echo "[start] ✅ http://127.0.0.1:$PORT" \
    || echo "[start] ⚠ 启动中，稍后用 scripts/status.sh 确认"
else
  echo "[start] http://127.0.0.1:$PORT （Ctrl+C 停止）"
  exec python3 agents_dashboard.py --port "$PORT"
fi
