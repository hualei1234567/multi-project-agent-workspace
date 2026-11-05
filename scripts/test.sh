#!/usr/bin/env bash
# 平台自测：py_compile + pytest + ruff（与 DDP CI 的 agent-platform 阶段等价）。
set -uo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-$WS/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
RUFF="${RUFF:-$WS/.venv/bin/ruff}"
[ -x "$RUFF" ] || RUFF="$(command -v ruff || true)"

cd "$WS/app" || exit 2
rc=0
echo "[1/3] py_compile"
"$PY" -m py_compile agents_dashboard.py agent_platform/*.py || rc=$((rc+1))
echo "[2/3] pytest"
"$PY" -m pytest tests -q || rc=$((rc+1))
echo "[3/3] ruff"
if [ -n "$RUFF" ]; then "$RUFF" check agent_platform tests agents_dashboard.py || rc=$((rc+1)); else echo "  （未安装 ruff，跳过）"; fi
[ "$rc" -eq 0 ] && echo "✅ 全部通过" || echo "✘ 有 $rc 项失败"
exit "$rc"
