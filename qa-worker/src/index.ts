/**
 * rag-agents-browser-qa Worker
 *
 * Dedicated browser QA Worker that controls a remote Chromium instance via
 * Cloudflare Browser Run.  It authenticates against the main Worker's QA auth
 * bypass (POST /auth/qa-login) and then runs read-only end-to-end chat tests
 * against the RAG Agents deployment.
 *
 * Endpoints:
 *   GET  /qa/health        - service health (no auth)
 *   POST /qa/run           - read-only page-load smoke test (QA_SECRET bearer auth)
 *   POST /qa/chat-test     - end-to-end RAG Agents chat test (QA_SECRET bearer auth)
 *
 * Security:
 *   - QA_SECRET is a Worker secret, never logged or returned.
 *   - Constant-time token comparison.
 *   - Test targets restricted to an explicit allowlist; arbitrary URLs rejected.
 *   - QA auth bypass is used for authentication (no Google OAuth).
 *   - No destructive actions, no emails, no purchases, no external actions.
 *   - Browser closed in `finally` on every code path.
 */

import { launch, type BrowserWorker } from "@cloudflare/playwright";
import type { Env } from "./env";

// --- Authorised test targets -------------------------------------------------
// Only these origins may be tested. Arbitrary URLs are rejected.
const APPROVED_TARGETS = new Set([
  "https://rag-agents.pratyakshrawat768.workers.dev",
  "https://rag-agents-qa.pratyakshrawat768.workers.dev",
]);

const DEFAULT_TARGET = "https://rag-agents-qa.pratyakshrawat768.workers.dev/";

// --- Timeouts (ms) -----------------------------------------------------------
const NAVIGATE_TIMEOUT_MS = 30000;
const BROWSER_OPERATION_TIMEOUT_MS = 180000;
const AUTH_TIMEOUT_MS = 90000;
const RESPONSE_TIMEOUT_MS = 120000;

// --- JSON helpers ------------------------------------------------------------
function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", ...headers },
  });
}

function toBase64(bytes: Uint8Array | null | undefined): string | null {
  if (!bytes) return null;
  let binary = "";
  for (let i = 0; i < bytes.length; i++) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

// --- Constant-time token comparison ------------------------------------------
function constantTimeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) {
    const n = Math.max(a.length, b.length);
    let result = 0;
    for (let i = 0; i < n; i++) {
      result |= (a.charCodeAt(i) ?? 0) ^ (b.charCodeAt(i) ?? 0);
    }
    return result === 0 && a.length === b.length;
  }
  let result = 0;
  for (let i = 0; i < a.length; i++) {
    result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return result === 0;
}

function extractBearerToken(request: Request): string | null {
  const auth = request.headers.get("authorization") ?? "";
  const prefix = "Bearer ";
  if (!auth.startsWith(prefix)) return null;
  return auth.slice(prefix.length);
}

function isApprovedTarget(urlString: string): boolean {
  try {
    const url = new URL(urlString);
    if (url.protocol !== "https:") return false;
    return APPROVED_TARGETS.has(url.origin);
  } catch {
    return false;
  }
}

// --- Helpers -----------------------------------------------------------------
async function waitForSelector(page: any, selector: string, timeoutMs = 15000): Promise<any> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const el = await page.$(selector);
      if (el) return el;
    } catch {
      /* retry */
    }
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(`selector not found within ${timeoutMs}ms: ${selector}`);
}

async function clickWhenVisible(page: any, selector: string, timeoutMs = 15000): Promise<void> {
  const el = await waitForSelector(page, selector, timeoutMs);
  await el.click();
}

async function captureScreenshot(page: any, label: string): Promise<string | null> {
  try {
    const img = await page.screenshot({ fullPage: true });
    return toBase64(img);
  } catch (err: any) {
    return null;
  }
}

// --- Screenshot report (page-load smoke test) --------------------------------
interface ScreenshotReport {
  ok: boolean;
  passed: boolean;
  target: string;
  timestamp: string;
  error: string | null;
  pageTitle: string | null;
  finalUrl: string | null;
  loadStatus: string | null;
  visibleUiChecks: Record<string, unknown>;
  consoleErrors: Array<{ text: string; url?: string }>;
  failedNetworkRequests: Array<{ url: string; method?: string; status?: number; failure?: string }>;
  screenshotBase64: string | null;
  screenshotContentType: string | null;
}

