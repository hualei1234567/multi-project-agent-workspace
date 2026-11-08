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

# 守护：systemd 重启会清掉执行器拉起的 claude 进程组。有在途调度项时默认拒绝重启。
if (( ! FORCE )); then
  echo "[deploy] 检查在途调度项"
  ACTIVE="$(ssh "$HOST" "cd $TARGET && .venv/bin/python - <<'PY' 2>/dev/null || true
import sqlite3
c = sqlite3.connect('data/platform.db')
rows = c.execute(
    \"select status, count(*) from dispatch_items where status in ('running','leased','queued') group by 1\"
).fetchall()
print(' '.join(f'{s}={n}' for s, n in rows))
PY"
)"
  if [[ -n "${ACTIVE// /}" ]]; then
    echo "[deploy] ✘ 存在在途调度项：$ACTIVE"
    echo "         重启会杀掉执行器拉起的 claude 进程；等它们结束，或确认无影响后加 --force"
    exit 2
  fi
  echo "[deploy] ✓ 无在途调度项"
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

echo "[deploy] 重启服务"
ssh "$HOST" "sudo systemctl restart agent-platform && sleep 3 && systemctl is-active agent-platform"

echo "[deploy] 健康检查"
ssh "$HOST" "curl -s -m 5 http://127.0.0.1:8099/healthz" || {
  echo "[deploy] 服务未就绪，查看：ssh $HOST 'sudo journalctl -u agent-platform -n 50'" >&2
  exit 1
}
echo
echo "[deploy] 完成。局域网访问：http://<LAN_HOST>:8099/（无需令牌）"
