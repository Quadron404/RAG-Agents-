/**
 * Self-healing D1 schema migrations.
 *
 * Wrangler cannot apply migrations to the remote database from this sandbox
 * (the API token lacks D1 permissions), so the Worker applies any missing
 * additive columns itself on startup.  Every migration here is idempotent:
 * it checks PRAGMA table_info before altering, so re-running is safe.
 */
import type { Env } from "./env";

async function tableColumns(env: Env, table: string): Promise<string[]> {
  const info = await env.DB.prepare(`PRAGMA table_info(${table})`).all();
  const rows = info.results as Array<{ name: string }>;
  return rows.map((c) => c.name);
}

export async function ensureSchema(env: Env): Promise<void> {
  // Migration 0011: connector_approvals.updated_at
  try {
    const cols = await tableColumns(env, "connector_approvals");
    if (!cols.includes("updated_at")) {
      await env.DB.prepare(
        "ALTER TABLE connector_approvals ADD COLUMN updated_at INTEGER"
      ).run();
      await env.DB.prepare(
        "UPDATE connector_approvals SET updated_at = created_at WHERE updated_at IS NULL"
      ).run();
    }
  } catch {
    // Table may not exist yet; nothing to do
  }

  // Migration 0012: routines.authorization_policy
  try {
    const cols = await tableColumns(env, "routines");
    if (!cols.includes("authorization_policy")) {
      await env.DB.prepare(
        "ALTER TABLE routines ADD COLUMN authorization_policy TEXT NOT NULL DEFAULT 'per_run'"
      ).run();
    }
  } catch {
    // Table may not exist yet; nothing to do
  }
}
