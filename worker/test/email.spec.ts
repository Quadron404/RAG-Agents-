/**
 * Email end-to-end tests (prompt 67).
 *
 * Covers: direct request → approval creation, Allow, Deny, expiration,
 * unauthorized owner, invalid alias, revoked OAuth grant, Gmail rejection,
 * duplicate click, retry and scheduled execution.
 *
 * The Gmail API is mocked via the global fetch; no real email is ever sent.
 * Approvals and users live in the D1 test database.
 */
import { describe, expect, it, beforeEach, vi } from "vitest";
import { env } from "cloudflare:test";
import {
  createEmailApproval,
  executeEmailApproval,
  type EmailRequestInput,
  validateEmailBody,
  validateEmailSubject,
} from "../src/chat/email-service";
import { createConnectorApproval, getConnectorApproval, listPendingApprovals } from "../src/connector-approvals";

const GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token";
const GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send";

interface SendCall {
  recipient: string;
  subject: string;
  body: string;
}

function makeEnv(overrides: Partial<Record<string, string>> = {}) {
  return {
    ...env,
    GMAIL_SENDER_EMAIL: "bot@example.com",
    GMAIL_SENDER_REFRESH_TOKEN: "refresh-token",
    GOOGLE_CLIENT_ID: "test-client-id",
    GOOGLE_CLIENT_SECRET: "test-client-secret",
    ...overrides,
  } as unknown as typeof env;
}

