/**
 * QA integration runner for the deployed Cloudflare Browser QA service.
 *
 * Reads QA_SECRET strictly from the environment (never hardcoded, printed,
 * exposed, or committed). Submits an authenticated POST /qa/run over HTTPS
 * to the deployed QA Worker, parses the JSON report, verifies screenshot
 * evidence, and exits non-zero on any failure.
 *
 * Intended to run after every code change and successful deployment.
 */

const QA_ENDPOINT = "https://rag-agents-qa.pratyakshrawat768.workers.dev/qa/run";
const QA_HEALTH = "https://rag-agents-qa.pratyakshrawat768.workers.dev/qa/health";

function fail(message) {
  console.error(`QA FAIL: ${message}`);
  process.exitCode = 1;
}

function isValidPngBase64(b64) {
  if (!b64 || typeof b64 !== "string") return false;
  try {
    const buf = Buffer.from(b64, "base64");
    return buf.length > 8 &&
      buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47 &&
      buf[4] === 0x0d && buf[5] === 0x0a && buf[6] === 0x1a && buf[7] === 0x0a;
  } catch {
    return false;
  }
}

async function main() {
  const qaSecret = process.env.QA_SECRET;
  if (!qaSecret) {
    fail("QA_SECRET is not set in the environment. Set it via `wrangler secret put QA_SECRET` or export QA_SECRET before running.");
    return;
  }

  // 1. Health check (no auth required)
  console.log(`GET ${QA_HEALTH}`);
  let healthRes;
  try {
    healthRes = await fetch(QA_HEALTH);
  } catch (err) {
    fail(`Health check request failed: ${err.message}`);
    return;
  }
  if (!healthRes.ok) {
    fail(`Health check returned HTTP ${healthRes.status}`);
    return;
  }
  const health = await healthRes.json();
  console.log(`Health: ok=${health.ok} service=${health.service} timestamp=${health.timestamp}`);

  // 2. Run the smoke test (authenticated)
  console.log(`POST ${QA_ENDPOINT}`);
  let runRes;
  try {
    runRes = await fetch(QA_ENDPOINT, {
      method: "POST",
      headers: {
        authorization: `Bearer ${qaSecret}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({ url: "https://rag-agents.pratyakshrawat768.workers.dev/" }),
    });
  } catch (err) {
    fail(`QA run request failed: ${err.message}`);
    return;
  }

  let report;
  try {
    report = await runRes.json();
  } catch (err) {
    fail(`QA run returned non-JSON (HTTP ${runRes.status}): ${err.message}`);
    return;
  }

  // Never print or log the secret. Report only structural results.
  console.log(`QA run HTTP ${runRes.status}`);
  console.log(`  ok: ${report.ok}`);
  console.log(`  passed: ${report.passed}`);
  console.log(`  target: ${report.target}`);
  console.log(`  timestamp: ${report.timestamp}`);
  console.log(`  error: ${report.error ?? null}`);
  console.log(`  pageTitle: ${report.pageTitle ?? null}`);
  console.log(`  finalUrl: ${report.finalUrl ?? null}`);
  console.log(`  loadStatus: ${report.loadStatus ?? null}`);
  console.log(`  consoleErrors: ${report.consoleErrors ? report.consoleErrors.length : 0}`);
  console.log(`  failedNetworkRequests: ${report.failedNetworkRequests ? report.failedNetworkRequests.length : 0}`);
  console.log(`  visibleUiChecks keys: ${report.visibleUiChecks ? Object.keys(report.visibleUiChecks).join(", ") : "none"}`);
  console.log(`  screenshotBase64 length: ${report.screenshotBase64 ? report.screenshotBase64.length : 0}`);

  if (report.consoleErrors && report.consoleErrors.length > 0) {
    console.log("  Console errors:");
    for (const e of report.consoleErrors) {
      console.log(`    - ${e.text} ${e.url ? `(${e.url})` : ""}`);
    }
  }
  if (report.failedNetworkRequests && report.failedNetworkRequests.length > 0) {
    console.log("  Failed network requests:");
    for (const r of report.failedNetworkRequests) {
      console.log(`    - ${r.method ?? "?"} ${r.url} ${r.status ?? r.failure ?? ""}`);
    }
  }

  // 3. Verify screenshot evidence
  const screenshotOk = isValidPngBase64(report.screenshotBase64);
  console.log(`  screenshotValidPng: ${screenshotOk}`);

  // 4. Determine overall pass/fail
  const allOk =
    report.ok === true &&
    report.passed === true &&
    report.error === null &&
    report.loadStatus === "ok" &&
    (report.consoleErrors || []).length === 0 &&
    (report.failedNetworkRequests || []).length === 0 &&
    screenshotOk === true;

  if (!allOk) {
    fail("QA smoke test did not pass all checks.");
    return;
  }

  console.log("QA PASS: read-only smoke test passed against the deployed RAG Agents production target.");
  console.log("  Screenshot evidence captured (valid PNG, base64-encoded in report).");
}

main();
