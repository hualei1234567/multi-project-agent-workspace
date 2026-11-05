#!/usr/bin/env bash
# 平台状态自检：进程、端口、项目数、库路径。
set -uo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8099}"
export DDP_AGENT_PLATFORM_DB="${DDP_AGENT_PLATFORM_DB:-$WS/data/platform.db}"
export DDP_AGENT_PLATFORM_WORKSPACE="$WS"

echo "=== 工作区 ==="; echo "  $WS"
echo "=== 库 ==="; echo "  $DDP_AGENT_PLATFORM_DB"
[ -f "$DDP_AGENT_PLATFORM_DB" ] && echo "  大小 $(du -h "$DDP_AGENT_PLATFORM_DB" | cut -f1)" || echo "  ⚠ 不存在"
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
