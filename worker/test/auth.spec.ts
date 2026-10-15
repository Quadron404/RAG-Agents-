import { SELF, env, fetchMock } from "cloudflare:test";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { hashSessionToken } from "../src/store";
import { BASE, beginLogin, completeLogin, googleIdToken, jwksFor, makeKeypair } from "./helpers";

let keys: CryptoKeyPair;
let jwks: { keys: JsonWebKey[] };

function mockToken(idToken: string): { getBody: () => string } {
  let body = "";
  fetchMock
    .get("https://oauth.test")
    .intercept({ path: "/token", method: "POST" })
    .reply((options) => {
      body = String((options as { body?: unknown }).body ?? "");
      return {
        statusCode: 200,
        data: JSON.stringify({ id_token: idToken, access_token: "at", expires_in: 3600 }),
      };
    });
  return { getBody: () => body };
}

beforeAll(async () => {
  keys = await makeKeypair();
  jwks = (await jwksFor(keys.publicKey)) as { keys: JsonWebKey[] };
  fetchMock.activate();
  fetchMock.disableNetConnect();
  fetchMock
    .get("https://jwks.test")
    .intercept({ path: "/certs" })
    .reply(200, JSON.stringify(jwks))
    .persist();
});

afterAll(() => {
  fetchMock.assertNoPendingInterceptors();
});

describe("Google login flow", () => {
  it("sends the browser to Google with login scopes only", async () => {
    const started = await beginLogin();
    const url = new URL(started.authorizationUrl);
    expect(url.origin + url.pathname).toBe("https://accounts.google.com/o/oauth2/v2/auth");
    expect(url.searchParams.get("scope")).toBe("openid email profile");
    expect(url.searchParams.get("scope") ?? "").not.toContain("gmail");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("code_challenge")).toBeTruthy();
    expect(url.searchParams.get("state")).toBe(started.state);
    expect(url.searchParams.get("nonce")).toBe(started.nonce);
  });

  it("completes a login, marks the cookie HttpOnly/Secure/SameSite, and authenticates", async () => {
    const started = await beginLogin("/inbox");
    const token = await googleIdToken(keys.privateKey, { nonce: started.nonce, email: "alice@example.com" });
    mockToken(token);

    const callback = await SELF.fetch(
      `${BASE}/auth/callback?state=${started.state}&code=test-code`,
      { redirect: "manual" },
    );
    expect(callback.status).toBe(302);
    expect(callback.headers.get("location")).toBe(`${BASE}/inbox`);
    const setCookie = callback.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain("rag_session=");
    expect(setCookie.toLowerCase()).toContain("httponly");
    expect(setCookie.toLowerCase()).toContain("secure");
    expect(setCookie).toContain("SameSite=Lax");

    const cookie = setCookie.split(";")[0];
    const session = await SELF.fetch(`${BASE}/auth/session`, { headers: { cookie } });
    const body = (await session.json()) as {
      authenticated: boolean;
      csrf: string;
      user: { email: string };
      gmail: { connected: boolean; status: string };
    };
    expect(body.authenticated).toBe(true);
    expect(body.user.email).toBe("alice@example.com");
    expect(body.csrf).toBeTruthy();
    // The edge never claims a mailbox is connected; the backend owns that fact
    // and the UI reads /auth/gmail/status for it.
    expect(body.gmail.connected).toBe(false);
    expect(body.gmail.status).toBe("unknown");
  });

  it("returns an anonymous shape rather than 401 before sign-in", async () => {
    const response = await SELF.fetch(`${BASE}/auth/session`);
    expect(response.status).toBe(200);
    const body = (await response.json()) as { authenticated: boolean; user: unknown };
    expect(body.authenticated).toBe(false);
    expect(body.user).toBeNull();
  });

  it("stores only a hash of the session token", async () => {
    const started = await beginLogin();
    const token = await googleIdToken(keys.privateKey, { nonce: started.nonce });
    mockToken(token);
    const session = await completeLogin({ state: started.state, idToken: token });
    const raw = session.cookie.slice("rag_session=".length);
    const expected = await hashSessionToken(env, raw);
    const row = await env.DB.prepare("SELECT session_hash FROM sessions WHERE user_id = ?1")
      .bind(session.userId)
      .first<{ session_hash: string }>();
    expect(row?.session_hash).toBe(expected);
    expect(row?.session_hash).not.toBe(raw);
  });

  it("sends the stored PKCE verifier to the token endpoint", async () => {
    const started = await beginLogin();
    expect(started.verifier.length).toBeGreaterThanOrEqual(43);
    const token = await googleIdToken(keys.privateKey, { nonce: started.nonce });
    const capture = mockToken(token);
    await completeLogin({ state: started.state, idToken: token });
    expect(capture.getBody()).toContain("code_verifier=");
    expect(decodeURIComponent(capture.getBody())).toContain(started.verifier);
  });

  it("rejects a replayed authorization state", async () => {
    const started = await beginLogin();
    const token = await googleIdToken(keys.privateKey, { nonce: started.nonce });
    mockToken(token);
    await completeLogin({ state: started.state, idToken: token });
    const replay = await SELF.fetch(
      `${BASE}/auth/callback?state=${started.state}&code=test-code`,
      { redirect: "manual" },
    );
    expect(replay.status).toBe(400);
  });

  it("rejects an id_token with the wrong nonce and records the rejection", async () => {
    const started = await beginLogin();
    const token = await googleIdToken(keys.privateKey, { nonce: "wrong-nonce" });
    mockToken(token);
    const response = await SELF.fetch(
      `${BASE}/auth/callback?state=${started.state}&code=test-code`,
      { redirect: "manual" },
    );
    expect(response.status).toBe(400);
    const audit = await env.DB.prepare(
      "SELECT outcome FROM login_audit WHERE outcome = 'rejected'",
    ).first<{ outcome: string }>();
    expect(audit?.outcome).toBe("rejected");
  });

  it("redirects a denied consent without minting a session", async () => {
    const response = await SELF.fetch(`${BASE}/auth/callback?error=access_denied`, {
      redirect: "manual",
    });
    expect(response.status).toBe(302);
    expect(response.headers.get("location")).toBe(`${BASE}/?auth=denied`);
    expect(response.headers.get("set-cookie")).toBeNull();
  });
});

describe("logout", () => {
  it("requires CSRF and then revokes the session", async () => {
    const started = await beginLogin();
    const token = await googleIdToken(keys.privateKey, { nonce: started.nonce });
    mockToken(token);
    const session = await completeLogin({ state: started.state, idToken: token });

    const withoutCsrf = await SELF.fetch(`${BASE}/auth/logout`, {
      method: "POST",
      headers: { cookie: session.cookie },
    });
    expect(withoutCsrf.status).toBe(403);

    const withCsrf = await SELF.fetch(`${BASE}/auth/logout`, {
      method: "POST",
      headers: { cookie: session.cookie, "x-rag-csrf": session.csrf },
    });
    expect(withCsrf.status).toBe(200);
    expect(withCsrf.headers.get("set-cookie") ?? "").toContain("rag_session=;");

    const after = await SELF.fetch(`${BASE}/auth/session`, { headers: { cookie: session.cookie } });
    expect(((await after.json()) as { authenticated: boolean }).authenticated).toBe(false);
  });
});
