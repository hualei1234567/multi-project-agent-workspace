#!/bin/zsh
# 等调度器真正空闲后自动部署。
# 口径与 deploy-laptop.sh 一致：running/leased=0、最近 90 秒无调度活动、且没有 claude -p 进程，
# 连续两次才部署。数据库状态可能滞后于进程，所以进程数是硬条件。
# 用法：scripts/deploy-when-idle.sh [最多等待分钟数]
set -u

HOST="${DEPLOY_HOST:-<ssh-alias>}"
TARGET="${DEPLOY_PATH:-/opt/agent-platform}"
MAX_MIN="${1:-180}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

dispatch_state() {
  ssh -o BatchMode=yes "$HOST" "cd $TARGET && .venv/bin/python - <<'PY' 2>/dev/null || echo ERR
import subprocess, sqlite3, datetime
c = sqlite3.connect('data/platform.db')
now = datetime.datetime.now(datetime.timezone.utc)
cut = (now - datetime.timedelta(seconds=90)).isoformat(timespec='seconds')
run = c.execute(
    \"select count(*) from dispatch_items where status in ('running','leased')\"
).fetchone()[0]
recent = c.execute(
    \"select count(*) from dispatch_items where coalesce(finished_at, started_at) >= ?\", (cut,)
).fetchone()[0]
queued = c.execute(
    \"select count(*) from dispatch_items where status = 'queued'\"
).fetchone()[0]
# 数据库状态可能滞后于进程：正在跑的 claude -p 一律算忙
try:
    # -x 只认可执行名 claude：避免 ssh 包装命令里出现同一串文本而自匹配
    out = subprocess.run(['pgrep', '-xc', 'claude'], capture_output=True, text=True)
    proc = int((out.stdout or '0').strip() or 0)
except Exception:
    proc = -1
print(f'run={run} recent={recent} queued={queued} proc={proc}')
PY"
}

quiet=0
for ((i = 0; i < MAX_MIN; i++)); do
  STATE="$(dispatch_state)"
  RUN="$(echo "$STATE" | sed -n 's/.*run=\([0-9]*\).*/\1/p')"
  RECENT="$(echo "$STATE" | sed -n 's/.*recent=\([0-9]*\).*/\1/p')"
  PROC="$(echo "$STATE" | sed -n 's/.*proc=\(-\{0,1\}[0-9]*\).*/\1/p')"
  echo "[wait] $(date '+%H:%M:%S') $STATE"
  if [[ -z "$RUN" || -z "$PROC" ]]; then
    echo "[wait] 读不到调度状态，继续等" >&2
    quiet=0
  elif (( RUN == 0 && RECENT == 0 && PROC == 0 )); then
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
