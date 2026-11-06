#!/bin/zsh
# 平台库每日热备 + 轮转（P4-OPS-004）。
# 保留最近 KEEP 份，其余按时间从旧到新删除。只删本脚本产生的前缀文件。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

KEEP="${P4_BACKUP_KEEP:-14}"
PY="$ROOT/.venv/bin/python"

BACKUP_PATH=$("$PY" - <<'PY'
import sys
sys.path.insert(0, "app")
from agent_platform import migrate, store
print(migrate.backup_database(store.default_db_path()) or "")
PY
)

if [[ -z "$BACKUP_PATH" ]]; then
  echo "[backup] 备份失败：未生成文件" >&2
  exit 1
fi

echo "[backup] 已生成 $BACKUP_PATH"

# 轮转：只处理 platform.db.bak-<时间戳> 形态的文件
count=$(ls -1 data/platform.db.bak-* 2>/dev/null | wc -l | tr -d ' ')
if (( count > KEEP )); then
  remove=$(( count - KEEP ))
  echo "[backup] 轮转：保留 $KEEP 份，删除最旧 $remove 份"
  ls -1t data/platform.db.bak-* | tail -n "$remove" | while read -r old; do
    echo "[backup] 删除 $old"
    rm -f "$old"
  done
fi
