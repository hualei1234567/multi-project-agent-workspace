#!/usr/bin/env bash
# 平台状态自检：进程、端口、项目数、库路径。
set -uo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8099}"
export AGWS_PLATFORM_DB="${AGWS_PLATFORM_DB:-$WS/data/platform.db}"
export AGWS_PLATFORM_WORKSPACE="$WS"

echo "=== 工作区 ==="; echo "  $WS"
echo "=== 库 ==="; echo "  $AGWS_PLATFORM_DB"
[ -f "$AGWS_PLATFORM_DB" ] && echo "  大小 $(du -h "$AGWS_PLATFORM_DB" | cut -f1)" || echo "  ⚠ 不存在"
echo "=== 进程 ==="
pgrep -fl "agents_dashboard.py --port $PORT" || echo "  未运行"
echo "=== 接口 ==="
if curl -sf "http://127.0.0.1:$PORT/api/projects" >/dev/null 2>&1; then
  python3 - "$PORT" <<'PY'
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/projects", timeout=5) as r:
    data = json.load(r)
print(f"  ✅ 项目 {data.get('total', 0)} 个，默认 {data.get('default_project_id')}")
for p in data.get("items", []):
    print(f"     - {p['id']:10s} {p['name']}")
PY
else
  echo "  ⚠ 未响应（未启动或无权限）"
fi
echo "=== 健康 ==="
HZ="$(curl -sf -m 5 "http://127.0.0.1:$PORT/healthz" 2>/dev/null || true)"
if [ -n "$HZ" ]; then
  echo "  ✅ $HZ"
else
  echo "  ⚠ /healthz 无响应"
fi
echo "=== 执行器 / 桥接 ==="
pgrep -fl "session-bridge.py" >/dev/null 2>&1 \
  && echo "  ✅ 会话桥接运行中（本机窗口可被平台「进去看」）" \
  || echo "  - 会话桥接未运行：scripts/session-bridge.py（影响外部窗口的实时查看与派发）"
