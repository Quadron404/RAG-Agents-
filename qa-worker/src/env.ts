import type { BrowserWorker } from "@cloudflare/playwright";

/**
 * Bindings and vars for the rag-agents-qa Worker.
 *
 * Secrets (QA_SECRET, QA_TEST_EMAIL, QA_TEST_PASSWORD) are set with
 * `wrangler secret put` and are never committed.
 */
export interface Env {
  /** Browser Run binding configured as "browser": { "binding": "BROWSER" }. */
  BROWSER: BrowserWorker;
  /** Shared secret protecting POST /qa/run and POST /qa/chat-test. */
  QA_SECRET: string;
  /** Dedicated QA test account email (Google account for OAuth sign-in). Never a personal account. */
  QA_TEST_EMAIL?: string;
  /** Dedicated QA test account password. Never logged or returned. */
  QA_TEST_PASSWORD?: string;
  DEFAULT_QA_TARGET?: string;
}
