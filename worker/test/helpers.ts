/**
 * Test helpers: mint real RS256 Google ID tokens against a locally generated
 * key, and drive a real login through the Worker to obtain a session.
 */
import { SELF, env } from "cloudflare:test";
import { base64urlEncode } from "../src/encoding";

const encoder = new TextEncoder();

export interface KeyPair {
  privateKey: CryptoKey;
  publicKey: CryptoKey;
}

export async function makeKeypair(): Promise<KeyPair> {
  return (await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  )) as CryptoKeyPair;
}

export async function jwksFor(publicKey: CryptoKey, kid = "k1") {
  const jwk = (await crypto.subtle.exportKey("jwk", publicKey)) as JsonWebKey;
  return {
    keys: [{ ...jwk, kid, alg: "RS256", use: "sig" }],
  };
}

function encodeJson(value: unknown): string {
  return base64urlEncode(encoder.encode(JSON.stringify(value)));
}

export async function signJwt(
  privateKey: CryptoKey,
  header: Record<string, unknown>,
  payload: Record<string, unknown>,
): Promise<string> {
  const encodedHeader = encodeJson(header);
  const encodedPayload = encodeJson(payload);
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    privateKey,
    encoder.encode(`${encodedHeader}.${encodedPayload}`),
  );
  return `${encodedHeader}.${encodedPayload}.${base64urlEncode(new Uint8Array(signature))}`;
}

export interface IdTokenOptions {
  sub?: string;
  iss?: string;
  aud?: string;
  exp?: number;
  iat?: number;
  nonce?: string;
  email?: string | null;
  email_verified?: boolean;
  kid?: string;
  alg?: string;
}

export async function googleIdToken(
  privateKey: CryptoKey,
  options: IdTokenOptions = {},
): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  const payload: Record<string, unknown> = {
    iss: options.iss ?? "https://accounts.google.com",
    aud: options.aud ?? "test-client-id",
    sub: options.sub ?? "sub-1",
    iat: options.iat ?? now - 60,
    exp: options.exp ?? now + 3600,
    azp: options.aud ?? "test-client-id",
  };
  if (options.email !== null) {
    payload.email = options.email ?? "alice@example.com";
    payload.email_verified = options.email_verified ?? true;
  }
  if (options.nonce !== undefined) {
    payload.nonce = options.nonce;
  }
  return signJwt(
    privateKey,
    { alg: options.alg ?? "RS256", typ: "JWT", kid: options.kid ?? "k1" },
    payload,
  );
}

export const BASE = "https://example.com";

export interface StartedLogin {
  state: string;
  nonce: string;
  verifier: string;
  authorizationUrl: string;
}

/** Start the login flow and read back the state the Worker stored. */
export async function beginLogin(next = "/"): Promise<StartedLogin> {
  const response = await SELF.fetch(`${BASE}/auth/login?next=${encodeURIComponent(next)}`, {
    redirect: "manual",
  });
  const authorizationUrl = response.headers.get("location") ?? "";
  const state = new URL(authorizationUrl).searchParams.get("state") ?? "";
  const row = await env.DB.prepare(
    "SELECT nonce, code_verifier FROM oauth_states WHERE state = ?1",
  )
    .bind(state)
    .first<{ nonce: string; code_verifier: string }>();
  return {
    state,
    nonce: row?.nonce ?? "",
    verifier: row?.code_verifier ?? "",
    authorizationUrl,
  };
}

export interface SessionCookie {
  cookie: string;
  setCookie: string;
  csrf: string;
  userId: string;
  email: string;
}

/** Complete a login with a given id_token and return the session cookie. */
export async function completeLogin(
  options: { state: string; idToken: string },
): Promise<SessionCookie> {
  const callback = await SELF.fetch(
    `${BASE}/auth/callback?state=${options.state}&code=test-code`,
    { redirect: "manual" },
  );
  const setCookie = callback.headers.get("set-cookie") ?? "";
  const cookie = setCookie.split(";")[0];
  const session = await SELF.fetch(`${BASE}/auth/session`, { headers: { cookie } });
  const body = (await session.json()) as { csrf: string; user: { id: string; email: string } };
  return { cookie, setCookie, csrf: body.csrf, userId: body.user.id, email: body.user.email };
}
