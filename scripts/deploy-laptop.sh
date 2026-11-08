#!/bin/zsh
# 把平台部署到笔记本服务器（Ubuntu 22.04，systemd 托管）。
#
# 拓扑：Mac 只做开发，运行实例在笔记本上，Mac 通过局域网访问。
# 用法：scripts/deploy-laptop.sh [--with-data]
#   --with-data  连 data/platform.db 一起推过去（默认只在首次部署时加）
#
# 幂等：重复执行 = 更新代码 + 重启服务，不动库、不动 token。
set -euo pipefail

HOST="${DEPLOY_HOST:-<ssh-alias>}"
TARGET="${DEPLOY_PATH:-/opt/agent-platform}"
WITH_DATA=0
[[ "${1:-}" == "--with-data" ]] && WITH_DATA=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

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
echo "[deploy] 修正远端 repo_root"
ssh "$HOST" "cd $TARGET && sed -i 's|<HOME>/vscodeworkspace|/home/user/vscodeworkspace|' config/projects.json"

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