async function runSmokeTest(browserBinding: BrowserWorker, target: string): Promise<ScreenshotReport> {
  const report: ScreenshotReport = {
    ok: false,
    passed: false,
    target,
    timestamp: new Date().toISOString(),
    error: null,
    pageTitle: null,
    finalUrl: null,
    loadStatus: null,
    visibleUiChecks: {},
    consoleErrors: [],
    failedNetworkRequests: [],
    screenshotBase64: null,
    screenshotContentType: null,
  };

  let browser: any = null;
  const abortController = new AbortController();
  const timeoutId = setTimeout(() => abortController.abort(), BROWSER_OPERATION_TIMEOUT_MS);

  try {
    browser = await launch(browserBinding);

    const context = browser.contexts()[0] ?? (await browser.newContext());
    let page = context.pages()[0] ?? (await context.newPage());

    page.on("console", (msg: any) => {
      if (msg.type() === "error") {
        report.consoleErrors.push({ text: msg.text(), url: msg.location?.url });
      }
    });
    page.on("requestfailed", (req: any) => {
      report.failedNetworkRequests.push({
        url: req.url(),
        method: req.method(),
        failure: req.failure?.errorText,
      });
    });
    page.on("response", (res: any) => {
      if (!res.ok()) {
        report.failedNetworkRequests.push({
          url: res.url(),
          method: res.request().method(),
          status: res.status(),
        });
      }
    });

    const response = await page.goto(target, {
      waitUntil: "domcontentloaded",
      timeout: NAVIGATE_TIMEOUT_MS,
      signal: abortController.signal,
    });

    report.finalUrl = page.url();
    report.pageTitle = await page.title();
    report.loadStatus = response ? (response.ok() ? "ok" : `http-${response.status()}`) : "unknown";

    report.visibleUiChecks = await page.evaluate(() => {
      const checks: Record<string, unknown> = {};
      const selectors = [
        "h1, h2, h3",
        "main",
        "[role=main]",
        "input, textarea, select",
        "button, [role=button], a",
      ];
      for (const sel of selectors) {
        const els = document.querySelectorAll(sel);
        const visible: Array<Record<string, unknown>> = [];
        for (let i = 0; i < els.length; i++) {
          const el = els[i];
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          if (rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none") {
            visible.push({
              tag: el.tagName,
              text: (el.textContent ?? "").trim().slice(0, 120),
              placeholder: el.getAttribute("placeholder") ?? null,
              href: el.getAttribute("href") ?? null,
            });
          }
        }
        checks[sel] = { count: els.length, visible: visible.length, sample: visible.slice(0, 5) };
      }
      return checks;
    });

    const screenshot = await page.screenshot({ fullPage: true });
    report.screenshotBase64 = toBase64(screenshot);
    report.screenshotContentType = "image/png";

    report.ok = true;
    report.passed =
      report.loadStatus === "ok" &&
      report.consoleErrors.length === 0 &&
      report.failedNetworkRequests.length === 0;
  } catch (error: any) {
    report.error = error?.message ?? String(error);
    report.ok = false;
    report.passed = false;
  } finally {
    clearTimeout(timeoutId);
    if (browser) {
      try {
        await browser.close();
      } catch {
        // Best-effort cleanup; never mask the original result.
      }
    }
  }

  return report;
}

// --- Chat test report --------------------------------------------------------
interface ChatTestReport {
  ok: boolean;
  passed: boolean;
  target: string;
  timestamp: string;
  error: string | null;
  stage: string;
  pageTitle: string | null;
  finalUrl: string | null;
  loginResult: { ok: boolean; authenticated: boolean; error: string | null };
  submissionResult: { ok: boolean; sent: boolean; error: string | null };
  aiResponseText: string | null;
  responseValidation: { ok: boolean; checklistItemCount: number; itemsStartWithBracket: boolean; error: string | null };
  responseTimeMs: number | null;
  persistenceResult: { ok: boolean; persisted: boolean; error: string | null };
  consoleErrors: Array<{ text: string; url?: string }>;
  failedNetworkRequests: Array<{ url: string; method?: string; status?: number; failure?: string }>;
  screenshots: { input?: string | null; response?: string | null; persistence?: string | null };
}

