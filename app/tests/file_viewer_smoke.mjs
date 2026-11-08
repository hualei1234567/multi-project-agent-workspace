#!/usr/bin/env node
/**
 * Agent Workspace 文件查看器冒烟：交付记录 / 文档配置两个入口的新标签页查看。
 *
 * 覆盖：
 *   0) 自建临时项目与报告文件（不依赖真实项目里有报告）
 *   1) 交付详情「打开产物」→ 文件弹窗「新标签页查看」→ /view 返回 200 且 HTML 非空
 *   2) 文档配置预览「新标签页查看」→ 同上
 *   3) 目录穿越路径被 /view 拒绝（404，不泄露内容）
 *   4) /view 返回 HTML 查看页而不是裸 text/plain（可独立打开）
 *
 * 用法：AGWS_URL=... AGWS_DB=<实例库路径> node app/tests/file_viewer_smoke.mjs
 * 退出码：0 全通过；1 有失败；2 环境不可用。
 */

import { createRequire } from "node:module";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
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

const TOKEN = process.env.AGWS_TOKEN || "";
const AUTH = TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {};

async function prepareContext(context) {
  if (!TOKEN) return;
  await context.addInitScript((t) => {
    try { localStorage.setItem("agws-token", t); } catch { /* 隐私模式忽略 */ }
  }, TOKEN);
}

const BASE = (process.env.AGWS_URL || "http://127.0.0.1:8098").replace(/\/$/, "");
const DB_PATH = process.env.AGWS_DB
  || process.env.AGWS_PLATFORM_DB
  || join(HERE, "..", "..", "data", "platform.db");
const results = [];
const check = (cond, label, detail = "") => {
  results.push(Boolean(cond));
  console.log(`[${cond ? "PASS" : "FAIL"}] ${label}${detail ? ` — ${detail}` : ""}`);
};

async function api(path, method = "GET", body = null) {
  const res = await fetch(BASE + path, {
    method,
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...AUTH },
    body: body ? JSON.stringify(body) : undefined,
  });
  return { status: res.status, body: await res.json().catch(() => null) };
}

const CODE = `qa-fv-${Date.now().toString(36).slice(-5)}`;
const REPO_ROOT = `/tmp/projects/${CODE}`;
const REPORT_REL = "ai-team/reports/qa-viewer.md";

function writeReport() {
  mkdirSync(join(REPO_ROOT, "ai-team", "reports"), { recursive: true });
  writeFileSync(join(REPO_ROOT, "ai-team", "TASK_BOARD.md"), "# Board\n\n| Task ID | 标题 |\n|---|---|\n", "utf-8");
  writeFileSync(join(REPO_ROOT, REPORT_REL), "# 冒烟报告\n\n新标签页查看测试。\n", "utf-8");
}

function cleanup() {
  if (!existsSync(DB_PATH)) {
    console.log(`[WARN] 清理跳过：库不存在 ${DB_PATH}`);
    return;
  }
  const sql = [
    "BEGIN;",
    `DELETE FROM token_usage_events WHERE project_id='${CODE}';`,
    `DELETE FROM agent_events WHERE project_id='${CODE}';`,
    `DELETE FROM deliveries WHERE project_id='${CODE}';`,
    `DELETE FROM tasks WHERE project_id='${CODE}';`,
    `DELETE FROM project_agents WHERE project_id='${CODE}';`,
    `DELETE FROM project_members WHERE project_id='${CODE}';`,
    `DELETE FROM audit_logs WHERE project_id='${CODE}';`,
    `DELETE FROM agent_projects WHERE id='${CODE}';`,
    "COMMIT;",
  ].join(" ");
  try {
    execFileSync("sqlite3", [DB_PATH, sql], { stdio: "pipe" });
    console.log(`[INFO] 已清理测试项目：${CODE}`);
  } catch (e) {
    console.log(`[WARN] 测试项目清理失败：${CODE} — ${e.message}`);
  }
  try { rmSync(REPO_ROOT, { recursive: true, force: true }); } catch { /* 忽略 */ }
}

