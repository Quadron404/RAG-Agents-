/**
 * gmail-sender tests: the central Worker-native Gmail sender.
 *
 * Mocks fetch so no production Gmail credits are spent and no Google endpoint
 * is contacted.  Proves the sender-side rules that must hold regardless of the
 * live account: size limits, header-injection neutralisation, From resolving to
 * the verified alias, invalid_grant being permanent, and the send-as alias being
 * checked against Gmail's authoritative sendAs list before use.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import type { Env } from "../src/env";
import { GmailSenderError, sendGmail, __resetSenderCacheForTests } from "../src/gmail-sender";

const requests: { url: string; headers: Record<string, string>; body: string }[] = [];

function b64urlDecode(s: string): string {
  let b64 = s.replace(/-/g, "+").replace(/_/g, "/");
  while (b64.length % 4) b64 += "=";
  return decodeURIComponent(escape(atob(b64)));
}

function fakeFetch(response: Response, statusText?: string): ReturnType<typeof vi.fn> {
  return vi.fn(async (_url: string, init: RequestInit) => {
    const url = String(_url);
    let headerRecord: Record<string, string> = {};
    const hh = init.headers;
    if (hh) {
      if (typeof (hh as Headers).get === "function") {
        const h = hh as Headers;
        for (const key of ["authorization", "content-type"]) {
          const v = h.get(key);
          if (v) headerRecord[key] = v;
        }
      } else {
        for (const [k, v] of Object.entries(hh as Record<string, string>)) {
          headerRecord[String(k).toLowerCase()] = String(v);
        }
      }
    }
    requests.push({
      url,
      headers: headerRecord,
      body: typeof init.body === "string" ? init.body : "",
    });
    if (url.endsWith("/token")) {
      return new Response(
        JSON.stringify({ access_token: "access-token", expires_in: 3600 }),
        { status: 200 },
      );
    }
    return response;
  }) as unknown as ReturnType<typeof vi.fn>;
}

function envWith(overrides: Record<string, string> = {}): Env {
  return {
    GMAIL_SENDER_EMAIL: overrides.GMAIL_SENDER_EMAIL ?? "bot@example.com",
    GMAIL_SENDER_REFRESH_TOKEN: overrides.GMAIL_SENDER_REFRESH_TOKEN ?? "refresh-token",
    GMAIL_SENDER_ALIAS: overrides.GMAIL_SENDER_ALIAS ?? "",
    GOOGLE_CLIENT_ID: overrides.GOOGLE_CLIENT_ID ?? "client-id",
    GOOGLE_CLIENT_SECRET: overrides.GOOGLE_CLIENT_SECRET ?? "client-secret",
  } as unknown as Env;
}

beforeEach(() => {
  requests.length = 0;
  __resetSenderCacheForTests();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("gmail sender — message construction and limits", () => {
  it("sends to the Gmail send endpoint with a bearer token and base64raw body", async () => {
    globalThis.fetch = fakeFetch(new Response(JSON.stringify({ id: "gmail-msg-1" }), { status: 200 }));
    const id = await sendGmail(envWith(), "alice@example.com", "Hi", "hello there");
    expect(id).toBe("gmail-msg-1");
    const sendReqs = requests.filter((r) => r.url.endsWith("/messages/send"));
    expect(sendReqs).toHaveLength(1);
    const [req] = sendReqs;
    expect(req.url).toBe("https://gmail.googleapis.com/gmail/v1/users/me/messages/send");
    expect(req.headers.authorization).toMatch(/^Bearer /);
    expect(req.body).toContain('"raw"');
    const raw = JSON.parse(req.body).raw as string;
    const decoded = b64urlDecode(raw);
    expect(decoded).toContain("From: bot@example.com");
    expect(decoded).toContain("To: alice@example.com");
    expect(decoded).toContain("Subject: Hi");
    expect(decoded).toContain("hello there");
  });

  it("refuses an over-long subject and body", async () => {
    globalThis.fetch = fakeFetch(new Response(JSON.stringify({ id: "x" }), { status: 200 }));
    await expect(sendGmail(envWith(), "alice@example.com", "x".repeat(201), "body")).rejects.toBeInstanceOf(GmailSenderError);
    await expect(sendGmail(envWith(), "alice@example.com", "Hi", "y".repeat(100_001))).rejects.toBeInstanceOf(GmailSenderError);
    expect(requests).toHaveLength(0);
  });

  it("neutralises CR/LF in headers (header injection)", async () => {
    globalThis.fetch = fakeFetch(new Response(JSON.stringify({ id: "x" }), { status: 200 }));
    await sendGmail(envWith(), "alice@example.com", "Hi\r\nInjected: evil", "body");
    const sendReqs = requests.filter((r) => r.url.endsWith("/messages/send"));
    expect(sendReqs).toHaveLength(1);
    const raw = JSON.parse(sendReqs[0].body).raw as string;
    const decoded = b64urlDecode(raw);
    // The injected CR/LF must not appear as a header line break.
    expect(decoded.split("\r\n").filter((line) => line.startsWith("Injected:"))).toEqual([]);
    expect(decoded).toContain("Subject: Hi Injected: evil");
  });

  it("fails closed when the sender is not configured", async () => {
    await expect(sendGmail(envWith({ GMAIL_SENDER_EMAIL: "" }), "alice@example.com", "Hi", "body")).rejects.toBeInstanceOf(
      GmailSenderError,
    );
    expect(requests).toHaveLength(0);
  });
});

describe("gmail sender — token refresh permanence (invalid_grant)", () => {
  it("treats invalid_grant as permanent and does not retry", async () => {
    // The token endpoint rejects the grant permanently; no send is attempted.
    globalThis.fetch = vi.fn(async (_url: string) => {
      if (String(_url).endsWith("/token")) {
        return new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 });
      }
      return new Response(JSON.stringify({ id: "should-not-send" }), { status: 200 });
    }) as unknown as ReturnType<typeof vi.fn>;

    await expect(sendGmail(envWith(), "alice@example.com", "Hi", "body")).rejects.toMatchObject({
      permanent: true,
    });
    expect(requests.filter((r) => r.url.endsWith("/messages/send"))).toHaveLength(0);
  });
});

describe("gmail sender — send-as alias verification", () => {
  it("verifies the alias against the authoritative sendAs list before use", async () => {
    // First fetch: token endpoint.  Second fetch: sendAs list containing a
    // verified alias.  Third fetch: the actual send.
    globalThis.fetch = vi.fn(async (_url: string, init: RequestInit) => {
      const url = String(_url);
      let headerRecord: Record<string, string> = {};
      const hh = init.headers;
      if (hh && typeof (hh as Headers).get === "function") {
        const h = hh as Headers;
        for (const key of ["authorization", "content-type"]) {
          const v = h.get(key);
          if (v) headerRecord[key] = v;
        }
      } else if (hh) {
        for (const [k, v] of Object.entries(hh as Record<string, string>)) {
          headerRecord[String(k).toLowerCase()] = String(v);
        }
      }
      requests.push({ url, headers: headerRecord, body: typeof init.body === "string" ? init.body : "" });
      if (url.endsWith("/token")) {
        return new Response(JSON.stringify({ access_token: "access-token", expires_in: 3600 }), { status: 200 });
      }
      if (url.endsWith("/settings/sendAs")) {
        return new Response(JSON.stringify({ sendAs: [{ sendAsEmail: "alias@example.com", verificationStatus: "accepted" }] }), { status: 200 });
      }
      return new Response(JSON.stringify({ id: "gmail-msg-2" }), { status: 200 });
    }) as unknown as ReturnType<typeof vi.fn>;

    const id = await sendGmail(
      envWith({ GMAIL_SENDER_ALIAS: "alias@example.com" }),
      "alice@example.com",
      "Hi",
      "body",
    );
    expect(id).toBe("gmail-msg-2");
    const sendAsRequests = requests.filter((r) => r.url.endsWith("/settings/sendAs"));
    expect(sendAsRequests).toHaveLength(1);
    expect(sendAsRequests[0].headers.authorization).toMatch(/^Bearer /);
    const raw = JSON.parse(requests[requests.length - 1].body).raw as string;
    const decoded = b64urlDecode(raw);
    expect(decoded).toContain("From: alias@example.com");
  });

  it("fails closed when the alias is not a verified send-as identity", async () => {
    globalThis.fetch = vi.fn(async (_url: string) => {
      if (String(_url).endsWith("/token")) {
        return new Response(JSON.stringify({ access_token: "access-token", expires_in: 3600 }), { status: 200 });
      }
      return new Response(JSON.stringify({ sendAs: [] }), { status: 200 });
    }) as unknown as ReturnType<typeof vi.fn>;

    await expect(
      sendGmail(envWith({ GMAIL_SENDER_ALIAS: "unknown@example.com" }), "alice@example.com", "Hi", "body"),
    ).rejects.toMatchObject({ permanent: true });
    expect(requests.filter((r) => r.url.endsWith("/messages/send"))).toHaveLength(0);
  });
});
