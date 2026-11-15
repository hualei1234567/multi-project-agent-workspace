"""PWA 静态资源：manifest / Service Worker / 离线页 / 图标（P3-M6）。

**为什么资源在 Python 里生成，而不是仓库里放静态文件**

1. 平台是单文件托管：`agents_dashboard.py` 只服务页面本身和 `/api/file`，
   没有静态目录路由。为了 PWA 新增一个目录服务，等于给「任意路径可读」
   再开一个入口，风险大于收益；
2. 图标必须**真实可用**——引用一个不存在的 PNG 会让 Chrome 判为不可安装，
   而且失败是静默的。这里用纯 stdlib（zlib + struct）生成 PNG 字节并缓存，
   保证任何环境下 `/icons/*.png` 都返回真实图像，不依赖构建步骤；
3. `theme_color` 必须和页面一致：直接从 `agents_dashboard.html` 的
   `html[data-theme="dark"]` 里读 `--bg`（默认主题是深色），避免两处手写色值漂移。

**边界**：本模块只产出静态外壳资源，不读业务数据、不碰平台库。
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any

#: Service Worker 缓存版本。**改缓存策略/预缓存清单时必须改这里**，
#: 否则 `activate` 不会清掉旧缓存，客户端会一直吃旧资源。
SW_VERSION = "p3m6-v3"

#: manifest / SW 的路由路径（auth 白名单也用这几个常量，避免拼错）。
MANIFEST_PATH = "/manifest.webmanifest"
SW_PATH = "/sw.js"
OFFLINE_PATH = "/offline.html"

#: 页面文件：theme_color 与图标配色都从它读。
HTML_PATH = Path(__file__).resolve().parent.parent / "agents_dashboard.html"

#: 读不到页面时的兜底配色（与 agents_dashboard.html 深色主题一致）。
_FALLBACK_BG = "#0F172A"
_FALLBACK_ACCENT = "#4C9AFF"

_DARK_BLOCK = re.compile(
    r"""html\[data-theme=["']dark["']\]\s*\{(?P<body>[^}]*)\}""", re.DOTALL
)
_LIGHT_BLOCK = re.compile(r""":root\s*\{(?P<body>[^}]*)\}""", re.DOTALL)
_VAR = r"""--{}\s*:\s*(#[0-9a-fA-F]{{3,8}})"""


def _css_var(block: str, name: str) -> str | None:
    match = re.search(_VAR.format(re.escape(name)), block)
    return match.group(1) if match else None


@lru_cache(maxsize=4)
def _palette(mtime: float, _size: int) -> tuple[str, str]:
    """(背景色, 强调色)。入参让 lru_cache 跟着页面 mtime 失效。"""
    try:
        text = HTML_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _FALLBACK_BG, _FALLBACK_ACCENT
    dark = _DARK_BLOCK.search(text)
    light = _LIGHT_BLOCK.search(text)
    bg = (_css_var(dark.group("body"), "bg") if dark else None) or (
        _css_var(light.group("body"), "bg") if light else None
    ) or _FALLBACK_BG
    accent = (_css_var(dark.group("body"), "primary") if dark else None) or (
        _css_var(light.group("body"), "primary") if light else None
    ) or _FALLBACK_ACCENT
    return bg, accent


def _palette_now() -> tuple[str, str]:
    try:
        mtime = HTML_PATH.stat().st_mtime
    except OSError:
        mtime = 0.0
    return _palette(mtime, 0)


def theme_color() -> str:
    """页面深色主题的 --bg，用作 manifest 的 theme/background color。"""
    return _palette_now()[0]


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def manifest() -> bytes:
    """Web App Manifest。`start_url` / `scope` 都是根：整站即应用。"""
    bg = theme_color()
    payload: dict[str, Any] = {
        "id": "/",
        "name": "多项目 Agent 管理平台",
        "short_name": "Agent 平台",
        "description": "跨项目总指挥部看板：项目、任务、调度、预算与执行时间线。",
        "lang": "zh-CN",
        "dir": "ltr",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "any",
        "background_color": bg,
        "theme_color": bg,
        "icons": [
            {"src": ICON_ANY_192, "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": ICON_ANY_512, "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": ICON_MASKABLE_512, "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


# ---------------------------------------------------------------------------
# 图标（纯 stdlib 生成 PNG）
# ---------------------------------------------------------------------------
ICON_ANY_192 = "/icons/icon-192.png"
ICON_ANY_512 = "/icons/icon-512.png"
ICON_MASKABLE_512 = "/icons/icon-maskable-512.png"

#: 路由路径 → (边长, 是否 maskable)。
ICON_ROUTES: dict[str, tuple[int, bool]] = {
    ICON_ANY_192: (192, False),
    ICON_ANY_512: (512, False),
    ICON_MASKABLE_512: (512, True),
}


def _hex_rgb(value: str) -> tuple[int, int, int]:
    raw = value.lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    if len(raw) == 8:  # #RRGGBBAA：忽略 alpha，图标不透明
        raw = raw[:6]
    try:
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        return 15, 23, 42


def _inside_round_rect(px: float, py: float, x0: float, y0: float,
                       x1: float, y1: float, r: float) -> bool:
    """点是否落在圆角矩形内（圆角用圆心距判定）。"""
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    if r <= 0:
        return True
    cx = x0 + r if px < x0 + r else (min(px, x1 - r))
    cy = y0 + r if py < y0 + r else (min(py, y1 - r))
    dx = px - cx
    dy = py - cy
    return dx * dx + dy * dy <= r * r


def _png_bytes(size: int, pixels: bytes) -> bytes:
    """RGBA 像素 → PNG（filter 0，无调色板）。"""
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # 每行 filter type 0（None）
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


@lru_cache(maxsize=8)
def icon_png(size: int, maskable: bool = False) -> bytes:
    """生成图标：深色圆角底 + 四宫格品牌标记（与侧栏 brand-mark 同构）。

    `maskable=True` 时底色铺满整块画布（Android 会按自己的形状裁切），
    图形收进中心安全区，避免被裁掉。
    """
    bg_hex, accent_hex = _palette_now()
    bg = _hex_rgb(bg_hex)
    accent = _hex_rgb(accent_hex)

    # viewBox 24 里的品牌标记：三个方格 + 右下角一个加号。
    boxes = [(3.0, 3.0, 10.0, 10.0), (14.0, 3.0, 21.0, 10.0), (3.0, 14.0, 10.0, 21.0)]
    plus_h = (13.5, 16.6, 21.5, 18.4)
    plus_v = (16.6, 13.5, 18.4, 21.5)

    # 内容盒：maskable 要留安全区（中心 80% 内）
    span = size * (0.56 if maskable else 0.66)
    origin = (size - span) / 2.0
    scale = span / 24.0

    def to_px(box: tuple[float, float, float, float], radius: float) -> tuple[float, float, float, float, float]:
        x0, y0, x1, y1 = box
        return (origin + x0 * scale, origin + y0 * scale,
                origin + x1 * scale, origin + y1 * scale, radius * scale)

    shapes = [to_px(b, 1.4) for b in boxes] + [to_px(plus_h, 0.9), to_px(plus_v, 0.9)]

    # 底色：非 maskable 用圆角 + 透明角，maskable 铺满整块
    radius = 0.0 if maskable else size * 0.2
    bg_box = (0.0, 0.0, float(size), float(size), radius)

    pixels = bytearray(size * size * 4)
    # 每像素 2x2 超采样做抗锯齿；背景圆角只影响四个角，先用「角点全在内部」
    # 快路径避免整幅逐点采样——512 图标也只是几十毫秒。
    for y in range(size):
        py0 = y + 0.25
        py1 = y + 0.75
        row = y * size * 4
        for x in range(size):
            px0 = x + 0.25
            px1 = x + 0.75
            if _inside_round_rect(px0, py0, *bg_box) and _inside_round_rect(px1, py1, *bg_box) \
                    and _inside_round_rect(px1, py0, *bg_box) and _inside_round_rect(px0, py1, *bg_box):
                bg_cov = 4
            else:
                bg_cov = sum(
                    1 for (sx, sy) in ((px0, py0), (px1, py0), (px0, py1), (px1, py1))
                    if _inside_round_rect(sx, sy, *bg_box)
                )
            if bg_cov == 0:
                continue  # 透明角

            mark_cov = 0
            for shape in shapes:
                sx0, sy0, sx1, sy1, _r = shape
                if not (px0 <= sx1 and px1 >= sx0 and py0 <= sy1 and py1 >= sy0):
                    continue  # 包围盒外的整像素直接跳过
                mark_cov += sum(
                    1 for (sx, sy) in ((px0, py0), (px1, py0), (px0, py1), (px1, py1))
                    if _inside_round_rect(sx, sy, *shape)
                )

            bg_alpha = 255 * bg_cov / 4
            mark_alpha = min(1.0, mark_cov / 4)
            if mark_alpha <= 0:
                color = bg
                alpha = bg_alpha
            else:
                # 标记盖在底色上：按覆盖率混合
                color = tuple(
                    int(b * (1 - mark_alpha) + a * mark_alpha) for b, a in zip(bg, accent)
                )
                alpha = bg_alpha
            idx = row + x * 4
            pixels[idx] = color[0]
            pixels[idx + 1] = color[1]
            pixels[idx + 2] = color[2]
            pixels[idx + 3] = int(alpha)

    return _png_bytes(size, bytes(pixels))


def icon_for(path: str) -> tuple[bytes, str] | None:
    """路径 → (PNG 字节, MIME)，未登记返回 None。"""
    spec = ICON_ROUTES.get(path)
    if spec is None:
        return None
    size, maskable = spec
    return icon_png(size, maskable), "image/png"


# ---------------------------------------------------------------------------
# Service Worker
# ---------------------------------------------------------------------------
_SW_TEMPLATE = """/* 多项目 Agent 管理平台 · Service Worker（P3-M6 / ADR-P3-005）
 *
 * 缓存策略（硬约束，别改）：
 *   - /api/*  一律放行到网络，**绝不缓存**。缓存 API 会让人离线看到陈旧数据，
 *             比白屏更危险（V4.0：离线或未送达命令不得显示成功）。
 *   - 静态资源（离线页 / 图标 / manifest）：Cache-First。
 *   - 首页文档：Cache-First + 后台刷新（立即出界面，下一次导航拿到新版本）。
 *
 * 本文件由服务端按版本号渲染；改缓存策略必须同时改 SW_VERSION。
 */
const VERSION = "__VERSION__";
const STATIC_CACHE = `agws-static-${VERSION}`;
const OFFLINE_URL = "__OFFLINE__";

// 预缓存：装不上任何一个都不该让 SW 整体失败，故用 allSettled 兜底。
//
// 已知限制：远端实例开了令牌鉴权（AGWS_PLATFORM_AUTH=on）时，令牌存在页面的
// localStorage 里，SW 读不到也带不上 Authorization 头，因此 "/" 的预缓存会
// 401 失败。外壳资源（manifest / sw.js / 图标 / offline.html）在服务端已放行，
// 仍然能进缓存，所以离线时至少能显示离线说明页。要真正离线出看板外壳，
// 需要在页面注册成功后把令牌交给 SW（postMessage + IndexedDB）。
const PRECACHE = [
  OFFLINE_URL,
  "__MANIFEST__",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/icon-maskable-512.png",
  "/",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(STATIC_CACHE);
    const results = await Promise.allSettled(
      PRECACHE.map((url) => cache.add(new Request(url, { cache: "reload" })))
    );
    const failed = PRECACHE.filter((_url, i) => results[i].status !== "fulfilled");
    if (failed.length) {
      console.warn("[sw] 预缓存部分失败（不影响已缓存部分）：", failed);
    }
    // 立即接管，避免开发期新旧 SW 并存
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const keep = new Set([STATIC_CACHE]);
    const names = await caches.keys();
    await Promise.all(
      names.filter((name) => !keep.has(name)).map((name) => caches.delete(name))
    );
    await self.clients.claim();
  })());
});

async function cacheFirst(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request, { ignoreSearch: true });
  if (cached) return cached;
  const response = await fetch(request);
  if (response && response.ok) {
    cache.put(request, response.clone()).catch(() => {});
  }
  return response;
}

async function navigate(request) {
  const cache = await caches.open(STATIC_CACHE);
  const url = new URL(request.url);
  const isShell = url.pathname === "/" || url.pathname === "/index.html";

  // 非壳页面（如 /offline.html）：先精确命中缓存，再走网络，最后才降级。
  // 早期实现把所有导航都返回壳，导致「离线说明页」本身被壳替换，
  // 离线时反而打不开它——这正是离线降级页存在的意义。
  if (!isShell) {
    const exact = await cache.match(request, { ignoreSearch: true });
    if (exact) return exact;
    let fresh = null;
    try {
      fresh = await fetch(request);
    } catch {
      fresh = null;  // 网络层失败：才是真正的「离线」
    }
    if (fresh) {
      // 404 / 500 是服务端的真实答复，必须原样返回。
      // 用离线页顶替它，会把「这个地址不存在」说成「你没网」——谎报状态。
      if (fresh.ok) cache.put(request, fresh.clone()).catch(() => {});
      return fresh;
    }
    const fallback = await cache.match(OFFLINE_URL, { ignoreSearch: true });
    return fallback || new Response(
      "<!doctype html><meta charset=\"utf-8\"><title>离线</title>"
        + "<p style=\"font:16px/1.6 system-ui;padding:24px\">当前离线。</p>",
      { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
    );
  }

  const cachedShell = await cache.match("/", { ignoreSearch: true });
  // 后台刷新：不阻塞本次返回，下一屏就是新版本
  const revalidate = fetch(request)
    .then((response) => {
      if (response && response.ok) {
        cache.put("/", response.clone()).catch(() => {});
      }
      return response;
    })
    .catch(() => null);

  if (cachedShell) return cachedShell;

  const fresh = await revalidate;
  if (fresh) return fresh;

  const offline = await cache.match(OFFLINE_URL, { ignoreSearch: true });
  if (offline) return offline;
  return new Response(
    "<!doctype html><meta charset=\\"utf-8\\"><title>离线</title>"
      + "<p style=\\"font:16px/1.6 system-ui;padding:24px\\">当前离线，页面资源尚未缓存。</p>",
    { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } }
  );
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;              // 写操作永不代理
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;   // 跨域直连
  if (url.pathname.startsWith("/api/")) return;      // API：Network-Only，绝不缓存
  if (request.headers.has("range")) return;          // 断点续传/大文件不走缓存

  if (request.mode === "navigate") {
    event.respondWith(navigate(request));
    return;
  }
  event.respondWith(
    cacheFirst(request).catch(() => new Response("", { status: 504 }))
  );
});

self.addEventListener("message", (event) => {
  // 页面可以主动要求立即更新缓存（调试用）
  if (event.data === "skip-waiting") self.skipWaiting();
});
"""


def service_worker() -> bytes:
    return (_SW_TEMPLATE
            .replace("__VERSION__", SW_VERSION)
            .replace("__OFFLINE__", OFFLINE_PATH)
            .replace("__MANIFEST__", MANIFEST_PATH)
            .encode("utf-8"))


# ---------------------------------------------------------------------------
# 离线降级页
# ---------------------------------------------------------------------------
_OFFLINE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>离线 · 多项目 Agent 管理平台</title>
<style>
:root {
  --bg: __BG__; --panel: #1B2336; --line: #2B3648;
  --text: #E6EDF7; --text-2: #C4D0E0; --muted: #94A3B8; --accent: __ACCENT__;
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; background: var(--bg); color: var(--text);
  font: 14px/1.7 -apple-system, BlinkMacSystemFont, "SF Pro SC", "PingFang SC", "Segoe UI", system-ui, sans-serif;
  display: flex; align-items: center; justify-content: center; padding: 24px;
}
.card {
  width: 100%; max-width: 420px; background: var(--panel); border: 1px solid var(--line);
  border-radius: 16px; padding: 28px 24px; text-align: center;
}
.mark {
  width: 56px; height: 56px; margin: 0 auto 18px; border-radius: 14px;
  display: grid; grid-template-columns: 1fr 1fr; gap: 5px; padding: 11px;
  background: rgba(76,154,255,.14);
}
.mark span { background: var(--accent); border-radius: 3px; }
h1 { font-size: 18px; margin: 0 0 10px; }
p { margin: 0 0 10px; color: var(--text-2); }
.hint { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
button {
  font: inherit; cursor: pointer; padding: 9px 20px; border-radius: 9px;
  border: 1px solid var(--accent); background: transparent; color: var(--accent);
}
button:hover { background: rgba(76,154,255,.12); }
</style>
</head>
<body>
  <main class="card">
    <div class="mark" aria-hidden="true"><span></span><span></span><span></span><span></span></div>
    <h1>当前处于离线状态</h1>
    <p>这个页面只是暂时连不上平台服务，不是出错，数据也没有丢失。</p>
    <p class="hint">看板数据一律实时读取，不做离线缓存——离线时不会显示任何可能是旧的数字。</p>
    <button type="button" onclick="location.reload()">重新连接</button>
  </main>
</body>
</html>
"""


def offline_page() -> bytes:
    bg, accent = _palette_now()
    return (_OFFLINE_HTML.replace("__BG__", bg).replace("__ACCENT__", accent)
            .encode("utf-8"))
