# P3-M6 · PWA + 推送通知 · 前置条件分析

> 日期：2026-11-15
> 触发：用户决定「PWA 推送通知：需要」
> 结论：**有两个硬前置，当前都不满足**

---

## 问题本质

Service Worker 只在**安全上下文**（Secure Context）下可注册：

| 访问方式 | 安全上下文 | 能装 PWA | 能收推送 |
|---|---|---|---|
| `http://127.0.0.1:8099` | ✅ | ✅ | ✅ |
| `http://localhost:8099` | ✅ | ✅ | ✅ |
| `http://192.168.1.x:8099` | ❌ | ❌ | ❌ |
| `https://*.ts.net`（Tailscale） | ✅ | ✅ | ✅ |

**手机上通过局域网 IP 访问 → 装不了 PWA，也注册不了 Service Worker，更收不到推送。**

这不是实现问题，是浏览器安全模型。

---

## 当前状态（实测）

```
$ lsof -nP -iTCP:8099 -sTCP:LISTEN
python3.1  60277  hualei  3u  IPv4 ... TCP 127.0.0.1:8099 (LISTEN)
```

- 只监听 **127.0.0.1**，手机根本连不上
- Tailscale 已安装但**处于 stopped 状态**

---

## 三个硬前置

### 前置 1 · 服务要能被手机访问

当前 `bind_hosts` 只有回环。需要加局域网或 tailnet 监听。

**风险**：一旦监听非回环，`auth.py` 的 token 鉴权就成了唯一屏障。
现有设计已处理（远端必须 Bearer token），但需要实测确认。

### 前置 2 · 必须是 HTTPS（或 localhost）

| 方案 | 成本 | 体验 | 评价 |
|---|---|---|---|
| **A. Tailscale HTTPS** | 低 | 好 | **推荐**：`tailscale cert` 自动签发，tailnet 内可信 |
| B. 自签证书 | 中 | 差 | 手机要装 CA，每次可能被浏览器警告 |
| C. 内网穿透 + 公网 HTTPS | 中 | 好 | 引入外部依赖，暴露面变大 |
| D. 只在本机用 PWA | 零 | 无意义 | 手机装不了 |

### 前置 3 · VAPID 密钥

Web Push 需要 VAPID 密钥对。**密钥绝不能进仓库**（此前已因 `.npmrc` 泄漏过
一次代理配置，做过全历史脱敏）。

存放位置：`data/vapid-private.pem`（0600，`.gitignore` 已覆盖 `data/`）。

---

## 建议路径

```
1. 用户启动 Tailscale（tailscale up）
2. 平台以 --tailscale 模式运行，监听 tailnet 地址
3. tailscale cert <host>.ts.net 取证书
4. 平台用 HTTPS 提供服务
5. 手机访问 https://<host>.ts.net:8099 → SW 可注册 → PWA 可安装 → 推送可用
```

**这条路径不需要公网暴露，不需要买证书，不引入第三方。**

---

## 现在的处理

**P3-M6 暂时挂起**，不是因为难，是因为前置没就绪。
在 HTTP + 局域网 IP 下写 SW/推送代码，写了也验证不了——那等于假装做完了。

**继续推进不受影响的部分**：
- M3 三模式（Full Auto 的地基，最关键）
- M7 预算与效能（Full Auto 必须有刹车）
- M5 Global Task（缩水版）

PWA 待用户确认 Tailscale 方案后启动。
