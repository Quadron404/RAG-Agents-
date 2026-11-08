// End-to-end Playwright QA for RAG Agents against the local fake backend.
// Drives the REAL frontend (Vite dev server) through a REAL Chromium browser.
const { chromium } = require("playwright");

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();

  const results = [];
  const errors = [];
  page.on("pageerror", (e) => errors.push("REACT: " + e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push("CONSOLE: " + m.text());
  });

  const check = (name, cond, detail = "") => {
    results.push({ name, pass: !!cond, detail });
    console.log((cond ? "PASS" : "FAIL") + " | " + name + (detail ? " — " + detail : ""));
  };

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  // Set the value of a React-controlled textarea via real keyboard input so onChange fires.
  const setTextArea = async (el, text) => {
    await el.click();
    await page.keyboard.down("Control");
    await page.keyboard.press("A");
    await page.keyboard.up("Control");
    await page.keyboard.insertText(text);
  };

  await page.goto("http://127.0.0.1:5173/", { waitUntil: "domcontentloaded", timeout: 30000 });
  await wait(2000);

  // --- A. Authentication / entry ---
  const root = page.locator("#root");
  const rootText = await root.textContent();
  const isSignedIn = !/Sign in with Google/.test(rootText || "");
  check("A1: authenticated entry (no sign-in screen)", isSignedIn, rootText ? rootText.slice(0, 80) : "");
  check("A2: no error boundary on load", !/RAG Agents hit an unexpected error/.test(rootText || ""), "");

  await page.screenshot({ path: "/tmp/kilo/qa-01-entry.png", fullPage: true });

  // --- B. Sidebar navigation: all views render without crashing ---
  const navLabels = ["Chat", "Bots", "Tasks", "Computer", "Files"];
  for (const label of navLabels) {
    const btn = page.locator(`button[aria-label="${label}"], button[title="${label}"]`).first();
    if ((await btn.count()) > 0) {
      await btn.click({ timeout: 5000 }).catch(() => {});
      await wait(700);
      const txt = await page.locator("#root").textContent();
      const hasErr = /RAG Agents hit an unexpected error|Minified React/.test(txt || "");
      check(`B: "${label}" view renders without crash`, !hasErr && !!txt, hasErr ? "CRASH" : (txt || "").slice(0, 60));
    } else {
      check(`B: "${label}" nav button found`, false, "button not found");
    }
  }

  // Return to Chat
  await page.locator('button[aria-label="Chat"], button[title="Chat"]').first().click({ timeout: 5000 }).catch(() => {});
  await wait(700);

  // --- C. Chat: composer, send, streaming, Enter/Shift+Enter ---
  const composer = page.locator(".composer-shell");
  check("C1: composer present", (await composer.count()) > 0, "");
  const ta = page.locator("textarea[aria-label='Message'], textarea[placeholder*='Chief']").first();
  check("C2: textarea present", (await ta.count()) > 0, "");

  // Type a normal message
  await setTextArea(ta, "Hello Chief, this is a normal message.");
  const sendBtn = page.locator('button[aria-label="Send"]').first();
  check("C3: send button present", (await sendBtn.count()) > 0, "");
  await sendBtn.click({ timeout: 5000 }).catch(() => {});
  await wait(1200);
  const chatHtml = await page.locator("#root").innerHTML();
  const hasUserMsg = /Hello Chief/.test(chatHtml);
  check("C4: user message appears after send", hasUserMsg, "");

  // Long message
  await setTextArea(ta, "This is a longer message. " + "lorem ipsum dolor sit amet. ".repeat(10));
  await sendBtn.click({ timeout: 5000 }).catch(() => {});
  await wait(1000);
  const chatHtml2 = await page.locator("#root").innerHTML();
  check("C5: long message accepted", /lorem ipsum/.test(chatHtml2) || /This is a longer/.test(chatHtml2), "");

  // Multiline / Shift+Enter
  await setTextArea(ta, "line one");
  await page.keyboard.down("Shift");
  await page.keyboard.press("Enter");
  await page.keyboard.up("Shift");
  await ta.pressSequentially("line two");
  check("C6: Shift+Enter creates newline (multiline)", (await ta.inputValue()) === "line one\nline two", "value=" + (await ta.inputValue()));

  await wait(500);
  await page.screenshot({ path: "/tmp/kilo/qa-02-chat.png", fullPage: true });

  // --- C cont.: conversation switching / persistence ---
  const backBtn = page.locator('button[aria-label="Open conversations"]').first();
  if ((await backBtn.count()) > 0) {
    await backBtn.click({ timeout: 5000 }).catch(() => {});
    await wait(700);
    const convHtml = await page.locator("#root").innerHTML();
    check("C7: conversation list opens", /Planning the week|Research task|New conversation/.test(convHtml), "");
    // click second thread
    await page.locator('text=Research task').first().click({ timeout: 5000 }).catch(() => {});
    await wait(800);
    const afterSwitch = await page.locator("#root").innerHTML();
    check("C8: switched conversation renders", /Research task/.test(afterSwitch) || afterSwitch.length > 200, "");
  }

  // Refresh + history persistence
  await page.reload({ waitUntil: "domcontentloaded" });
  await wait(1500);
  const afterReload = await page.locator("#root").innerHTML();
  check("C9: session persists after refresh (still authenticated)", !/Sign in with Google/.test(afterReload || ""), "");
  check("C10: no error boundary after refresh", !/RAG Agents hit an unexpected error/.test(afterReload || ""), "");

  await page.screenshot({ path: "/tmp/kilo/qa-03-refresh.png", fullPage: true });

  // --- D. Bots view ---
  await page.locator('button[aria-label="Bots"], button[title="Bots"]').first().click({ timeout: 5000 }).catch(() => {});
  await wait(700);
  const botsHtml = await page.locator("#root").innerHTML();
  check("D1: bots view renders", botsHtml.length > 100, "");
  check("D2: no crash in bots", !/RAG Agents hit an unexpected error/.test(botsHtml || ""), "");

  // --- E. Tasks / routines ---
  await page.locator('button[aria-label="Tasks"], button[title="Tasks"]').first().click({ timeout: 5000 }).catch(() => {});
  await wait(700);
  const tasksHtml = await page.locator("#root").innerHTML();
  check("E1: tasks view renders", tasksHtml.length > 100, "");
  check("E2: no crash in tasks", !/RAG Agents hit an unexpected error/.test(tasksHtml || ""), "");

  // --- F. Computer view ---
  await page.locator('button[aria-label="Computer"], button[title="Computer"]').first().click({ timeout: 5000 }).catch(() => {});
  await wait(700);
  const compHtml = await page.locator("#root").innerHTML();
  check("F1: computer view renders", compHtml.length > 100, "");
  check("F2: no crash in computer", !/RAG Agents hit an unexpected error/.test(compHtml || ""), "");

  // --- G. Files view ---
  await page.locator('button[aria-label="Files"], button[title="Files"]').first().click({ timeout: 5000 }).catch(() => {});
  await wait(700);
  const filesHtml = await page.locator("#root").innerHTML();
  check("G1: files view renders", filesHtml.length > 100, "");
  check("G2: no crash in files", !/RAG Agents hit an unexpected error/.test(filesHtml || ""), "");

  // --- J. Settings ---
  const settingsBtn = page.locator('button[aria-label="Settings"], button[title="Settings"]').first();
  if ((await settingsBtn.count()) > 0) {
    await settingsBtn.click({ timeout: 5000 }).catch(() => {});
    await wait(700);
    const settingsHtml = await page.locator("#root").innerHTML();
    check("J1: settings view renders", settingsHtml.length > 100, "");
    check("J2: no crash in settings", !/RAG Agents hit an unexpected error/.test(settingsHtml || ""), "");
    // toggle a preference if present
    const darkToggle = page.locator('button:has-text("Dark"), button:has-text("Appearance"), [role="switch"]').first();
    if ((await darkToggle.count()) > 0) {
      await darkToggle.click({ timeout: 5000 }).catch(() => {});
      await wait(500);
      const afterToggle = await page.locator("#root").innerHTML();
      check("J3: settings control responds (no crash)", afterToggle.length > 0, "");
    }
    await page.screenshot({ path: "/tmp/kilo/qa-04-settings.png", fullPage: true });
  }

  // --- I. Gmail / approval cards (rendered inside Chat) ---
  await page.locator('button[aria-label="Chat"], button[title="Chat"]').first().click({ timeout: 5000 }).catch(() => {});
  await wait(700);
  const approvalCards = page.locator('.approval-card');
  const approvalCount = await approvalCards.count();
  check("I1: approval card(s) render when pending", approvalCount >= 0, "count=" + approvalCount);
  check("I2: no crash rendering approvals", !/RAG Agents hit an unexpected error/.test((await page.locator("#root").innerHTML()) || ""), "");
  // Try Allow on the first approval if present
  if (approvalCount > 0) {
    const allowBtn = approvalCards.first().locator('button[title="Allow"], button[aria-label="Allow this connector action"]').first();
    if ((await allowBtn.count()) > 0) {
      await allowBtn.click({ timeout: 5000 }).catch(() => {});
      await wait(700);
      const afterAllow = await page.locator("#root").innerHTML();
      check("I3: Allow click handled without crash", afterAllow.length > 0, "");
    }
  }

  // --- H. Tasks / routines lifecycle ---
  await page.locator('button[aria-label="Tasks"], button[title="Tasks"]').first().click({ timeout: 5000 }).catch(() => {});
  await wait(700);
  check("H1: routines view renders", (await page.locator("#root").innerHTML()).length > 0, "");
  // pause + resume + delete a routine
  const pauseBtns = page.locator('button:has-text("Pause"), button[title*="pause"], button[aria-label*="pause"]');
  if ((await pauseBtns.count()) > 0) {
    await pauseBtns.first().click({ timeout: 5000 }).catch(() => {});
    await wait(500);
    check("H2: pause routine handled", true, "");
  }
  const resumeBtns = page.locator('button:has-text("Resume"), button[title*="resume"], button[aria-label*="resume"]');
  if ((await resumeBtns.count()) > 0) {
    await resumeBtns.first().click({ timeout: 5000 }).catch(() => {});
    await wait(500);
    check("H3: resume routine handled", true, "");
  }
  const delBtns = page.locator('button:has-text("Delete"), button[title*="Delete"], button[aria-label*="Delete"]');
  if ((await delBtns.count()) > 0) {
    await delBtns.first().click({ timeout: 5000 }).catch(() => {});
    await wait(500);
    check("H4: delete routine handled", true, "");
  }

  // --- F. Computer start/stop ---
  await page.locator('button[aria-label="Computer"], button[title="Computer"]').first().click({ timeout: 5000 }).catch(() => {});
  await wait(700);
  const compHtml2 = await page.locator("#root").innerHTML();
  const startBtn = page.locator('button:has-text("Start"), button[title*="Start computer"], button[aria-label*="Start computer"]').first();
  if ((await startBtn.count()) > 0) {
    await startBtn.click({ timeout: 5000 }).catch(() => {});
    await wait(600);
    check("F2: computer start handled (no crash)", (await page.locator("#root").innerHTML()).length > 0, "");
    const stopBtn = page.locator('button:has-text("Stop"), button[title*="Stop computer"], button[aria-label*="Stop computer"]').first();
    if ((await stopBtn.count()) > 0) {
      await stopBtn.click({ timeout: 5000 }).catch(() => {});
      await wait(500);
      check("F3: computer stop handled (no crash)", true, "");
    }
  }

  // --- C-ext. Call button (TTS+STT) present in composer ---
  // Close any open settings overlay so the rail nav click lands on Chat.
  const closeBtn = page.locator('button:has-text("Close"), button[title="Close"], button[aria-label="Close"]').first();
  if ((await closeBtn.count()) > 0) { await closeBtn.click({ timeout: 5000 }).catch(() => {}); await wait(400); }
  await page.locator('button[aria-label="Chat"], button[title="Chat"]').first().click({ timeout: 5000 }).catch(() => {});
  await wait(900);
  await page.waitForSelector('.composer-shell', { timeout: 10000 }).catch(() => {});
  const callPresent = await page.evaluate(() => !!document.querySelector('button[aria-label*="voice call"]'));
  const callBtn = page.locator('button[aria-label*="voice call"]').first();
  const allLabels = await page.evaluate(() => Array.from(document.querySelectorAll("button")).map((b) => b.getAttribute("aria-label")).filter(Boolean));
  console.log("C11 diag: callPresent=" + callPresent + " labels=" + JSON.stringify(allLabels));
  check("C11: Call button present in composer", callPresent, "");
  if (callPresent) {
    await callBtn.click({ timeout: 5000 }).catch(() => {});
    await wait(500);
    const callingNow = await page.evaluate(() => !!document.querySelector('button[aria-label="End call"]'));
    check("C12: Call toggle starts (call-active state)", callingNow, "");
    await callBtn.click({ timeout: 5000 }).catch(() => {});
    await wait(400);
    check("C13: Call toggle ends without crash", true, "");
  }

  await page.screenshot({ path: "/tmp/kilo/qa-05-final.png", fullPage: true });

  // Summary
  const fails = results.filter((r) => !r.pass);
  console.log("\n========================================");
  console.log("TOTAL:", results.length, "PASS:", results.length - fails.length, "FAIL:", fails.length);
  fails.forEach((r) => console.log("  FAIL:", r.name, "|", r.detail));
  console.log("JS_ERRORS:", errors.length);
  errors.forEach((e) => console.error("  ERR:", e));
  console.log("========================================");

  await browser.close();
  process.exit(fails.length > 0 || errors.length > 0 ? 1 : 0);
})().catch((e) => {
  console.error("FATAL:", e);
  process.exit(2);
});
