#!/usr/bin/env node
/**
 * 多项目 Agent 平台 · 阶段 1 浏览器冒烟（Playwright + 本机 Chrome）。
 *
 * 前置：平台实例已运行，默认 http://127.0.0.1:8098（AGWS_URL 覆盖）。
 * 依赖：web/node_modules/playwright（不额外安装浏览器，使用 Playwright 自带 chromium）。
 *
 * 覆盖：
 *   1) 平台总览标题 / 2 项目侧栏 / KPI 与 /api/agent-platform/overview 一致
 *   2) 切换 demo-b：URL、Token 0、花费 —、任务 0；再切回 ddp
 *   3) 切换竞态：延迟目标项目 /api/state，快速切换，旧响应不得覆盖当前项目
 *   4) Tab 深链、刷新保持、前进后退
 *   5) 旧链接 /?agent=<id>&theme=dark 重定向
 *   6) 新建项目弹窗（不填仓库）→ 侧栏出现、状态空 → 清理测试项目
 *
 * 用法：node scripts/tests/ui_smoke.mjs
 * 退出码：0 全通过；1 有失败；2 环境不可用。
 */

import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, rmdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || (() => {
  // 平台已独立于任何仓库（2026-11-04）：优先本机安装，其次复用 DDP 前端的 node_modules
  const candidates = [
    "playwright",
    join(HERE, "node_modules", "playwright"),
    join(HERE, "..", "node_modules", "playwright"),
    "<HOME>/vscodeworkspace/DenseAIDataPlatform/web/node_modules/playwright",
  ];
  for (const c of candidates) {
    try { return require.resolve(c); } catch { /* 继续找 */ }
  }
  console.error("[smoke] 找不到 playwright，请设置 PLAYWRIGHT_MODULE 或安装依赖");
  process.exit(2);
})());

const BASE = (process.env.AGWS_URL || "http://127.0.0.1:8098").replace(/\/$/, "");
// 清理测试项目要写**被测实例**的库：优先 AGWS_DB，其次实例启动用的 DDP_AGENT_PLATFORM_DB，
// 最后回落到工作区默认库。以前默认写死一个 /tmp 路径，清理悄悄落在别的库上，
// 侧栏看起来「没恢复」，实际是脏数据留在被测库里。
const DB_PATH = process.env.AGWS_DB
  || process.env.DDP_AGENT_PLATFORM_DB
  || join(HERE, "..", "..", "data", "platform.db");
const SHOT_DIR = join("/tmp", "agws-smoke");
const results = [];

