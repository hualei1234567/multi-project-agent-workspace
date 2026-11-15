#!/usr/bin/env bash
# 冒烟前置自检 + 执行：CI 与本地共用，保证两边条件一致。
#
# 检查顺序刻意是「先查依赖，再查被测对象」：本地开发时平台常驻，
# 依赖却可能没装；CI 上反过来。两种情形都要给出能直接照做的错误。
set -uo pipefail
WS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${AGWS_URL:-http://127.0.0.1:8098}"
CHROME="${AGWS_CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
rc=0

echo "[1/4] 检查浏览器"
if [ ! -x "$CHROME" ]; then
  echo "  ✘ 浏览器不存在：$CHROME"
  echo "    macOS：安装 Google Chrome"
  echo "    CI/无头：npx playwright install chromium"
  echo "             export AGWS_CHROME=\"\$(node -e \"const{chromium}=require('playwright');console.log(chromium.executablePath())\")\""
  exit 2
fi
echo "  ✅ $CHROME"

echo "[2/4] 检查 sqlite3 CLI"
# 冒烟要直连被测实例的库做夹具清理，没有 sqlite3 会静默跳过清理、
# 留下脏数据影响下一次运行，所以这里当成硬依赖。
if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "  ✘ 缺少 sqlite3 命令行（冒烟用它清理测试夹具）"
  echo "    Ubuntu: sudo apt-get install -y sqlite3"
  echo "    macOS:  自带"
  exit 2
fi
echo "  ✅ $(command -v sqlite3)"

echo "[3/4] 检查平台实例 $URL"
if ! curl -fsS "$URL/healthz" >/dev/null 2>&1; then
  echo "  ✘ 平台实例不可用（$URL/healthz）"
  echo "    本地启动：./scripts/start.sh 8098 --background"
  exit 2
fi
echo "  ✅ $(curl -fsS "$URL/healthz")"

echo "[4/4] 运行 UI 冒烟"
cd "$WS" || exit 2
# 默认库里跑冒烟会在生产库留下 demo-b 夹具（异常退出时尤其明显）。
# 显式提醒：对生产实例跑之前先想清楚，清理靠测试自己的 finally。
if [ "${AGWS_ALLOW_PROD_SMOKE:-0}" != "1" ]; then
  case "$URL" in
    *:8099*)
      echo "  ⚠ 目标看起来是生产实例: $URL"
      echo "    冒烟会在该库创建/删除 demo-b 夹具。"
      echo "    建议用隔离实例；确实要跑请设 AGWS_ALLOW_PROD_SMOKE=1"
      exit 2
      ;;
  esac
fi
AGWS_URL="$URL" AGWS_CHROME="$CHROME" node app/tests/ui_smoke.mjs || rc=$?

[ "$rc" -eq 0 ] && echo "✅ 冒烟全部通过" || echo "✘ 冒烟失败 rc=$rc"
exit "$rc"
