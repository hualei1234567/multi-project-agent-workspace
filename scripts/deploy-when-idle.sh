#!/bin/zsh
# 等调度队列清空后自动部署（避免 systemd 重启杀掉执行器的 claude 子进程）。
# 用法：scripts/deploy-when-idle.sh [最多等待分钟数]
set -u

HOST="${DEPLOY_HOST:-<ssh-alias>}"
TARGET="${DEPLOY_PATH:-/opt/agent-platform}"
MAX_MIN="${1:-180}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

active_count() {
  ssh -o BatchMode=yes "$HOST" "cd $TARGET && .venv/bin/python - <<'PY' 2>/dev/null || echo ERR
import sqlite3
c = sqlite3.connect('data/platform.db')
n = c.execute(
    \"select count(*) from dispatch_items where status in ('running','leased','queued')\"
).fetchone()[0]
print(n)
PY"
}

idle_streak=0
for ((i = 0; i < MAX_MIN; i++)); do
  n="$(active_count)"
  echo "[wait] $(date '+%H:%M:%S') 在途调度项=$n"
  if [[ "$n" == "0" ]]; then
    idle_streak=$((idle_streak + 1))
  else
    idle_streak=0
  fi
  # 连续两次读到 0 才部署，避免和派发器抢跑
  if (( idle_streak >= 2 )); then
    echo "[wait] 队列已空闲，开始部署"
    cd "$ROOT" && ./scripts/deploy-laptop.sh
    exit $?
  fi
  sleep 60
done

echo "[wait] 超过 ${MAX_MIN} 分钟仍在跑，放弃自动部署；请稍后手动执行 scripts/deploy-laptop.sh" >&2
exit 3
