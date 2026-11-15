"""访问控制：本机免令牌 + 远端需 Bearer 令牌（M3-4 T3-401）。

为什么需要它：平台原本绑定 127.0.0.1，是安全的。但 M3-4 要求手机能访问，
``--tailscale`` 会把服务暴露到整个 tailnet —— 那时**任何**同网设备都能
读写全部项目、触发调度、执行运行时动作。这是当前最大的风险面。

策略（最小侵入、不打断本机使用）：

* 请求来自回环地址（127.0.0.1 / ::1）→ 直接放行，本机体验零变化。
* 其它来源 → 必须带 ``Authorization: Bearer <token>``。
* 令牌从 ``data/token`` 读取；不存在则首次启动生成（0600）。
* 页面本身（``/`` 与静态）也需要令牌，否则移动端打开就是一堵墙；
  移动端用 ``?token=`` 首次访问后由前端存进 localStorage 并注入请求头。
* ``POST /api/open`` 只允许回环来源：它会用系统默认程序打开本机文件，
  远端触发等于给了别人一个本机文件打开器。
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Any

from . import config

TOKEN_HEADER = "Authorization"

#: 只允许本机访问的路径（即便带了合法令牌）。
LOOPBACK_ONLY_PATHS = frozenset({"/api/open"})

#: 免鉴权路径：健康探针与令牌换取页需要能在未登录时访问。
#:
#: PWA 外壳资源也在其中（P3-M6）：Service Worker 的 `install` 预缓存由浏览器
#: 发起，**不带 Authorization 头**（令牌存在 localStorage，不进 Cookie），
#: 若这些路径要求令牌，远端 HTTPS 访问时预缓存会整批 401，离线能力形同虚设。
#: 这些资源只有 manifest / 脚本 / 图标 / 离线页，不含任何业务数据。
PUBLIC_PATHS = frozenset({
    "/healthz",
    "/api/auth/token",
    "/manifest.webmanifest",
    "/sw.js",
    "/offline.html",
})

#: 免鉴权前缀：图标有多条路径，逐条登记容易漏。
PUBLIC_PREFIXES = ("/icons/",)


def is_public_path(path: str) -> bool:
    """路径是否无需令牌（也跳过治理闸门，与既有 PUBLIC_PATHS 一致）。"""
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def token_path() -> Path:
    override = os.environ.get("AGWS_PLATFORM_TOKEN_FILE")
    if override:
        return Path(override).expanduser()
    return config.data_dir() / "token"


def load_or_create_token() -> str:
    path = token_path()
    try:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        # 写不了盘也不能让平台起不来：本次进程内用这个令牌
        pass
    return token


_CACHED: str | None = None


def current_token() -> str:
    global _CACHED
    if _CACHED is None:
        _CACHED = load_or_create_token()
    return _CACHED


def reset_cache() -> None:
    """测试用：清掉内存缓存，强制重新读盘。"""
    global _CACHED
    _CACHED = None


def auth_enabled() -> bool:
    """远端令牌校验是否开启（默认关闭）。"""
    return os.environ.get("AGWS_PLATFORM_AUTH", "off").strip().lower() in {
        "on", "1", "true", "yes",
    }


def is_loopback(address: str | None) -> bool:
    if not address:
        return False
    return address in {"127.0.0.1", "::1", "localhost"} or address.startswith("127.")


def _extract_token(headers: Any, query_params: dict[str, list[str]] | None = None) -> str | None:
    raw = headers.get(TOKEN_HEADER) or ""
    scheme, _, value = raw.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    # 允许 ?token= 用于移动端首次打开页面
    if query_params:
        values = query_params.get("token") or []
        if values and values[0].strip():
            return values[0].strip()
    return None


def check_request(
    *,
    path: str,
    client_address: str | None,
    headers: Any,
    query_params: dict[str, list[str]] | None = None,
    auth_enabled: bool | None = None,
) -> tuple[bool, str]:
    """返回 (是否放行, 拒绝原因)。放行时原因为空。"""
    if is_public_path(path):
        return True, ""

    enabled = auth_enabled
    if enabled is None:
        # 默认关闭：局域网内单用户使用，不强制令牌。
        # 需要时用 AGWS_PLATFORM_AUTH=on 打开远端校验。
        enabled = os.environ.get("AGWS_PLATFORM_AUTH", "off").strip().lower() in {
            "on", "1", "true", "yes",
        }

    # /api/open 会用系统默认程序打开本机文件：无论鉴权是否开启，
    # 都不允许非回环来源触发。这一条不受开关影响。
    if path in LOOPBACK_ONLY_PATHS and not is_loopback(client_address):
        return False, "loopback_only"

    if not enabled:
        return True, ""
    if is_loopback(client_address):
        return True, ""

    supplied = _extract_token(headers, query_params)
    if not supplied:
        return False, "missing_token"
    if not hmac.compare_digest(supplied, current_token()):
        return False, "invalid_token"
    return True, ""


def client_info(headers: Any) -> dict[str, Any]:
    return {
        "authorization": bool(headers.get(TOKEN_HEADER)),
        "user_agent": (headers.get("User-Agent") or "")[:120],
    }
