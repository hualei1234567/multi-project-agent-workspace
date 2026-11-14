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
# 执行器默认开启：本机即服务器，平台的托管会话/派发要用它。
# 需要关闭时显式传 AGWS_PLATFORM_EXECUTOR=off。
export AGWS_PLATFORM_EXECUTOR="${AGWS_PLATFORM_EXECUTOR:-on}"

cd "$WS/app"
if [ "$MODE" = "--background" ]; then
  mkdir -p "$WS/data/logs"
  nohup "$PY" agents_dashboard.py --port "$PORT" >>"$WS/data/logs/dashboard-$PORT.log" 2>&1 &
  echo "[start] 后台启动，PID=$!，日志 $WS/data/logs/dashboard-$PORT.log"
  sleep 1
  for _ in $(seq 1 15); do
    if curl -sf -m 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
      echo "[start] ✅ http://127.0.0.1:$PORT （执行器：${AGWS_PLATFORM_EXECUTOR}）"
      exit 0
    fi
    sleep 1
  done
  echo "[start] ⚠ 15 秒内未就绪，查看 data/logs/dashboard-$PORT.log"
else
  echo "[start] http://127.0.0.1:$PORT （执行器：${AGWS_PLATFORM_EXECUTOR}，Ctrl+C 停止）"
  exec "$PY" agents_dashboard.py --port "$PORT"
fi