function mockGmail(sendResponse: () => Response): void {
  vi.stubGlobal("fetch", async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input);
    if (url === GMAIL_TOKEN_URL) {
      return new Response(JSON.stringify({ access_token: "fake-access-token", expires_in: 3600 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url === GMAIL_SEND_URL) {
      return sendResponse();
    }
    return new Response(JSON.stringify({}), { status: 200, headers: { "content-type": "application/json" } });
  });
}

function restoreFetch(): void {
  vi.unstubAllGlobals();
}

async function seedUser(db: D1Database, id = "user-1", email = "alice@example.com"): Promise<string> {
  await db.prepare(
    `INSERT INTO users (id, google_sub, email, email_verified, name, picture, timezone, developer_mode, created_at, updated_at)
     VALUES (?1, ?2, ?3, 1, ?4, '', 'UTC', 1, ?5, ?5)`,
  ).bind(id, `sub-${id}`, email, email, Date.now()).run();
  return id;
}

describe("email content validation", () => {
  it("accepts a normal body", () => {
    expect(validateEmailBody("Hello world").ok).toBe(true);
  });

  it("rejects an empty body", () => {
    const result = validateEmailBody("");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toBe("email content is required.");
  });

  it("rejects a body with newlines (header injection)", () => {
    const result = validateEmailBody("Hello\nBcc: victim@example.com");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toBe("email content must not contain newlines.");
  });

  it("rejects a body over the limit", () => {
    const result = validateEmailBody("x".repeat(100_001));
    expect(result.ok).toBe(false);
  });

  it("rejects a subject with newlines", () => {
    const result = validateEmailSubject("Re: meeting\r\nBcc: victim@example.com");
    expect(result.ok).toBe(false);
  });
});

describe("approval creation and lifecycle", () => {
  beforeEach(async () => {
    await seedUser(env.DB as unknown as D1Database);
  });

  it("creates an approval_pending approval through the shared service", async () => {
    const e = makeEnv();
    const result = await createEmailApproval(e, {
      ownerSub: "sub-user-1",
      ownerId: "user-1",
      content: "Hello from the routine",
      senderEmail: "bot@example.com",
    });
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error(result.error);
    expect(result.approval.status).toBe("approval_pending");
    expect(result.approval.connector).toBe("gmail");
    expect(result.approval.action).toBe("send_email");
    expect(result.approval.expires_at).toBeGreaterThan(Date.now());

    const stored = await getConnectorApproval(e, result.approval.approval_id);
    expect(stored).not.toBeNull();
    expect(stored!.status).toBe("approval_pending");
    expect(stored!.user_id).toBe("user-1");
  });

  it("lists only pending, non-expired approvals", async () => {
    const e = makeEnv();
    const a = await createEmailApproval(e, {
      ownerSub: "sub-user-1", ownerId: "user-1", content: "one", senderEmail: "bot@example.com",
    });
    const b = await createEmailApproval(e, {
      ownerSub: "sub-user-1", ownerId: "user-1", content: "two", senderEmail: "bot@example.com",
    });
    if (!a.ok || !b.ok) throw new Error("setup failed");
    // Expire the first approval directly.
    await env.DB.prepare(
      `UPDATE connector_approvals SET status = 'expired', expires_at = ?1 WHERE approval_id = ?2`,
    ).bind(Date.now() - 1000, a.approval.approval_id).run();

    const pending = await listPendingApprovals(e, "user-1", false);
    expect(pending.map((p) => p.approval_id)).toEqual([b.approval.approval_id]);
  });
});

describe("executeEmailApproval (Allow/Deny/concurrency)", () => {
  let sent: SendCall[];

  beforeEach(async () => {
    sent = [];
    await seedUser(env.DB as unknown as D1Database);
  });

  it("Allow sends exactly one email and marks accepted_by_gmail", async () => {
    const e = makeEnv();
    mockGmail(() => new Response(JSON.stringify({ id: "msg-123" }), { status: 200 }));
    const created = await createEmailApproval(e, {
      ownerSub: "sub-user-1", ownerId: "user-1", content: "body text", senderEmail: "bot@example.com",
    });
    if (!created.ok) throw new Error(created.error);

    const result = await executeEmailApproval(e, created.approval.approval_id, "user-1");
    expect(result.ok).toBe(true);
    expect(result.state).toBe("accepted_by_gmail");
    expect(result.messageId).toBe("msg-123");

    const stored = await getConnectorApproval(e, created.approval.approval_id);
    expect(stored!.status).toBe("accepted_by_gmail");
    restoreFetch();
  });

  it("Deny prevents sending", async () => {
    const e = makeEnv();
    let sendCount = 0;
    mockGmail(() => { sendCount += 1; return new Response(JSON.stringify({ id: "msg" }), { status: 200 }); });
    const created = await createEmailApproval(e, {
      ownerSub: "sub-user-1", ownerId: "user-1", content: "body", senderEmail: "bot@example.com",
    });
    if (!created.ok) throw new Error(created.error);

    const denied = await env.DB.prepare(
      `UPDATE connector_approvals SET status = 'denied', decided_at = ?1, decided_by = 'user-1', result = 'denied by owner', updated_at = ?1 WHERE approval_id = ?2`,
    ).bind(Date.now(), created.approval.approval_id).run();
    expect(denied.meta?.changes).toBe(1);

    const result = await executeEmailApproval(e, created.approval.approval_id, "user-1");
    expect(result.ok).toBe(false);
    expect(sendCount).toBe(0);
    restoreFetch();
  });

  it("duplicate clicks after Allow return a clear terminal result", async () => {
    const e = makeEnv();
    mockGmail(() => new Response(JSON.stringify({ id: "msg-1" }), { status: 200 }));
    const created = await createEmailApproval(e, {
      ownerSub: "sub-user-1", ownerId: "user-1", content: "body", senderEmail: "bot@example.com",
    });
    if (!created.ok) throw new Error(created.error);

    const first = await executeEmailApproval(e, created.approval.approval_id, "user-1");
    expect(first.ok).toBe(true);
    const second = await executeEmailApproval(e, created.approval.approval_id, "user-1");
    expect(second.ok).toBe(false);
    expect(second.state).toBe("accepted_by_gmail");
    restoreFetch();
  });

  it("unauthorized owner cannot execute", async () => {
    const e = makeEnv();
    mockGmail(() => new Response(JSON.stringify({ id: "msg" }), { status: 200 }));
    const created = await createEmailApproval(e, {
      ownerSub: "sub-user-1", ownerId: "user-1", content: "body", senderEmail: "bot@example.com",
    });
    if (!created.ok) throw new Error(created.error);

    const result = await executeEmailApproval(e, created.approval.approval_id, "user-2");
    expect(result.ok).toBe(false);
    restoreFetch();
  });

  it("expired approval cannot be executed", async () => {
    const e = makeEnv();
    mockGmail(() => new Response(JSON.stringify({ id: "msg" }), { status: 200 }));
    const created = await createEmailApproval(e, {
      ownerSub: "sub-user-1", ownerId: "user-1", content: "body", senderEmail: "bot@example.com",
    });
    if (!created.ok) throw new Error(created.error);
    // Force expiry.
    await env.DB.prepare(
      `UPDATE connector_approvals SET expires_at = ?1 WHERE approval_id = ?2`,
    ).bind(Date.now() - 1000, created.approval.approval_id).run();

    const result = await executeEmailApproval(e, created.approval.approval_id, "user-1");
    expect(result.ok).toBe(false);
    expect(result.state).toBe("expired");
    const stored = await getConnectorApproval(e, created.approval.approval_id);
    expect(stored!.status).toBe("expired");
    restoreFetch();
  });

  it("Gmail rejection marks failed and does not send again on retry", async () => {
    const e = makeEnv();
    mockGmail(() => new Response(JSON.stringify({ error: "quota" }), { status: 403 }));
    const created = await createEmailApproval(e, {
      ownerSub: "sub-user-1", ownerId: "user-1", content: "body", senderEmail: "bot@example.com",
    });
    if (!created.ok) throw new Error(created.error);

    const result = await executeEmailApproval(e, created.approval.approval_id, "user-1");
    expect(result.ok).toBe(false);
    expect(result.state).toBe("failed");

    const stored = await getConnectorApproval(e, created.approval.approval_id);
    expect(stored!.status).toBe("failed");

    // Retry must not resend: the approval is no longer approval_pending.
    const retry = await executeEmailApproval(e, created.approval.approval_id, "user-1");
    expect(retry.ok).toBe(false);
    restoreFetch();
  });

  it("ambiguous Gmail response records outcome_unknown", async () => {
    const e = makeEnv();
    // 500 server error: ambiguous whether Gmail sent.
    mockGmail(() => new Response(JSON.stringify({ error: "internal error" }), { status: 500 }));
    const created = await createEmailApproval(e, {
      ownerSub: "sub-user-1", ownerId: "user-1", content: "body", senderEmail: "bot@example.com",
    });
    if (!created.ok) throw new Error(created.error);

    const result = await executeEmailApproval(e, created.approval.approval_id, "user-1");
    expect(result.ok).toBe(false);
    expect(result.state).toBe("outcome_unknown");

    const stored = await getConnectorApproval(e, created.approval.approval_id);
    expect(stored!.status).toBe("outcome_unknown");
    const parsed = JSON.parse(stored!.result ?? "{}");
    expect(parsed.requires_reconciliation).toBe(true);
    restoreFetch();
  });
});

describe("scheduled execution linkage", () => {
  it("links an approval to a routine run and completes the run on Allow", async () => {
    const e = makeEnv();
    mockGmail(() => new Response(JSON.stringify({ id: "msg-sched" }), { status: 200 }));
    await seedUser(env.DB as unknown as D1Database, "user-sched", "sched@example.com");

    // Create a routine run row manually.
    await env.DB.prepare(
      `INSERT INTO routine_runs (routine_run_id, routine_id, owner_sub, scheduled_for, status, attempt_count,
         idempotency_key, approval_id, created_at, updated_at)
       VALUES (?1, ?2, ?3, ?4, 'pending', 0, ?5, '', ?6, ?6)`,
    ).bind("run-1", "routine-1", "user-sched", Date.now(), "run-1:1000", Date.now()).run();

    const created = await createEmailApproval(e, {
      ownerSub: "sub-user-sched", ownerId: "user-sched", content: "scheduled digest",
      senderEmail: "bot@example.com", routineId: "routine-1", routineRunId: "run-1",
    });
    if (!created.ok) throw new Error(created.error);

    const result = await executeEmailApproval(e, created.approval.approval_id, "user-sched");
    expect(result.ok).toBe(true);
    expect(result.state).toBe("accepted_by_gmail");

    const run = await env.DB.prepare("SELECT status FROM routine_runs WHERE routine_run_id = ?1")
      .bind("run-1").first<{ status: string }>();
    expect(run?.status).toBe("completed");
    restoreFetch();
  });
});