// --- End-to-end chat test ----------------------------------------------------
async function runChatTest(browserBinding: BrowserWorker, target: string, env: Env): Promise<ChatTestReport> {
  const report: ChatTestReport = {
    ok: false,
    passed: false,
    target,
    timestamp: new Date().toISOString(),
    error: null,
    stage: "start",
    pageTitle: null,
    finalUrl: null,
    loginResult: { ok: false, authenticated: false, error: null },
    submissionResult: { ok: false, sent: false, error: null },
    aiResponseText: null,
    responseValidation: { ok: false, checklistItemCount: 0, itemsStartWithBracket: false, error: null },
    responseTimeMs: null,
    persistenceResult: { ok: false, persisted: false, error: null },
    consoleErrors: [],
    failedNetworkRequests: [],
    screenshots: {},
  };

  const qaAuthTarget = env.QA_AUTH_TARGET;
  const qaSecret = env.QA_SECRET;

  if (!qaAuthTarget) {
    report.error = "QA_AUTH_TARGET is not configured on the QA Worker.";
    report.stage = "configuration";
    return report;
  }
  if (!qaSecret) {
    report.error = "QA_SECRET is not configured on the QA Worker.";
    report.stage = "configuration";
    return report;
  }

  let browser: any = null;
  const abortController = new AbortController();
  const timeoutId = setTimeout(() => abortController.abort(), BROWSER_OPERATION_TIMEOUT_MS);

  try {
    browser = await launch(browserBinding);
    report.stage = "browser_launched";

    const context = browser.contexts()[0] ?? (await browser.newContext());
    let page = context.pages()[0] ?? (await context.newPage());

    // Collect console errors (read-only).
    page.on("console", (msg: any) => {
      if (msg.type() === "error") {
        report.consoleErrors.push({ text: msg.text(), url: msg.location?.url });
      }
    });

    // Collect failed network requests / HTTP error responses (read-only).
    page.on("requestfailed", (req: any) => {
      report.failedNetworkRequests.push({
        url: req.url(),
        method: req.method(),
        failure: req.failure?.errorText,
      });
    });
    page.on("response", (res: any) => {
      if (!res.ok()) {
        report.failedNetworkRequests.push({
          url: res.url(),
          method: res.request().method(),
          status: res.status(),
        });
      }
    });

    // --- Step 1: Authenticate via QA auth bypass --------------------------
    report.stage = "authenticating";

    // Navigate the browser to the QA login-quick endpoint on the main Worker
    // QA deployment.  It sets the session cookie (domain-shared so it is sent
    // to the app origin) and redirects to the target app.  This avoids CORS
    // and subrequest restrictions that affect fetch() from within the Worker.
    let loginOk = false;
    try {
      const loginUrl = new URL(`${qaAuthTarget}/auth/qa-login-quick`);
      loginUrl.searchParams.set("next", target);
      const loginRes = await page.goto(loginUrl.toString(), {
        waitUntil: "domcontentloaded",
        timeout: NAVIGATE_TIMEOUT_MS,
        signal: abortController.signal,
      });
      if (loginRes && loginRes.ok()) {
        loginOk = true;
      } else {
        report.loginResult.error = `QA login-quick returned HTTP ${loginRes?.status ?? "unknown"}`;
      }
    } catch (err: any) {
      report.loginResult.error = `QA login-quick navigation failed: ${err?.message ?? String(err)}`;
    }

    if (!loginOk) {
      report.stage = "auth_failed";
      throw new Error(report.loginResult.error ?? "QA login failed");
    }

    report.loginResult.ok = true;
    report.loginResult.authenticated = true;
    report.stage = "authenticated";

    report.finalUrl = page.url();
    report.pageTitle = await page.title();

    // Verify we're in the chat view (not the sign-in gate).
    try {
      await waitForSelector(page, ".thread, .composer__ta, .chatlist", AUTH_TIMEOUT_MS);
    } catch (err: any) {
      report.loginResult.error = `App did not load after QA auth: ${err?.message ?? String(err)}`;
      report.stage = "app_not_loaded";
      throw new Error(report.loginResult.error);
    }

    // --- Step 3: Open a new conversation ----------------------------------
    report.stage = "new_conversation";
    let hasComposer = false;
    try {
      hasComposer = Boolean(await page.$(".composer__ta"));
    } catch {
      /* proceed */
    }

    if (!hasComposer) {
      try {
        await clickWhenVisible(page, ".chatlist__head button[title='New conversation'], .chatlist__head button", 10000);
      } catch {
        try {
          await clickWhenVisible(page, "button[aria-label='New conversation']", 5000);
        } catch {
          /* may already be on a conversation */
        }
      }
    }

    try {
      await waitForSelector(page, ".composer__ta, textarea[placeholder='Message Chief…']", 15000);
    } catch (err: any) {
      report.submissionResult.error = `Composer not found after new conversation: ${err?.message ?? String(err)}`;
      report.stage = "composer_not_found";
      throw new Error(report.submissionResult.error);
    }

    report.submissionResult.ok = true;
    report.stage = "composer_ready";

    // --- Step 4: Enter the test message -----------------------------------
    const testMessage = "Create a checklist containing exactly three items for preparing a school bag. Each item must start with [ ]. Keep it concise.";
    report.stage = "entering_message";

    try {
      const input = await waitForSelector(page, ".composer__ta", 10000);
      await input.fill("");
      await input.type(testMessage, { delay: 10 });
    } catch (err: any) {
      report.submissionResult.error = `Failed to type message: ${err?.message ?? String(err)}`;
      report.stage = "type_failed";
      throw new Error(report.submissionResult.error);
    }

    report.screenshots.input = await captureScreenshot(page, "input");

    // --- Step 5: Submit the message ---------------------------------------
    report.stage = "submitting";

    try {
      const sendButton = await waitForSelector(page, "button.composer__send:not(.composer__stop)", 10000);
      await sendButton.click();
    } catch (err: any) {
      try {
        await page.keyboard.press("Enter");
      } catch (e2: any) {
        report.submissionResult.error = `Failed to submit message: ${err?.message ?? String(err)}`;
        report.stage = "submit_failed";
        throw new Error(report.submissionResult.error);
      }
    }

    report.submissionResult.sent = true;
    report.submissionResult.ok = true;
    report.stage = "submitted";

    // --- Step 6: Wait for Chief's response --------------------------------
    report.stage = "waiting_for_response";

    let aiResponseText = null;
    try {
      await waitForSelector(page, ".msg--agent, .step-card, .runbanner", RESPONSE_TIMEOUT_MS);

      await new Promise((resolve) => {
        const check = async () => {
          try {
            const busy = await page.evaluate(() => {
              const run = document.querySelector(".runbanner");
              const stepCard = document.querySelector(".step-card--busy");
              return !run && !stepCard;
            });
            if (busy) {
              resolve(null);
            } else {
              setTimeout(check, 1000);
            }
          } catch {
            setTimeout(check, 1000);
          }
        };
        check();
      });

      await new Promise((r) => setTimeout(r, 2000));

      aiResponseText = await page.evaluate(() => {
        const agentMsgs = Array.from(document.querySelectorAll(".msg--agent .bubble, .step-card__report"));
        if (agentMsgs.length === 0) return null;
        const last = agentMsgs[agentMsgs.length - 1];
        return last.textContent ? last.textContent.trim() : null;
      });
    } catch (err: any) {
      report.responseValidation.error = `No AI response received: ${err?.message ?? String(err)}`;
      report.stage = "no_response";
      throw new Error(report.responseValidation.error);
    }

    report.aiResponseText = aiResponseText;
    report.stage = "response_received";
    report.screenshots.response = await captureScreenshot(page, "response");

    // --- Step 7: Validate the response ------------------------------------
    report.stage = "validating_response";
    let checklistItemCount = 0;
    let itemsStartWithBracket = false;
    let validationError: string | null = null;

    if (aiResponseText) {
      const lines = aiResponseText.split("\n").map((l: string) => l.trim()).filter(Boolean);
      const bracketItems = lines.filter((l: string) => l.startsWith("[ ]"));
      checklistItemCount = bracketItems.length;
      itemsStartWithBracket = checklistItemCount === 3 && lines.length >= 3;

      if (checklistItemCount !== 3) {
        validationError = `Expected exactly 3 checklist items starting with [ ], found ${checklistItemCount}`;
      }
    } else {
      validationError = "No AI response text captured";
    }

    report.responseValidation = {
      ok: validationError === null,
      checklistItemCount,
      itemsStartWithBracket,
      error: validationError,
    };
    report.stage = "validated";

    // --- Step 8: Refresh and verify persistence ---------------------------
    report.stage = "persistence_check";

    try {
      await page.reload({ waitUntil: "domcontentloaded", timeout: NAVIGATE_TIMEOUT_MS });

      await waitForSelector(page, ".composer__ta, .thread__scroll", 20000);

      const persistenceCheck = await page.evaluate((expectedText: string) => {
        const agentMsgs = Array.from(document.querySelectorAll(".msg--agent .bubble, .step-card__report"));
        const combined = agentMsgs.map((m) => m.textContent ? m.textContent.trim() : "").join("\n");
        return {
          hasAgentMessages: agentMsgs.length > 0,
          containsResponse: combined.includes(expectedText),
          threadCount: document.querySelectorAll(".convrow").length,
        };
      }, testMessage);

      report.persistenceResult = {
        ok: persistenceCheck.hasAgentMessages && persistenceCheck.containsResponse,
        persisted: persistenceCheck.hasAgentMessages && persistenceCheck.containsResponse,
        error: persistenceCheck.hasAgentMessages && persistenceCheck.containsResponse
          ? null
          : "Conversation or response did not persist after refresh",
      };
    } catch (err: any) {
      report.persistenceResult.error = `Persistence check failed: ${err?.message ?? String(err)}`;
      report.stage = "persistence_failed";
    }

    report.screenshots.persistence = await captureScreenshot(page, "persistence");
    report.stage = "persistence_done";

    // --- Overall pass/fail ------------------------------------------------
    const allOk =
      report.loginResult.ok &&
      report.loginResult.authenticated &&
      report.submissionResult.ok &&
      report.submissionResult.sent &&
      report.responseValidation.ok &&
      report.persistenceResult.ok &&
      report.aiResponseText !== null;

    report.ok = allOk;
    report.passed = allOk;
    report.error = allOk ? null : "One or more chat-test stages did not pass";

  } catch (error: any) {
    report.error = error?.message ?? String(error);
    report.ok = false;
    report.passed = false;
  } finally {
    clearTimeout(timeoutId);
    if (browser) {
      try {
        await browser.close();
      } catch {
        // Best-effort cleanup; never mask the original result.
      }
    }
  }

  return report;
}

