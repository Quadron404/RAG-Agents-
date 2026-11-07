import type { BrowserWorker } from "@cloudflare/playwright";

/**
 * Bindings and vars for the rag-agents-qa Worker.
 *
 * Secrets (QA_SECRET) are set with `wrangler secret put` and are never committed.
 */
export interface Env {
  /** Browser Run binding configured as "browser": { "binding": "BROWSER" }. */
  BROWSER: BrowserWorker;
  /** Shared secret protecting POST /qa/run. Never logged or returned. */
  QA_SECRET: string;
  DEFAULT_QA_TARGET?: string;
}
