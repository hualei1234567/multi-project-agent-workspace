#!/usr/bin/env bash
# 全流程验收（R4）：从代码健康到真实链路，逐层验证并给出可核对的结论。
#
# 与 scripts/test.sh 的区别：
#   test.sh  = 开发自测（快，只跑单测）
#   本脚本   = 验收自测（全，含契约、隔离实例冒烟、数据一致性、真实链路证据）
#
# 退出码：0 全通过；1 有失败；2 环境不可用。
set -uo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WS" || exit 2
PY="${PYTHON:-$WS/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
PORT="${AGWS_ACCEPTANCE_PORT:-9097}"
DIR="/tmp/agws-acceptance"
pass=0; fail=0
ok()   { echo "  ✅ $1"; pass=$((pass+1)); }
bad()  { echo "  ✘ $1"; fail=$((fail+1)); }
step() { echo; echo "── $1"; }

cleanup() {
  [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null
  wait "${SERVER_PID:-}" 2>/dev/null
}
trap cleanup EXIT

step "[1/7] 代码健康：py_compile + ruff"
"$PY" -m py_compile app/agents_dashboard.py app/agent_platform/*.py app/agent_platform/p4/*.py \
  && ok "py_compile" || bad "py_compile"
if [ -x "$WS/.venv/bin/ruff" ]; then
  "$WS/.venv/bin/ruff" check app/agent_platform app/tests app/agents_dashboard.py >/dev/null 2>&1 \
    && ok "ruff clean" || bad "ruff"
else
  echo "  （未装 ruff，跳过）"
fi

step "[2/7] 单元与集成测试"
if "$PY" -m pytest app/tests -q >/tmp/agws-acceptance-pytest.log 2>&1; then
  n=$(grep -oE "[0-9]+ passed" /tmp/agws-acceptance-pytest.log | tail -1)
  ok "pytest: $n"
else
  bad "pytest 失败（见 /tmp/agws-acceptance-pytest.log）"
  tail -20 /tmp/agws-acceptance-pytest.log
fi

step "[3/7] 路由契约：OpenAPI 双写一致性"
if "$PY" -m pytest app/tests/test_route_contract.py -q >/dev/null 2>&1; then
  ok "契约一致"
else
  bad "契约漂移"
fi

step "[4/7] 基线数字（与 R4 文档对照）"
"$PY" - <<'PYEOF'
import sys
sys.path.insert(0, "app")
from agent_platform import openapi, store
conn = store.connect()
ver = conn.execute("select max(version) from schema_version").fetchone()[0]
tables = len([r for r in conn.execute(
    "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
    " and name not like 'knowledge_fts_%'")])
ops = len(openapi.API_PATHS)
paths = len({p for _, p in openapi.API_PATHS})
print(f"  schema=v{ver}  tables={tables}  api_ops={ops}  api_paths={paths}")
PYEOF

step "[5/7] 隔离实例 + UI 冒烟"
rm -rf "$DIR"; mkdir -p "$DIR/projects" "$DIR/repo"
printf '%s\n' '{"projects": [{"id": "material", "code": "material", "name": "MaterialDev", "repo_root": "'"$DIR"'/repo"}], "default_project_id": "material"}' > "$DIR/projects.json"
AGWS_PLATFORM_DB="$DIR/platform.db" \
AGWS_PLATFORM_CONFIG="$DIR/projects.json" \
AGWS_PLATFORM_PROJECTS_ROOT="$DIR/projects" \
AGWS_PLATFORM_EXECUTOR=off \
  "$PY" app/agents_dashboard.py --port "$PORT" > "$DIR/server.log" 2>&1 &
SERVER_PID=$!
ready=0
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
if [ "$ready" -eq 1 ]; then
  ok "隔离实例就绪，端口 $PORT"
  if AGWS_URL="http://127.0.0.1:$PORT" AGWS_DB="$DIR/platform.db" \
       ./scripts/ci-smoke.sh > "$DIR/smoke.log" 2>&1; then
    summary=$(grep -E "用例 [0-9]+，通过" "$DIR/smoke.log" | tail -1)
    ok "UI 冒烟: $summary"
  else
    bad "UI 冒烟失败（见 $DIR/smoke.log）"
    grep "FAIL" "$DIR/smoke.log" | head -10
  fi
else
  bad "隔离实例未能启动（见 $DIR/server.log）"
  tail -20 "$DIR/server.log"
fi

step "[6/7] 数据库一致性"
"$PY" - <<'PYEOF'
import sqlite3, sys
conn = sqlite3.connect("data/platform.db")
conn.row_factory = sqlite3.Row
problems = []
# 1) 外键完整性
broken = conn.execute("PRAGMA foreign_key_check").fetchall()
if broken:
    problems.append(f"外键断裂 {len(broken)} 处")
else:
    print("  ✅ 外键完整")
# 2) 台账不变量：有 cost_quality 的事件，状态必须自洽
bad_cost = conn.execute(
    "SELECT COUNT(*) FROM token_usage_events WHERE cost_quality='actual'"
    " AND actual_cost_usd IS NULL").fetchone()[0]
if bad_cost:
    problems.append(f"{bad_cost} 条事件标 actual 却没有金额")
else:
    print("  ✅ 成本口径自洽")
# 3) 执行追溯：dispatcher 写入的必须带 task_id
orphan = conn.execute(
    "SELECT COUNT(*) FROM executions WHERE source='dispatcher'"
    " AND (task_id IS NULL OR link_quality != 'linked')").fetchone()[0]
if orphan:
    problems.append(f"{orphan} 条调度执行缺 task 关联")
else:
    print("  ✅ 调度执行均已关联 Task")
# 4) 闸门：waiting_approval 的运行必须有等待项
bad_gate = conn.execute(
    "SELECT COUNT(*) FROM workflow_runs WHERE status='waiting_approval'"
    " AND id NOT IN (SELECT run_id FROM workflow_node_runs"
    "                WHERE status='waiting_approval')").fetchone()[0]
if bad_gate:
    problems.append(f"{bad_gate} 个运行卡在闸门但没有等待项")
else:
    print("  ✅ 闸门状态与节点一致")
conn.close()
for p in problems:
    print(f"  ✘ {p}")
sys.exit(1 if problems else 0)
PYEOF
[ $? -eq 0 ] && ok "数据一致性" || bad "数据一致性问题"

step "[7/7] 真实链路证据（R3 试点）"
"$PY" - <<'PYEOF'
import sqlite3, sys
conn = sqlite3.connect("data/platform.db")
conn.row_factory = sqlite3.Row
checks = [
    ("eval_run 有 runtime 来源",
     "SELECT COUNT(*) FROM eval_runs WHERE output_source='runtime'"),
    ("release 标注了 mode",
     "SELECT COUNT(*) FROM agent_releases WHERE release_mode IS NOT NULL"),
    ("闸门审批有可信 approver",
     "SELECT COUNT(*) FROM workflow_approvals WHERE approver IS NOT NULL"
     " AND approver != ''"),
    ("执行有真实账单",
     "SELECT COUNT(*) FROM executions WHERE cost_quality='actual'"
     " AND actual_cost_usd IS NOT NULL"),
]
bad = 0
for label, sql in checks:
    n = conn.execute(sql).fetchone()[0]
    if n:
        print(f"  ✅ {label}（{n} 条）")
    else:
        print(f"  ✘ {label}（0 条）")
        bad += 1
conn.close()
sys.exit(1 if bad else 0)
PYEOF
[ $? -eq 0 ] && ok "真实链路证据齐全" || bad "真实链路证据缺失"

echo
echo "════════════════════════════════════"
echo "验收结果：通过 $pass 项，失败 $fail 项"
[ "$fail" -eq 0 ] && echo "✅ 全流程验收通过" || echo "✘ 有 $fail 项未通过"
exit $([ "$fail" -eq 0 ] && echo 0 || echo 1)