// --- Worker ------------------------------------------------------------------
export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/qa/health" && request.method === "GET") {
      return json({
        ok: true,
        service: "rag-agents-browser-qa",
        timestamp: new Date().toISOString(),
      });
    }

    if (path === "/qa/run" && request.method === "POST") {
      const supplied = extractBearerToken(request);
      if (!supplied || !env.QA_SECRET || !constantTimeEqual(supplied, env.QA_SECRET)) {
        return json({ ok: false, error: "authentication required" }, 401);
      }

      let body: { url?: string } = {};
      try {
        body = (await request.json()) ?? {};
      } catch {
        /* ignore invalid body */
      }
      const target = typeof body.url === "string" && body.url ? body.url : DEFAULT_TARGET;

      if (!isApprovedTarget(target)) {
        return json(
          { ok: false, error: "target not authorised", target },
          403,
          { "content-type": "application/json; charset=utf-8" },
        );
      }

      const report = await runSmokeTest(env.BROWSER, target);
      const status = report.ok ? 200 : 502;
      return json(report, status);
    }

    if (path === "/qa/chat-test" && request.method === "POST") {
      const supplied = extractBearerToken(request);
      if (!supplied || !env.QA_SECRET || !constantTimeEqual(supplied, env.QA_SECRET)) {
        return json({ ok: false, error: "authentication required" }, 401);
      }

      let body: { url?: string } = {};
      try {
        body = (await request.json()) ?? {};
      } catch {
        /* ignore invalid body */
      }
      const target = typeof body.url === "string" && body.url ? body.url : DEFAULT_TARGET;

      if (!isApprovedTarget(target)) {
        return json(
          { ok: false, error: "target not authorised", target },
          403,
          { "content-type": "application/json; charset=utf-8" },
        );
      }

      const report = await runChatTest(env.BROWSER, target, env);
      const status = report.passed ? 200 : 502;
      return json(report, status);
    }

    return json({ ok: false, error: "not found" }, 404);
  },
};
