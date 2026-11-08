#!/usr/bin/env node
/**
 * Agent Workspace 文件查看器冒烟：交付记录 / 文档配置两个入口的新标签页查看。
 *
 * 覆盖：
 *   1) 交付记录弹窗「新标签页查看」→ /view 返回 200 且 HTML 内容非空；
 *   2) 文档配置预览「新标签页查看」→ 同上；
 *   3) 目录穿越路径被 /view 拒绝（404，不泄露内容）；
 *   4) /view 返回 HTML 查看页而不是裸 text/plain（可独立打开）。
 *
 * 用法：AGWS_URL=http://127.0.0.1:8098 node scripts/tests/file_viewer_smoke.mjs
 * 退出码：0 全通过；1 有失败；2 环境不可用。
 */

import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || (() => {
  // 平台已独立于任何仓库（2026-11-04）：优先本机安装，其次复用业务前端的 node_modules
  const candidates = [
    "playwright",
    join(HERE, "node_modules", "playwright"),
    join(HERE, "..", "node_modules", "playwright"),
    "/web/node_modules/playwright",
  ];
  for (const c of candidates) {
    try { return require.resolve(c); } catch { /* 继续找 */ }
  }
  console.error("[smoke] 找不到 playwright，请设置 PLAYWRIGHT_MODULE 或安装依赖");
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

const BASE = (process.env.AGWS_URL || "http://127.0.0.1:8098").replace(/\/$/, "");
const results = [];
const check = (cond, label, detail = "") => {
  results.push(Boolean(cond));
  console.log(`[${cond ? "PASS" : "FAIL"}] ${label}${detail ? ` — ${detail}` : ""}`);
};

async function api(path) {
  const res = await fetch(BASE + path, { cache: "no-store", headers: AUTH });
  return { status: res.status, body: await res.json().catch(() => null) };
}

async function openModalAndGetHref(page, hash) {
  await page.goto(`${BASE}/${hash}`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".file-row", { timeout: 25000 }).catch(() => {});
  const rows = page.locator(".file-row[data-path$='.md']");
  await rows.first().waitFor({ state: "visible", timeout: 25000 }).catch(() => {});
  if (!(await rows.count())) return null;
  await rows.first().click();
  const link = page.locator('#modal-body a:has-text("新标签页查看")').first();
  await link.waitFor({ state: "visible", timeout: 10000 }).catch(() => {});
  return (await link.count()) ? await link.getAttribute("href") : null;
}

async function verifyView(page, href, label) {
  const out = await page.evaluate(async (u) => {
    const r = await fetch(u, { cache: "no-store", headers: AUTH });
    const text = await r.text();
    return {
      status: r.status,
      ct: r.headers.get("content-type") || "",
      len: text.length,
      isViewer: /<!doctype html>/i.test(text) && text.includes("</html>") && /<pre class="doc">/.test(text),
    };
  }, href);
  check(out.status === 200, `${label}：新标签页 URL 返回 200`, `${out.status} ${href}`);
  check(out.ct.includes("text/html"), `${label}：返回 HTML 查看页`, out.ct);
  check(out.len > 0 && out.isViewer, `${label}：内容非空`, `${out.len} chars`);
  // 独立标签页实际渲染
  const tab = await page.context().newPage();
  const resp = await tab.goto(`${BASE}${href}`, { waitUntil: "domcontentloaded" }).catch(() => null);
  const bodyText = (await tab.locator("body").innerText().catch(() => "")).trim();
  check(resp && resp.status() === 200, `${label}：独立标签页打开 200`, resp ? String(resp.status()) : "no response");
  check(bodyText.length > 0, `${label}：独立标签页有可见内容`, `${bodyText.length} chars`);
  await tab.close();
}

async function main() {
  try {
    const h = await api("/api/projects");
    if (h.status !== 200) throw new Error(`/api/projects HTTP ${h.status}`);
    const st = await api("/api/state?project=material");
    if (st.status !== 200 || !Array.isArray(st.body?.reports)) throw new Error("/api/state 未返回 reports");
  } catch (e) {
    console.log(`[FAIL] 平台实例不可用（${BASE}）：${e.message}`);
    process.exit(2);
  }

  const chromePath = process.env.AGWS_CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const browser = await chromium.launch({ headless: true, executablePath: chromePath });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await prepareContext(context);
  const page = await context.newPage();
  try {
    const deliveryHref = await openModalAndGetHref(page, "#/p/material/deliveries");
    check(Boolean(deliveryHref), "交付记录：弹窗出现「新标签页查看」链接", deliveryHref || "未找到");
    if (deliveryHref) await verifyView(page, deliveryHref, "交付记录");

    const docsHref = await openModalAndGetHref(page, "#/p/material/docs");
    check(Boolean(docsHref), "文档配置：弹窗出现「新标签页查看」链接", docsHref || "未找到");
    if (docsHref) await verifyView(page, docsHref, "文档配置");

    // 目录穿越必须被拒绝
    const bad = await page.evaluate(async () => {
      const r = await fetch("/view?path=" + encodeURIComponent("../../etc/passwd") + "&project=material", { cache: "no-store", headers: AUTH });
      return { status: r.status, text: await r.text() };
    });
    check(bad.status === 404, "目录穿越路径被拒绝", String(bad.status));
    check(!/root:/i.test(bad.text), "拒绝响应不含目标文件内容", `${bad.text.length} chars`);
  } finally {
    await browser.close();
  }

  const failed = results.filter((ok) => !ok).length;
  console.log(`\n${results.length - failed}/${results.length} 通过`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