function check(cond, label, detail = "") {
  results.push({ cond: Boolean(cond), label, detail });
  const tag = cond ? "PASS" : "FAIL";
  console.log(`[${tag}] ${label}${detail ? ` — ${detail}` : ""}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path) {
  const res = await fetch(BASE + path, { cache: "no-store" });
  const body = await res.json().catch(() => null);
  return { status: res.status, body };
}

const fmtNum = (n) => (n == null ? "—" : Number(n) >= 1e6 ? (Number(n) / 1e6).toFixed(2) + "M" : Number(n) >= 1e3 ? (Number(n) / 1e3).toFixed(1) + "k" : String(n));
const fmtUsd = (n) => (n == null || n === "" ? "—" : `$${Number(n).toFixed(2)}`);
const PLATFORM_TITLE = "平台总览 · 多项目 Agent 管理平台";

async function kpiValue(page, label) {
  const loc = page.locator(".kpi", { has: page.locator(".label", { hasText: label }) }).first();
  await loc.waitFor({ state: "visible", timeout: 25000 }).catch(() => {});
  if (!(await loc.count())) return null;
  return (await loc.locator(".value").first().innerText()).trim();
}

/** 项目页花费：新版在「用量」面板以「预估成本」呈现；若存在 近 24h 花费 KPI 则优先用它。 */
async function projectCostText(page) {
  const kpi = page.locator(".kpi", { has: page.locator(".label", { hasText: "近 24h 花费" }) }).first();
  if (await kpi.count()) return (await kpi.locator(".value").first().innerText()).trim();
  const usage = page.locator("#usage-rich .u-split").first();
  await usage.waitFor({ state: "visible", timeout: 25000 }).catch(() => {});
  const text = (await usage.innerText().catch(() => "")).replace(/\s+/g, " ");
  const m = text.match(/预估成本\s*(\S+)/);
  return m ? m[1] : null;
}

const TAB_LABELS = ["概览", "Agent 看板", "任务看板", "调度", "执行记录", "交付记录", "文档配置", "项目设置"];

/** 解析 UI 格式化数字：488.91M / 9.5k / 123 / $8.29。 */
function parseUiNumber(text) {
  if (text == null || text === "" || text === "—") return null;
  const m = String(text).replace(/[$,]/g, "").trim().match(/^([\d.]+)\s*([Mk]?)$/);
  if (!m) return null;
  const n = Number(m[1]);
  if (Number.isNaN(n)) return null;
  const unit = { M: 1e6, k: 1e3, "": 1 }[m[2]];
  return n * unit;
}

/** 实时数据可能在上一次 API 快照后继续增长，按 ±2% 容差比较。 */
function withinTolerance(ui, api, pct = 0.02) {
  const a = parseUiNumber(ui);
  const b = Number(api);
  if (a == null || Number.isNaN(b)) return false;
  return Math.abs(a - b) <= Math.max(Math.abs(b) * pct, 0.02);
}

async function settle(page, ms = 700) {
  await page.waitForLoadState("domcontentloaded").catch(() => {});
  await sleep(ms);
}

/** 等待项目概览 KPI 渲染完成（state 加载可能慢于固定 sleep）。 */
async function waitOverviewReady(page) {
  await page
    .waitForFunction(() => document.querySelectorAll("#tab-content .kpi").length >= 5, null, { timeout: 25000 })
    .catch(() => {});
  await sleep(100);
}

async function waitText(page, marker) {
  await page.getByText(marker, { exact: false }).first().waitFor({ state: "visible", timeout: 25000 }).catch(() => {});
}

async function activePid(page) {
  const el = page.locator("#sb-projects .sb-proj.active").first();
  if (!(await el.count())) return null;
  return await el.getAttribute("data-pid");
}

function cleanupProject(code) {
  if (!existsSync(DB_PATH)) {
    console.log(`[WARN] 清理跳过：库不存在 ${DB_PATH}（用 AGWS_DB 指向被测实例的库）`);
    return;
  }
  const sql = [
    "BEGIN;",
    `DELETE FROM token_usage_events WHERE project_id='${code}';`,
    `DELETE FROM agent_events WHERE project_id='${code}';`,
    `DELETE FROM deliveries WHERE project_id='${code}';`,
    `DELETE FROM tasks WHERE project_id='${code}';`,
    `DELETE FROM project_agents WHERE project_id='${code}';`,
    `DELETE FROM agent_projects WHERE id='${code}';`,
    "COMMIT;",
  ].join(" ");
  try {
    execFileSync("sqlite3", [DB_PATH, sql], { stdio: "pipe" });
    console.log(`[INFO] 已清理测试项目数据库记录：${code}`);
  } catch (e) {
    console.log(`[WARN] 测试项目清理失败（可手动删除）：${code} — ${e.message}`);
  }
  try {
    rmdirSync(`/tmp/projects/${code}`);
    console.log(`[INFO] 已清理测试项目工作区：/tmp/projects/${code}`);
  } catch {
    /* 目录可能非空或不在预期位置，忽略 */
  }
}

async function main() {
  // 环境可用性
  let overview;
  let projects;
  try {
    const h = await api("/api/projects");
    if (h.status !== 200) throw new Error(`/api/projects HTTP ${h.status}`);
    projects = h.body;
    overview = (await api("/api/agent-platform/overview")).body;
  } catch (e) {
    console.log(`[FAIL] 平台实例不可用（${BASE}）：${e.message}`);
    process.exit(2);
  }

  mkdirSync(SHOT_DIR, { recursive: true });

  // 夹具自带：demo-b 必须存在且名为「演示项目 B」。以前靠人工往库里塞，
  // 换个机器跑就会因夹具缺失/改名而误报；这里缺了就用 API 建出来，收尾再删。
  // 基线项目数按实际配置算（ddp + material + …），不写死：新增纳管项目不该让冒烟失败
  const baselineIds = projects.items.map((p) => p.id).filter((id) => id !== "demo-b");
  const baselineCount = baselineIds.length;
  let fixtureCreated = false;
  if (!projects.items.some((p) => p.id === "demo-b")) {
    // 平台拒绝不存在的仓库路径，夹具目录自己建（收尾一并清理）
    mkdirSync("/tmp/projects/demo-b", { recursive: true });
    const r = await fetch(`${BASE}/api/projects`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        code: "demo-b",
        name: "演示项目 B",
        description: "隔离验证",
        repo_root: "/tmp/projects/demo-b",
      }),
    });
    if (!r.ok) {
      console.log(`[FAIL] 无法创建 demo-b 夹具：HTTP ${r.status}`);
      process.exit(2);
    }
    fixtureCreated = true;
    console.log("[INFO] 已创建 demo-b 夹具（收尾自动清理）");
    projects = (await api("/api/projects")).body;
  }

  // 本机 Playwright 浏览器未下载完整，优先用系统 Chrome；可用 AGWS_CHROME 覆盖。
  const chromePath = process.env.AGWS_CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const browser = await chromium.launch({ headless: true, executablePath: chromePath });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  try {
    // ---------------------------------------------------------------- 1) 总览
    await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
    await page.locator(".platform-kpis .kpi").first().waitFor({ state: "visible", timeout: 25000 }).catch(() => {});
    await settle(page, 500);
    check((await page.title()) === PLATFORM_TITLE, "平台总览标题正确", await page.title());
    const defaultTheme = await page.locator("html").getAttribute("data-theme");
    check(defaultTheme === "dark", "默认深色主题", String(defaultTheme));
    const navLabels = await page
      .locator("#nav-platform, #nav-projects, #nav-favorites, #nav-recent")
      .evaluateAll((els) => els.map((e) => e.textContent.trim()));
    check(
      navLabels.length === 4 && ["平台总览", "全部项目", "我的收藏", "最近访问"].every((t) => navLabels.includes(t)),
      "左侧 4 个导航齐全",
      navLabels.join(" / "),
    );
    const sidebarCount = await page.locator("#sb-projects .sb-proj").count();
    check(
      sidebarCount === baselineCount + 1,
      `左侧显示 ${baselineCount + 1} 个项目（基线 ${baselineCount} + demo-b 夹具）`,
      `实际 ${sidebarCount}`,
    );
    const pids = await page.locator("#sb-projects .sb-proj").evaluateAll((els) => els.map((e) => e.dataset.pid));
    check(pids.includes("ddp") && pids.includes("demo-b"), "侧栏包含 ddp 与 demo-b", pids.join(","));

    const expToken = fmtNum(overview.token_24h);
    const expCost = fmtUsd(overview.cost_24h);
    const uiToken = await kpiValue(page, "24h Token");
    const uiCost = await kpiValue(page, "近 24h 花费");
    check(withinTolerance(uiToken, overview.token_24h), "平台 KPI Token 与 API 一致（±2%）", `UI ${uiToken} / API ${expToken}`);
    check(withinTolerance(uiCost, overview.cost_24h), "平台 KPI 花费与 API 一致（±2%）", `UI ${uiCost} / API ${expCost}`);
    // 汇总口径：等于各项目相加（null 安全）
    const sumToken = projects.items.reduce((a, p) => a + (typeof p.stats?.token_24h === "number" ? p.stats.token_24h : 0), 0);
    const costs = projects.items.map((p) => p.stats?.cost_24h).filter((v) => typeof v === "number");
    const sumCost = costs.length ? Number(costs.reduce((a, b) => a + b, 0).toFixed(6)) : null;
    check(overview.token_24h === sumToken, "平台 Token 汇总=各项目之和", `${overview.token_24h} vs ${sumToken}`);
    check(costs.length === 0 || overview.cost_24h === sumCost, "平台花费汇总=各项目之和（null 安全）", `${overview.cost_24h} vs ${sumCost}`);
    await page.screenshot({ path: join(SHOT_DIR, "01-platform.png"), fullPage: true });

    // -------------------------------------------------------- 2) 切换 demo-b
    await page.locator('#sb-projects .sb-proj[data-pid="demo-b"]').click();
    await page.waitForFunction(() => location.hash === "#/p/demo-b/overview", null, { timeout: 5000 });
    await waitOverviewReady(page);
    check(new URL(page.url()).hash === "#/p/demo-b/overview", "切换 demo-b 后 URL 正确", new URL(page.url()).hash);
    const demoToken = await kpiValue(page, "今日 Token");
    const demoCost = await projectCostText(page);
    const demoDoing = await kpiValue(page, "进行中任务");
    check(demoToken === "0", "demo-b 今日 Token 为 0", String(demoToken));
    check(demoCost === "—", "demo-b 花费为 —", String(demoCost));
    check(demoDoing === "0", "demo-b 进行中任务为 0", String(demoDoing));
    check((await activePid(page)) === "demo-b", "侧栏高亮 demo-b", String(await activePid(page)));
    const tabLabels = await page.locator("#view .tabs .tab").evaluateAll((els) => els.map((e) => e.textContent.trim()));
    check(
      tabLabels.length === TAB_LABELS.length && TAB_LABELS.every((t) => tabLabels.includes(t)),
      `项目头 ${TAB_LABELS.length} 个 Tab 齐全（含调度与执行记录）`,
      tabLabels.join(" / "),
    );
    const ovText = await page.locator("#view").innerText();
    for (const marker of ["Worktree", "用量", "执行记录", "交付物"]) {
      check(ovText.includes(marker), `项目概览包含「${marker}」区块`, marker);
    }
    await page.screenshot({ path: join(SHOT_DIR, "02-demo-b.png"), fullPage: true });

    await page.locator('#sb-projects .sb-proj[data-pid="ddp"]').click();
    await page.waitForFunction(() => location.hash === "#/p/ddp/overview", null, { timeout: 5000 });
    await waitOverviewReady(page);
    const ddpToken = await kpiValue(page, "今日 Token");
    // 两条口径：UI 读 /api/state 的实时 transcript 扫描，/api/projects 的 stats 读回填进库的
    // token_usage_events。不先回填，活跃会话持续增长就会让两者越拉越远（不是 bug，是没对齐）。
    // 比较前触发一次回填并重取 API，让两条口径落在同一时刻。
    await fetch(`${BASE}/api/projects/ddp/backfill`, { method: "POST" });
    projects = (await api("/api/projects")).body;
    const ddpTokenApi = projects.items.find((p) => p.id === "ddp").stats.token_24h;
    check(withinTolerance(ddpToken, ddpTokenApi), "回填后 ddp 今日 Token 与 API 一致（±2%）", `UI ${ddpToken} / API ${fmtNum(ddpTokenApi)}`);
    check((await activePid(page)) === "ddp", "侧栏高亮 ddp", String(await activePid(page)));

    // ------------------------------------------------------------ 3) 竞态
    async function raceCase(delayedPid, firstPid, secondPid, label) {
      const pattern = new RegExp(`/api/state\\?project=${delayedPid}(?:&|$)`);
      const handler = async (route) => {
        await sleep(900);
        await route.continue().catch(() => {});
      };
      await page.route(pattern, handler);
      // 从任一项目出发，先点 first 再点 second
      if ((await activePid(page)) !== firstPid) {
        await page.locator(`#sb-projects .sb-proj[data-pid="${firstPid}"]`).click();
        await settle(page, 600);
      }
      await page.locator(`#sb-projects .sb-proj[data-pid="${firstPid}"]`).click();
      await sleep(120);
      await page.locator(`#sb-projects .sb-proj[data-pid="${secondPid}"]`).click();
      await sleep(1700);
      const hash = new URL(page.url()).hash;
      const pid = await activePid(page);
      const title = await page.title();
      const expectedName = projects.items.find((p) => p.id === secondPid).name;
      check(hash === `#/p/${secondPid}/overview`, `${label}：URL 停留在 ${secondPid}`, hash);
      check(pid === secondPid, `${label}：侧栏高亮 ${secondPid}`, String(pid));
      check(title.startsWith(expectedName), `${label}：文档标题属于 ${secondPid}`, title);
      await page.unroute(pattern, handler);
    }

    // 反向：延迟 ddp 响应，先点 ddp 再点 demo-b；旧 ddp 响应不得覆盖 demo-b
    await raceCase("ddp", "ddp", "demo-b", "竞态(延迟 ddp)");
    // 正向：延迟 demo-b 响应，先点 demo-b 再点 ddp
    await raceCase("demo-b", "demo-b", "ddp", "竞态(延迟 demo-b)");

    // --------------------------------------------------------- 4) Tab 深链
    const tabs = [
      ["agents", "角色花名册"],
      ["tasks", "任务看板"],
      ["dispatch", "调度"],
      ["executions", "执行记录"],
      ["deliveries", "交付记录"],
      ["docs", "文档与规范"],
      ["settings", "项目设置"],
    ];
    for (const [tab, marker] of tabs) {
      await page.goto(`${BASE}/#/p/ddp/${tab}`, { waitUntil: "domcontentloaded" });
      await waitText(page, marker);
      // 深链必须真的渲染出内容：只断言 marker 会漏掉「路由中断但无报错」（曾真实发生）
      const viewLen = await page.locator("#view").innerText().then((t) => t.trim().length).catch(() => 0);
      check(viewLen > 40, `深链 ${tab} 渲染出内容`, `${viewLen} 字符`);
      const sbCount = await page.locator(".sb-proj").count();
      check(sbCount >= 1, `深链 ${tab} 侧栏已渲染`, `${sbCount} 个项目`);
      await settle(page, 200);
      const hash = new URL(page.url()).hash;
      const found = await page.getByText(marker, { exact: false }).first().count();
      check(hash === `#/p/ddp/${tab}` && found > 0, `深链 #/p/ddp/${tab} 可达`, `hash=${hash} marker=${marker}`);
      // 刷新保持
      await page.reload({ waitUntil: "domcontentloaded" });
      await waitText(page, marker);
      await settle(page, 200);
      check(new URL(page.url()).hash === `#/p/ddp/${tab}`, `刷新后保持 #/p/ddp/${tab}`, new URL(page.url()).hash);
    }
    // 前进/后退
    await page.goto(`${BASE}/#/p/ddp/agents`, { waitUntil: "domcontentloaded" });
    await settle(page, 800);
    await page.goto(`${BASE}/#/p/ddp/tasks`, { waitUntil: "domcontentloaded" });
    await settle(page, 800);
    await page.goBack();
    await settle(page, 700);
    check(new URL(page.url()).hash === "#/p/ddp/agents", "后退回到 agents", new URL(page.url()).hash);
    await page.goForward();
    await settle(page, 700);
    check(new URL(page.url()).hash === "#/p/ddp/tasks", "前进回到 tasks", new URL(page.url()).hash);

    // ------------------------------------------------ 5) 旧链接与主题保留
    const legacyAgent = "legacy-agent-smoke";
    await page.goto(`${BASE}/?agent=${legacyAgent}&theme=dark`, { waitUntil: "domcontentloaded" });
    await settle(page, 1000);
    const legacyHash = new URL(page.url()).hash;
    const legacySearch = new URL(page.url()).search;
    const theme = await page.locator("html").getAttribute("data-theme");
    check(
      legacyHash === `#/p/ddp/agents?agent=${legacyAgent}`,
      "旧链接重定向到 #/p/ddp/agents?agent=...",
      legacyHash,
    );
    check(legacySearch.includes(`agent=${legacyAgent}`) && legacySearch.includes("theme=dark"), "旧查询参数保留（含 theme=dark）", legacySearch);
    check(theme === "dark", "?theme=dark 生效", String(theme));
    await page.screenshot({ path: join(SHOT_DIR, "03-legacy-dark.png"), fullPage: true });

    // ------------------------------------------------------- 6) 新建项目
    const code = `qa-ui-${Date.now().toString(36).slice(-5)}`;
    await page.goto(`${BASE}/#/`, { waitUntil: "domcontentloaded" });
    await settle(page, 900);
    await page.locator("#btn-new-project").click();
    await page.locator("#cp-name").waitFor({ state: "visible" });
    await page.locator("#cp-name").fill("QA 冒烟临时项目");
    await page.locator("#cp-code").fill(code);
    await page.locator("#cp-repo").fill("");
    await page.locator("#cp-submit").click();
    await page.waitForFunction((c) => location.hash === `#/p/${c}/overview`, code, { timeout: 8000 }).catch(() => {});
    await waitOverviewReady(page);
    const newHash = new URL(page.url()).hash;
    check(newHash === `#/p/${code}/overview`, "新建项目后进入其概览", newHash);
    const newInSidebar = await page.locator(`#sb-projects .sb-proj[data-pid="${code}"]`).count();
    check(newInSidebar === 1, "新建项目出现在侧栏", `count=${newInSidebar}`);
    const newToken = await kpiValue(page, "今日 Token");
    const newCost = await projectCostText(page);
    const newDoing = await kpiValue(page, "进行中任务");
    check(newToken === "0" && newCost === "—" && newDoing === "0", "新项目状态为空（Token 0 / 花费 — / 任务 0）", `${newToken} / ${newCost} / ${newDoing}`);
    await page.screenshot({ path: join(SHOT_DIR, "04-new-project.png"), fullPage: true });

    // 清理：删除测试项目，避免污染临时库；夹具是本轮新建的也一并删掉
    cleanupProject(code);
    if (fixtureCreated) cleanupProject("demo-b");
    // 侧栏项目列表有缓存，刷新后重新拉取 /api/projects
    await page.goto(`${BASE}/#/`, { waitUntil: "domcontentloaded" });
    await page.reload({ waitUntil: "domcontentloaded" });
    await settle(page, 1000);
    const afterCount = await page.locator("#sb-projects .sb-proj").count();
    const expectCount = fixtureCreated ? baselineCount : baselineCount + 1;
    check(afterCount === expectCount, `清理后侧栏恢复 ${expectCount} 个项目`, `实际 ${afterCount}`);
    const afterIds = await page.locator("#sb-projects .sb-proj").evaluateAll((els) => els.map((e) => e.dataset.pid));
    check(
      afterIds.every((id) => baselineIds.includes(id)),
      "清理后只剩基线项目",
      afterIds.join(","),
    );
  } finally {
    await context.close();
    await browser.close();
  }

  const failed = results.filter((r) => !r.cond);
  console.log("\n===== UI 冒烟汇总 =====");
  console.log(`用例 ${results.length}，通过 ${results.length - failed.length}，失败 ${failed.length}`);
  console.log(`截图目录：${SHOT_DIR}`);
  if (failed.length) {
    for (const f of failed) console.log(`  FAIL: ${f.label}${f.detail ? ` — ${f.detail}` : ""}`);
    process.exit(1);
  }
}

main().catch((e) => {
  console.error("[FATAL]", e);
  process.exit(1);
});
