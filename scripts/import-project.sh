#!/usr/bin/env bash
# 把一个新项目接入多项目 Agent 管理平台。
#
# 用法：
#   scripts/import-project.sh --id demo --name "示例项目" <来源>
#
# <来源> 可以是：
#   - git 地址：git@host:group/repo.git 或 https://...
#   - 本地 zip / tar.gz / tar 归档
#   - 本地已存在的目录（原样保留，不复制）
#
# 做的事（每步都幂等）：
#   1. 把代码放到 ~/vscodeworkspace/<目录名>（已存在则跳过，不覆盖）
#   2. 识别语言栈并准备依赖（Python → 独立 .venv；Node → npm ci）
#   3. 备份并更新 config/projects.json，登记项目
#   4. 创建 ~/agent-team/<id>-team
#   5. 打印下一步（部署到笔记本服务器）
#
# 默认只打印计划；加 --apply 才真正执行。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKSPACE_DIR="${WORKSPACE_DIR:-$HOME/vscodeworkspace}"
AGENT_TEAM_DIR="${AGENT_TEAM_DIR:-$HOME/agent-team}"
APPLY=0
ID=""; NAME=""; DIRNAME=""; SOURCE=""

die() { echo "错误：$*" >&2; exit 1; }
say() { echo "[import] $*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --id) ID="${2:-}"; shift 2 ;;
    --name) NAME="${2:-}"; shift 2 ;;
    --dir) DIRNAME="${2:-}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) die "未知参数 $1" ;;
    *) SOURCE="$1"; shift ;;
  esac
done

[[ -n "$ID" ]] || die "缺少 --id"
[[ -n "$SOURCE" ]] || die "缺少代码来源"
[[ -n "$NAME" ]] || NAME="$ID"
[[ -n "$DIRNAME" ]] || DIRNAME="$(basename "${SOURCE%/}")"
DIRNAME="${DIRNAME%.git}"; DIRNAME="${DIRNAME%.zip}"; DIRNAME="${DIRNAME%.tar.gz}"; DIRNAME="${DIRNAME%.tar}"
TARGET="$WORKSPACE_DIR/$DIRNAME"

say "项目 id    : $ID"
say "项目名称   : $NAME"
say "代码来源   : $SOURCE"
say "落地目录   : $TARGET"
say "team 目录  : $AGENT_TEAM_DIR/$ID-team"
say "模式       : $([[ $APPLY -eq 1 ]] && echo 执行 || echo 预演（加 --apply 执行）)"

if [[ $APPLY -eq 0 ]]; then
  [[ -e "$TARGET" ]] && say "目录已存在，将复用：$TARGET" || say "将创建目录：$TARGET"
  echo "预演结束，未改动任何文件。"
  exit 0
fi

# 1. 取代码 -----------------------------------------------------------------
mkdir -p "$WORKSPACE_DIR"
if [[ -e "$TARGET" ]]; then
  say "目录已存在，跳过取代码：$TARGET"
elif [[ "$SOURCE" =~ ^(git@|https?://|ssh://) ]]; then
  say "克隆 $SOURCE"
  git clone "$SOURCE" "$TARGET"
elif [[ -f "$SOURCE" ]]; then
  case "$SOURCE" in
    *.zip) say "解压 zip（按预期目录名为 ${DIRNAME}）"
           tmp="$(mktemp -d)"; unzip -q "$SOURCE" -d "$tmp"
           inner="$(ls -A "$tmp")"
           if [[ "$(echo "$inner" | wc -l | tr -d ' ')" == "1" && -d "$tmp/$inner" ]]; then mv "$tmp/$inner" "$TARGET"; else mv "$tmp" "$TARGET"; fi ;;
    *.tar.gz|*.tgz) say "解压 tar.gz"; mkdir -p "$TARGET"; tar -xzf "$SOURCE" -C "$TARGET" --strip-components=1 ;;
    *.tar) say "解压 tar"; mkdir -p "$TARGET"; tar -xf "$SOURCE" -C "$TARGET" --strip-components=1 ;;
    *) die "不认识的归档格式：$SOURCE" ;;
  esac
else
  die "来源既不是 git 地址也不是存在的文件：$SOURCE"
fi

# 2. 依赖 -------------------------------------------------------------------
if [[ -f "$TARGET/pyproject.toml" || -f "$TARGET/requirements.txt" ]]; then
  [[ -n "${PYTHON:-}" ]] || PYTHON=python3
  if [[ ! -d "$TARGET/.venv" ]]; then
    say "创建独立虚拟环境 .venv（$("$PYTHON" -V 2>&1)）"
    "$PYTHON" -m venv "$TARGET/.venv"
    "$TARGET/.venv/bin/pip" install -q --upgrade pip
    if [[ -f "$TARGET/requirements.txt" ]]; then
      say "安装 requirements.txt"
      "$TARGET/.venv/bin/pip" install -q -r "$TARGET/requirements.txt" || say "依赖安装有失败项，需人工看日志"
    else
      say "安装 pyproject 依赖（editable）"
      "$TARGET/.venv/bin/pip" install -q -e "$TARGET" || say "editable 安装失败，需人工处理"
    fi
  else
    say "已有 .venv，跳过 Python 依赖"
  fi
else
  say "未发现 Python 依赖清单，跳过"
fi

if [[ -f "$TARGET/package.json" ]]; then
  if [[ ! -d "$TARGET/node_modules" ]]; then
    say "安装前端依赖（npm，使用国内镜像）"
    ( cd "$TARGET" && npm install --no-fund --no-audit \
        --registry=https://registry.npmmirror.com ) || say "npm 安装失败，需人工处理"
  else
    say "已有 node_modules，跳过"
  fi
else
  say "未发现 package.json，跳过"
fi

# 3. 登记项目 ---------------------------------------------------------------
mkdir -p "$AGENT_TEAM_DIR/$ID-team"
"$ROOT/.venv/bin/python" - "$ROOT/config/projects.json" "$ID" "$NAME" "$TARGET" "$AGENT_TEAM_DIR/$ID-team" <<'PY'
import json, shutil, sys, datetime
cfg_path, pid, name, repo, team = sys.argv[1:6]
bak = f"{cfg_path}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}"
shutil.copy2(cfg_path, bak)
cfg = json.load(open(cfg_path, encoding="utf-8"))
projects = cfg.setdefault("projects", [])
entry = {
    "id": pid, "code": pid, "name": name, "repo_root": repo,
    "team_dir": team, "default_branch": "main", "session_slugs": [],
}
for i, p in enumerate(projects):
    if p.get("id") == pid:
        projects[i] = {**p, **entry}
        print(f"[import] 已更新既有项目 {pid}")
        break
else:
    projects.append(entry)
    print(f"[import] 新增项目 {pid}")
json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"[import] 配置备份：{bak}")
PY

# 4. 摘要 -------------------------------------------------------------------
say "完成。项目 $ID 已登记，team 目录：$AGENT_TEAM_DIR/$ID-team"
say "下一步：scripts/deploy-laptop.sh  把平台与配置同步到笔记本服务器"
say "     然后在面板确认项目出现：http://<LAN_HOST>:8099/"
