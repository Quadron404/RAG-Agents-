/**
 * rag-agents-qa Worker
 *
 * Dedicated, read-only browser QA Worker for the RAG Agents deployment.
 * Runs entirely inside Cloudflare via the @cloudflare/playwright Browser Run
 * binding. Does not touch the rag-agents Worker, its D1, or its assets.
 *
 * Endpoints:
 *   GET  /qa/health        - service health (no auth)
 *   POST /qa/run           - read-only smoke test (QA_SECRET bearer auth)
 *
 * Security:
 *   - QA_SECRET is a Worker secret, never logged or returned.
 *   - Constant-time token comparison.
 *   - Test targets restricted to an explicit allowlist; arbitrary URLs rejected.
 *   - No destructive actions, no form submissions, no production data writes.
 *   - Browser closed in `finally` on every code path.
 */

import { launch, type BrowserWorker } from "@cloudflare/playwright";
import type { Env } from "./env";

// --- Authorised test targets -------------------------------------------------
// Only these origins may be tested. Arbitrary URLs are rejected.
// Add explicitly approved preview origins here as needed.
const APPROVED_TARGETS = new Set([
  "https://rag-agents.pratyakshrawat768.workers.dev",
]);

const DEFAULT_TARGET = "https://rag-agents.pratyakshrawat768.workers.dev/";

// --- Timeouts (ms) -----------------------------------------------------------
const NAVIGATE_TIMEOUT_MS = 30000;
const BROWSER_OPERATION_TIMEOUT_MS = 110000;

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
    // Different lengths can never be equal; still perform a full-width XOR loop
    // against a buffer of the longer length so the compare path has no early
    // return that depends on secret contents when lengths match.
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

// --- Smoke test --------------------------------------------------------------
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
  let started = false;

  const abortController = new AbortController();
  const timeoutId = setTimeout(() => abortController.abort(), BROWSER_OPERATION_TIMEOUT_MS);

  try {
    browser = await launch(browserBinding);
    started = true;

    const context = browser.contexts()[0] ?? (await browser.newContext());
    let page = context.pages()[0] ?? (await context.newPage());

    // Collect console errors (read-only).
    page.on("console", (msg: any) => {
      if (msg.type() === "error") {
        report.consoleErrors.push({
          text: msg.text(),
          url: msg.location?.url,
        });
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

    const response = await page.goto(target, {
      waitUntil: "domcontentloaded",
      timeout: NAVIGATE_TIMEOUT_MS,
      signal: abortController.signal,
    });

    report.finalUrl = page.url();
    report.pageTitle = await page.title();
    report.loadStatus = response ? (response.ok() ? "ok" : `http-${response.status()}`) : "unknown";

    // Read-only visible UI checks (no clicks, no form submission).
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

    // Capture screenshot (read-only evidence).
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

// --- Worker ------------------------------------------------------------------
export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/qa/health" && request.method === "GET") {
      return json({
        ok: true,
        service: "rag-agents-qa",
        timestamp: new Date().toISOString(),
      });
    }

    if (path === "/qa/run" && request.method === "POST") {
      // Authentication: Bearer QA_SECRET, constant-time comparison.
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

    return json({ ok: false, error: "not found" }, 404);
  },
};
