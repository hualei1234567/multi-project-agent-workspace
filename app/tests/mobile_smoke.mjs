/**
 * 移动端冒烟（三期 M3-4）：iPhone 390 / iPad 768 两档视口。
 *
 * 覆盖：无横向溢出、无 JS 错误、汉堡菜单开合、路由切换自动收起侧栏。
 * 用法：AGWS_URL=http://127.0.0.1:8098 node app/tests/mobile_smoke.mjs
 */
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || (() => {
  const candidates = ["playwright", join(HERE, "..", "..", "node_modules", "playwright")];
  for (const c of candidates) {
    try { return require.resolve(c); } catch { /* 继续找 */ }
  }
  console.error("[smoke] 找不到 playwright，请先 npm install");
  process.exit(2);
})());
// 远端实例需要令牌：设置 AGWS_TOKEN 后，每个页面加载前先写进 localStorage。
const TOKEN = process.env.AGWS_TOKEN || "";
const AUTH = TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {};
async function prepareContext(context) {
  if (!TOKEN) return;
  await context.addInitScript((t) => {
    try { localStorage.setItem("agws-token", t); } catch { /* 隐私模式忽略 */ }
  }, TOKEN);
}

const BASE = process.env.AGWS_URL || "http://127.0.0.1:8098";
const browser = await chromium.launch({ headless: true, executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" });
const results = [];
const check = (ok, name, detail="") => { results.push({ok,name,detail}); console.log(`${ok?"[PASS]":"[FAIL]"} ${name}${detail?` — ${detail}`:""}`); };

for (const vp of [{w:390,h:844,name:"iPhone"},{w:768,h:1024,name:"iPad"}]) {
  const context = await browser.newContext({ viewport:{width:vp.w,height:vp.h}, isMobile: vp.w<500, hasTouch: vp.w<500 });
  await prepareContext(context);
  const page = await context.newPage();
  const errs = []; page.on("pageerror", e=>errs.push(String(e)));
  await page.goto(`${BASE}/#/p/material/dispatch`, {waitUntil:"domcontentloaded"});
  await page.waitForTimeout(3500);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 2);
  check(!overflow, `${vp.name} 无横向溢出`, `scrollW=${await page.evaluate(()=>document.documentElement.scrollWidth)} / vw=${vp.w}`);
  check(errs.length===0, `${vp.name} 无 JS 错误`, errs.slice(0,1).join(""));
  if (vp.w < 500) {
    const menuVisible = await page.locator("#btn-menu").isVisible();
    check(menuVisible, `${vp.name} 汉堡菜单可见`);
    await page.locator("#btn-menu").click();
    await page.waitForTimeout(600);
    const sidebarOpen = await page.locator("#sidebar.open").count();
    check(sidebarOpen===1, `${vp.name} 点菜单打开侧栏`);
    // 点导航后应自动收起（B-9 修复点）
    await page.locator('#sb-projects .sb-proj[data-pid="material"]').click().catch(()=>{});
    await page.waitForTimeout(1200);
    const stillOpen = await page.locator("#sidebar.open").count();
    check(stillOpen===0, `${vp.name} 路由切换后侧栏自动收起`);
  }
  await page.close();
}

/* ---------- PWA 外壳（P3-M6 / ADR-P3-005）----------
 * 三个断言都对着真实 HTTP 与真实浏览器：
 *   - manifest / sw.js 的 MIME 与 JSON 合法；
 *   - SW 在安全上下文（localhost / 127.0.0.1）下确实注册成功；
 *   - /api/* 不进 Cache Storage（缓存了就会看到假数据）。
 * 非安全上下文（http://局域网IP）下 SW 装不上是浏览器安全模型，不算失败：
 * 那种情况只要求页面无报错、功能正常。
 */
{
  const res = await fetch(`${BASE}/manifest.webmanifest`, { headers: AUTH });
  const manifestType = res.headers.get("content-type") || "";
  let manifest = null;
  try { manifest = await res.json(); } catch { /* 下面断言会失败 */ }
  check(res.status === 200, "manifest.webmanifest 返回 200", `status=${res.status}`);
  check(manifestType.startsWith("application/manifest+json"),
        "manifest MIME 为 application/manifest+json", manifestType);
  check(!!manifest && manifest.start_url === "/" && manifest.scope === "/",
        "manifest start_url/scope 为根路径",
        manifest ? `start_url=${manifest.start_url} scope=${manifest.scope}` : "JSON 非法");
  check(!!manifest && manifest.display === "standalone", "manifest display=standalone");

  const swRes = await fetch(`${BASE}/sw.js`, { headers: AUTH });
  const swType = swRes.headers.get("content-type") || "";
  check(swRes.status === 200, "sw.js 返回 200", `status=${swRes.status}`);
  check(swType.startsWith("application/javascript"), "sw.js MIME 为 application/javascript", swType);

  // 每个图标都必须真的存在（404 会让 PWA 静默不可安装）
  const icons = (manifest && manifest.icons) || [];
  const iconStatus = [];
  for (const icon of icons) {
    const r = await fetch(`${BASE}${icon.src}`, { headers: AUTH });
    iconStatus.push(`${icon.src}=${r.status}`);
  }
  check(icons.length > 0 && iconStatus.every(s => s.endsWith("=200")),
        "manifest 图标全部可访问", iconStatus.join(" "));

  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  await prepareContext(context);
  const page = await context.newPage();
  const errs = []; page.on("pageerror", e => errs.push(String(e)));
  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  const secure = await page.evaluate(() => window.isSecureContext);
  if (secure) {
    const reg = await page.evaluate(async () => {
      if (!("serviceWorker" in navigator)) return { ok: false, err: "不支持 serviceWorker" };
      try {
        await navigator.serviceWorker.register("/sw.js");
        const ready = await navigator.serviceWorker.ready;
        return { ok: true, scope: ready.scope };
      } catch (e) { return { ok: false, err: String(e) }; }
    });
    check(reg.ok, "Service Worker 注册成功", reg.ok ? `scope=${reg.scope}` : reg.err);

    if (reg.ok) {
      // 等预缓存落盘（install 里跑完了 active 才算数）
      await page.waitForTimeout(1500);
      const apiStatus = await page.evaluate(async () => {
        try { const r = await fetch("/api/projects"); return `HTTP ${r.status}`; }
        catch (e) { return String(e); }
      });
      const cachedPaths = await page.evaluate(async () => {
        const out = [];
        for (const name of await caches.keys()) {
          const cache = await caches.open(name);
          for (const req of await cache.keys()) out.push(new URL(req.url).pathname);
        }
        return out;
      });
      check(cachedPaths.length > 0, "预缓存已建立", cachedPaths.join(" "));
      check(!cachedPaths.some(p => p.startsWith("/api/")),
            "API 未被缓存（Network-Only）", apiStatus);
    }
  } else {
    // 非安全上下文：允许装不上，但不允许页面出错
    check(true, "非安全上下文：跳过 SW 注册断言", "浏览器安全模型限制，非缺陷");
  }
  check(errs.length === 0, "PWA 接入不影响页面（无 JS 错误）", errs.slice(0, 1).join(""));
  await context.close();
}

await browser.close();
const fail = results.filter(r=>!r.ok).length;
console.log(`\n用例 ${results.length}，通过 ${results.length-fail}，失败 ${fail}`);
process.exit(fail?1:0);
