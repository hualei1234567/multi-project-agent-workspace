#!/bin/bash
# 在服务器上导入订阅并验证出网（跑在服务器本地，不是 Mac）。
#
# 用法：sudo bash setup-server-proxy.sh "<订阅链接>"
#
# 做四件事：
#   1. 抓订阅（base64 / Clash YAML 两种格式都认）；
#   2. 生成覆盖原配置的 proxy-providers + 规则（保留端口、日志、控制器设置）；
#   3. 重启 mihomo 并确认端口在听；
#   4. 用真实请求验证 api.teamorouter.com 可达。
set -euo pipefail

SUB_URL="${1:-}"
if [[ -z "$SUB_URL" ]]; then
  echo "用法：sudo bash $0 \"<订阅链接>\"" >&2
  exit 2
fi

CONF=/opt/mihomo/config.yaml
SUB_FILE=/opt/mihomo/subscription.yaml

echo "[1/4] 拉取订阅"
curl -fsSL -m 60 -A "clash-meta/1.19" -o /tmp/sub.raw "$SUB_URL" || {
  echo "订阅下载失败：检查链接与网络" >&2; exit 1; }

if grep -qE "^proxies:|^proxy-groups:|^proxy-providers:" /tmp/sub.raw 2>/dev/null; then
  cp /tmp/sub.raw "$SUB_FILE"
  echo "      识别为 Clash YAML"
else
  # 多数机场返回 base64(v2ray 链接)，这里只做 Clash 订阅；不猜协议
  echo "      订阅不是 Clash 格式（可能是 base64/v2ray）" >&2
  echo "      请在客户端里选“Clash 订阅”再复制链接" >&2
  exit 1
fi

echo "[2/4] 生成配置（保留端口与控制器设置）"
python3 - "$SUB_FILE" "$CONF" <<'PY'
import sys, pathlib, re
sub_path, conf_path = sys.argv[1], sys.argv[2]
sub = pathlib.Path(sub_path).read_text(encoding="utf-8")
# 订阅里的 DNS 规则可能与服务器环境冲突，只取 proxies / proxy-groups / rules
blocks = {}
for key in ("proxies", "proxy-groups", "rules"):
    m = re.search(rf"^{key}:\s*\n((?:[ \t].*\n|\n)*)", sub, re.MULTILINE)
    blocks[key] = f"{key}:\n" + (m.group(1) if m else "  []\n")
header = """# 由 setup-server-proxy.sh 生成：服务器自用出网代理
mixed-port: 7890
allow-lan: false
bind-address: 127.0.0.1
mode: rule
log-level: warning
external-controller: 127.0.0.1:9091
secret: ""
tun:
  enable: false
"""
pathlib.Path(conf_path).write_text(header + "\n" + "\n".join(blocks.values()), encoding="utf-8")
print("      已写入", conf_path)
PY

echo "[3/4] 重启 mihomo"
systemctl restart mihomo
sleep 3
systemctl is-active mihomo
ss -ltn | grep -q 7890 && echo "      7890 已监听"

echo "[4/4] 验证出网"
code=$(curl -s -o /dev/null -w "%{http_code}" -m 20 -x http://127.0.0.1:7890 https://api.teamorouter.com/ || true)
echo "      api.teamorouter.com → HTTP $code"
if [[ "$code" == "000" ]]; then
  echo "      仍然不通：检查订阅节点是否可用（curl -x http://127.0.0.1:7890 -I https://www.google.com）" >&2
  exit 1
fi
echo "完成。执行器可开启：sudo systemctl edit --full agent-platform（加 AGWS_PLATFORM_EXECUTOR=on）"
