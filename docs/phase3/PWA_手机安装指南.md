# PWA 手机安装指南

> 更新：2026-11-15
> 状态：**服务端已就绪，等手机连接**

---

## 已完成

| 项 | 状态 | 证据 |
|---|---|---|
| manifest.webmanifest | ✅ 200 `application/manifest+json` | curl 实测 |
| sw.js | ✅ 200 `application/javascript` | curl 实测 |
| offline.html | ✅ 200 `text/html` | curl 实测 |
| 监听 tailnet 接口 | ✅ `100.117.129.23:8099` | `lsof` 实测 |
| HTTPS 证书 | ✅ 有效至 2027-01-30 | `openssl x509` |
| HTTPS 反代 | ✅ 443 → 8099 | `tailscale serve status` |

---

## 手机安装步骤

### 前提
1. 手机已装 Tailscale 并登录同一账号（`hl_930@`）
2. 手机 Tailscale 已连接（当前两台 Android 是 offline，需要打开）

### 步骤

```
1. 手机上确认 Tailscale 已连接
2. 浏览器打开：https://mac.taile81130.ts.net/
3. 首次会要求 token：
   - 在 Mac 上执行：cat ~/Multi-Project-Agent-Workspace/data/token
   - 访问 https://mac.taile81130.ts.net/?token=<那个值>
4. 浏览器菜单 →「添加到主屏幕」
5. 从主屏幕启动，应该没有浏览器地址栏（standalone 模式）
```

**必须先完成第 3 步**——远端访问需要 Bearer token，没 token 会 401。

---

## 为什么必须用 `https://` 而不是 `http://<IP>`

Service Worker 只在**安全上下文**注册：

| 访问方式 | SW 可注册 | PWA 可装 | 推送可用 |
|---|---|---|---|
| `https://mac.taile81130.ts.net` | ✅ | ✅ | ✅ |
| `http://127.0.0.1:8099` | ✅ | ✅（仅本机） | ✅ |
| `http://100.117.129.23:8099` | ❌ | ❌ | ❌ |

这不是实现问题，是浏览器安全模型。所以配了 tailscale serve 的 HTTPS。

---

## 已验证 / 未验证

**已验证**（本机 curl）：
- 三个静态资源返回 200 且 MIME 正确
- 8099 同时监听回环与 tailnet
- 证书签发成功、有效期正确
- tailscale serve 配置生效

**未验证**（需要真机）：
- 手机上 SW 注册是否成功
- 「添加到主屏幕」是否出现
- standalone 模式下页面表现
- 离线打开是否显示 offline.html

**本机无法验证的原因**：Mac 访问自己的 tailnet 域名会走本地短路
（`tailscale ping` 报 `is local Tailscale IP`），curl 连不通。
这不影响手机——手机是真正的远端节点。

---

## 回退

```bash
# 关闭 HTTPS 反代
tailscale serve --https=443 off

# 关闭 tailnet 监听（回到只监听本机）
AGWS_PLATFORM_TAILSCALE=off launchctl kickstart -k gui/$(id -u)/com.hualei.agent-platform
```
