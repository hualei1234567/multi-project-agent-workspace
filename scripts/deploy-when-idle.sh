#!/bin/zsh
# 等调度器真正空闲后自动部署。
# 口径与 deploy-laptop.sh 一致：running/leased=0 且最近 90 秒无调度活动，连续两次才部署。
# 用法：scripts/deploy-when-idle.sh [最多等待分钟数]
set -u

HOST="${DEPLOY_HOST:-<ssh-alias>}"
TARGET="${DEPLOY_PATH:-/opt/agent-platform}"
MAX_MIN="${1:-180}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

dispatch_state() {
  ssh -o BatchMode=yes "$HOST" "cd $TARGET && .venv/bin/python - <<'PY' 2>/dev/null || echo ERR
import sqlite3, datetime
c = sqlite3.connect('data/platform.db')
now = datetime.datetime.now(datetime.timezone.utc)
cut = (now - datetime.timedelta(seconds=90)).isoformat(timespec='seconds')
run = c.execute(
    \"select count(*) from dispatch_items where status in ('running','leased')\"
).fetchone()[0]
recent = c.execute(
    \"select count(*) from dispatch_items where coalesce(finished_at, started_at) >= ?\", (cut,)
).fetchone()[0]
print(f'run={run} recent={recent}')
PY"
}

quiet=0
for ((i = 0; i < MAX_MIN; i++)); do
  STATE="$(dispatch_state)"
  RUN="$(echo "$STATE" | sed -n 's/.*run=\([0-9]*\).*/\1/p')"
  RECENT="$(echo "$STATE" | sed -n 's/.*recent=\([0-9]*\).*/\1/p')"
  echo "[wait] $(date '+%H:%M:%S') $STATE"
  if [[ -z "$RUN" ]]; then
    echo "[wait] 读不到调度状态，继续等" >&2
    quiet=0
  elif (( RUN == 0 && RECENT == 0 )); then
    quiet=$((quiet + 1))
  else
    quiet=0
  fi
  if (( quiet >= 2 )); then
    echo "[wait] 已形成静默窗口，开始部署"
    cd "$ROOT" && ./scripts/deploy-laptop.sh
    exit $?
  fi
  sleep 60
done

echo "[wait] 超过 ${MAX_MIN} 分钟仍不安静，放弃自动部署；请稍后手动执行 scripts/deploy-laptop.sh" >&2
exit 3