/** 打开文件预览弹窗（交付详情里先点「打开产物」）。 */
async function openModalAndGetHref(page, hash, viaDelivery) {
  await page.goto(`${BASE}/${hash}`, { waitUntil: "domcontentloaded" });
  const rows = page.locator(".file-row[data-path$='.md']");
  await rows.first().waitFor({ state: "visible", timeout: 25000 }).catch(() => {});
  if (!(await rows.count())) return null;
  await rows.first().click();
  if (viaDelivery) {
    const openArtifact = page.locator("#dv-open");
    await openArtifact.waitFor({ state: "visible", timeout: 10000 }).catch(() => {});
    if (!(await openArtifact.count())) return null;
    await openArtifact.click();
  }
  const link = page.locator('#modal-body a:has-text("新标签页查看")').first();
  await link.waitFor({ state: "visible", timeout: 15000 }).catch(() => {});
  return (await link.count()) ? await link.getAttribute("href") : null;
}

async function verifyView(page, href, label) {
  // 浏览器上下文里没有 Node 变量：令牌与地址都作为参数传进去
  const out = await page.evaluate(async ({ u, token }) => {
    const r = await fetch(u, {
      cache: "no-store",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    const text = await r.text();
    return {
      status: r.status,
      ct: r.headers.get("content-type") || "",
      len: text.length,
      isViewer: /<!doctype html>/i.test(text) && text.includes("</html>") && /<pre class="doc">/.test(text),
    };
  }, { u: href, token: TOKEN });
  check(out.status === 200, `${label}：新标签页 URL 返回 200`, `${out.status} ${href}`);
  check(out.ct.includes("text/html"), `${label}：返回 HTML 查看页`, out.ct);
  check(out.len > 0 && out.isViewer, `${label}：内容非空`, `${out.len} chars`);
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
  } catch (e) {
    console.log(`[FAIL] 平台实例不可用（${BASE}）：${e.message}`);
    process.exit(2);
  }

  // 夹具：临时项目 + 报告文件 + 一条指向该文件的交付
  writeReport();
  const created = await api("/api/projects", "POST", {
    code: CODE, name: "文件查看器冒烟项目", repo_root: REPO_ROOT, session_slugs: [],
  });
  check(created.status === 200, "创建临时项目", String(created.status));
  const delivery = await api(`/api/projects/${CODE}/deliveries`, "POST", {
    title: "冒烟报告交付", kind: "document", path: REPORT_REL, summary: "新标签页查看夹具",
  });
  check(delivery.status === 200, "创建指向报告文件的交付", String(delivery.status));

  const chromePath = process.env.AGWS_CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const browser = await chromium.launch({ headless: true, executablePath: chromePath });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await prepareContext(context);
  const page = await context.newPage();
  try {
    const deliveryHref = await openModalAndGetHref(page, `#/p/${CODE}/deliveries`, true);
    check(Boolean(deliveryHref), "交付记录：弹窗出现「新标签页查看」链接", deliveryHref || "未找到");
    if (deliveryHref) await verifyView(page, deliveryHref, "交付记录");

    const docsHref = await openModalAndGetHref(page, `#/p/${CODE}/docs`, false);
    check(Boolean(docsHref), "文档配置：弹窗出现「新标签页查看」链接", docsHref || "未找到");
    if (docsHref) await verifyView(page, docsHref, "文档配置");

    // 目录穿越必须被拒绝（/view 是 loopback 只读接口，不需要令牌）
    const bad = await page.evaluate(async ({ token }) => {
      const r = await fetch("/view?path=" + encodeURIComponent("../../etc/passwd") + `&project=${encodeURIComponent("__x__")}`, {
        cache: "no-store",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      return { status: r.status, text: await r.text() };
    }, { token: TOKEN });
    check(bad.status === 404, "目录穿越路径被拒绝", String(bad.status));
    check(!/root:/i.test(bad.text), "拒绝响应不含目标文件内容", `${bad.text.length} chars`);
  } finally {
    await browser.close();
    cleanup();
  }

  const failed = results.filter((ok) => !ok).length;
  console.log(`\n${results.length - failed}/${results.length} 通过`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  try { cleanup(); } catch { /* 忽略 */ }
  process.exit(1);
});
