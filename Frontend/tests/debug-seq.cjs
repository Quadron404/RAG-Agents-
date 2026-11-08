const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.goto("http://127.0.0.1:5173/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await new Promise((r) => setTimeout(r, 1500));
  await page.locator('button[aria-label="Settings"], button[title="Settings"]').first().click({ timeout: 5000 }).catch(() => {});
  await new Promise((r) => setTimeout(r, 700));
  await page.locator('button[aria-label="Chat"], button[title="Chat"]').first().click({ timeout: 5000 }).catch(() => {});
  await new Promise((r) => setTimeout(r, 700));
  await page.locator('button[aria-label="Tasks"], button[title="Tasks"]').first().click({ timeout: 5000 }).catch(() => {});
  await new Promise((r) => setTimeout(r, 700));
  const pbs = page.locator('button:has-text("Pause"), button[title*="pause"], button[aria-label*="pause"]');
  console.log("pause btns:", await pbs.count());
  if (await pbs.count() > 0) { await pbs.first().click({ timeout: 5000 }).catch(() => {}); await new Promise((r) => setTimeout(r, 500)); }
  const rbs = page.locator('button:has-text("Resume"), button[title*="resume"], button[aria-label*="resume"]');
  if (await rbs.count() > 0) { await rbs.first().click({ timeout: 5000 }).catch(() => {}); await new Promise((r) => setTimeout(r, 500)); }
  await page.locator('button[aria-label="Computer"], button[title="Computer"]').first().click({ timeout: 5000 }).catch(() => {});
  await new Promise((r) => setTimeout(r, 700));
  const sb = page.locator('button:has-text("Start"), button[title*="Start computer"], button[aria-label*="Start computer"]').first();
  console.log("start btn count:", await sb.count());
  if (await sb.count() > 0 && !(await sb.isDisabled())) { await sb.click({ timeout: 5000 }).catch(() => {}); await new Promise((r) => setTimeout(r, 600)); }
  await page.locator('button[aria-label="Chat"], button[title="Chat"]').first().click({ timeout: 5000 }).catch(() => {});
  await new Promise((r) => setTimeout(r, 900));
  await page.waitForSelector(".composer-shell", { timeout: 10000 }).catch(() => {});
  const labels = await page.evaluate(() => Array.from(document.querySelectorAll("button")).map((b) => b.getAttribute("aria-label")).filter(Boolean));
  console.log("FINAL labels:", JSON.stringify(labels));
  console.log("has voice call:", labels.some((l) => l && l.includes("voice call")));
  await browser.close();
})().catch((e) => { console.error("FATAL", e); process.exit(2); });
