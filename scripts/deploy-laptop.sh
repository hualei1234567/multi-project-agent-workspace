#!/bin/zsh
# 把平台部署到笔记本服务器（Ubuntu 22.04，systemd 托管）。
#
# 拓扑：Mac 只做开发，运行实例在笔记本上，Mac 通过局域网访问。
# 用法：scripts/deploy-laptop.sh [--with-data] [--force]
#   --with-data  连 data/platform.db 一起推过去（默认只在首次部署时加）
#   --force      跳过「在途调度项」保护（会杀掉执行器子进程，慎用）
#
# 幂等：重复执行 = 更新代码 + 重启服务，不动库、不动 token。
set -euo pipefail

HOST="${DEPLOY_HOST:-<ssh-alias>}"
TARGET="${DEPLOY_PATH:-/opt/agent-platform}"
WITH_DATA=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --with-data) WITH_DATA=1 ;;
    --force) FORCE=1 ;;
    *) echo "[deploy] 未知参数：$arg（支持 --with-data / --force）" >&2; exit 64 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 守护：systemd 重启会清掉执行器拉起的 claude 进程组。
# 只查一次会有竞态（查的时候还 queued，重启前刚好 running），所以有两个动作：
#   1) 部署开始时先做一次快速检查，明显在跑就直接退出；
#   2) **重启前**再走一道静默门：要求最近 90 秒无调度活动，且连续两次采样都安静。
QUIET_WINDOW_SECONDS=90

dispatch_state() {
  ssh "$HOST" "cd $TARGET && .venv/bin/python - <<'PY' 2>/dev/null || echo ERR
import sqlite3, datetime
c = sqlite3.connect('data/platform.db')
now = datetime.datetime.now(datetime.timezone.utc)
cut = (now - datetime.timedelta(seconds=$QUIET_WINDOW_SECONDS)).isoformat(timespec='seconds')
run = c.execute(
    \"select count(*) from dispatch_items where status in ('running','leased')\"
).fetchone()[0]
recent = c.execute(
    \"select count(*) from dispatch_items where coalesce(finished_at, started_at) >= ?\", (cut,)
).fetchone()[0]
queued = c.execute(
    \"select count(*) from dispatch_items where status = 'queued'\"
).fetchone()[0]
print(f'run={run} recent={recent} queued={queued}')
PY"
}

parse_state() {
  RUNNING="$(echo "$1" | sed -n 's/.*run=\([0-9]*\).*/\1/p')"
  RECENT="$(echo "$1" | sed -n 's/.*recent=\([0-9]*\).*/\1/p')"
  QUEUED="$(echo "$1" | sed -n 's/.*queued=\([0-9]*\).*/\1/p')"
}

if (( ! FORCE )); then
  echo "[deploy] 检查在途调度项"
  STATE="$(dispatch_state)"
  parse_state "$STATE"
  if [[ -z "$RUNNING" ]]; then
    echo "[deploy] ⚠ 读不到调度状态（$STATE），按有风险处理；确认无影响后加 --force"
    exit 2
  fi
  if (( RUNNING > 0 )); then
    echo "[deploy] ✘ 有正在执行的调度项：$STATE"
    echo "         重启会杀掉执行器拉起的 claude 进程；等它们结束，或确认无影响后加 --force"
    exit 2
  fi
  echo "[deploy] ✓ 部署开始时无运行中的执行器进程（$STATE）"
  if (( QUEUED > 0 )); then
    echo "[deploy] ⚠ 仍有 $QUEUED 个排队项；重启前会再检查一次是否被派发"
  fi
fi

echo "[deploy] 同步代码 → $HOST:$TARGET"
rsync -az --delete \
  --exclude '.venv' --exclude '.venv-py39-bak' --exclude 'node_modules' \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.ruff_cache' \
  --exclude 'data' --exclude '.git' --exclude '*.plist.disabled' \
  ./ "$HOST:$TARGET/"

if (( WITH_DATA )); then
  echo "[deploy] 同步数据库（含 token 文件若存在）"
  rsync -az data/platform.db "$HOST:$TARGET/data/"
  [[ -f data/token ]] && rsync -az data/token "$HOST:$TARGET/data/"
fi

# 服务器上的仓库路径与 Mac 不同，部署后统一改写（幂等）
echo "[deploy] 修正远端 repo_root / team_dir"
ssh "$HOST" "cd $TARGET && sed -i -e 's|<HOME>/vscodeworkspace|/home/user/vscodeworkspace|g' -e 's|<HOME>/agent-team|/home/user/agent-team|g' config/projects.json"

# 环境变量前缀统一为 AGWS_PLATFORM_*；若服务端仍是旧前缀，unit 会读不到开关
echo "[deploy] 校验 systemd 环境变量前缀"
ssh "$HOST" "grep -rqs 'AGWS_PLATFORM_' /etc/systemd/system/agent-platform.service.d/ || echo '  警告：服务端未配置 AGWS_PLATFORM_* 开关与执行器，见 docs/p4/RUNBOOK.md'"

echo "[deploy] 远端跑测试"
ssh "$HOST" "cd $TARGET && .venv/bin/python -m pytest app/tests -q 2>&1 | tail -3"

# 重启前静默门：堵住「检查时还 queued、重启时已 running」的竞态
if (( ! FORCE )); then
  echo "[deploy] 重启前静默检查（最近 ${QUIET_WINDOW_SECONDS}s 无调度活动，连续 2 次）"
  quiet=0
  for ((i = 0; i < 18; i++)); do
    STATE="$(dispatch_state)"
    parse_state "$STATE"
    if [[ -z "$RUNNING" ]]; then
      echo "[deploy] ⚠ 读不到调度状态（$STATE），跳过重启；确认后可加 --force 重跑" >&2
      exit 2
    fi
    if (( RUNNING > 0 )); then
      echo "[deploy] ✘ 重启前发现执行器已启动：$STATE" >&2
      echo "         已跳过重启（代码已同步，服务仍在跑旧版本）；等任务结束后重跑本脚本" >&2
      exit 2
    fi
    if (( RECENT == 0 )); then
      quiet=$((quiet + 1))
    else
      quiet=0
    fi
    if (( quiet >= 2 )); then
      echo "[deploy] ✓ 静默窗口确认（$STATE）"
      break
    fi
    sleep 10
  done
  if (( quiet < 2 )); then
    echo "[deploy] ✘ 180 秒内没有形成静默窗口（$STATE）；已跳过重启" >&2
    exit 2
  fi
fi

echo "[deploy] 重启服务"
ssh "$HOST" "sudo systemctl restart agent-platform && sleep 3 && systemctl is-active agent-platform"

echo "[deploy] 健康检查"
ssh "$HOST" "curl -s -m 5 http://127.0.0.1:8099/healthz" || {
  echo "[deploy] 服务未就绪，查看：ssh $HOST 'sudo journalctl -u agent-platform -n 50'" >&2
  exit 1
}
echo
echo "[deploy] 完成。局域网访问：http://<LAN_HOST>:8099/（无需令牌）"
