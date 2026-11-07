import type { BrowserWorker } from "@cloudflare/playwright";

/**
 * Bindings and vars for the rag-agents-browser-qa Worker.
 *
 * Secrets (QA_SECRET) are set with `wrangler secret put` and are never committed.
 */
export interface Env {
  /** Browser Run binding configured as "browser": { "binding": "BROWSER" }. */
  BROWSER: BrowserWorker;
  /** Shared secret protecting POST /qa/run and POST /qa/chat-test. */
  QA_SECRET: string;
  /** Target app origin (read-only allowlist enforced in code). */
  DEFAULT_QA_TARGET?: string;
  /** Main Worker QA deployment used for QA auth bypass (POST /auth/qa-login). */
  QA_AUTH_TARGET?: string;
}
