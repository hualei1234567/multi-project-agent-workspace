/**
 * 移动端冒烟（三期 M3-4）：iPhone 390 / iPad 768 两档视口。
 *
 * 覆盖：无横向溢出、无 JS 错误、汉堡菜单开合、路由切换自动收起侧栏。
 * 用法：AGWS_URL=http://127.0.0.1:8098 node app/tests/mobile_smoke.mjs
 */
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { chromium } = require("/web/node_modules/playwright");
const BASE = process.env.AGWS_URL || "http://127.0.0.1:8098";
const browser = await chromium.launch({ headless: true, executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" });
const results = [];
const check = (ok, name, detail="") => { results.push({ok,name,detail}); console.log(`${ok?"[PASS]":"[FAIL]"} ${name}${detail?` — ${detail}`:""}`); };

for (const vp of [{w:390,h:844,name:"iPhone"},{w:768,h:1024,name:"iPad"}]) {
  const page = await browser.newPage({ viewport:{width:vp.w,height:vp.h}, isMobile: vp.w<500, hasTouch: vp.w<500 });
  const errs = []; page.on("pageerror", e=>errs.push(String(e)));
  await page.goto(`${BASE}/#/p/ddp/dispatch`, {waitUntil:"domcontentloaded"});
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
await browser.close();
const fail = results.filter(r=>!r.ok).length;
console.log(`\n用例 ${results.length}，通过 ${results.length-fail}，失败 ${fail}`);
process.exit(fail?1:0);
