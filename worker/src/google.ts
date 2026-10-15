/**
 * Google OpenID Connect: the authorization-code flow with PKCE.
 *
 * Only the login grant is implemented here.  It requests `openid email profile`
 * and nothing else, so signing in never asks for Gmail and never depends on
 * mailbox authorization.  A Gmail consent step is a separate, later grant that
 * a different layer owns; keeping it out of this module is what makes the two
 * independent.
 */
import type { Env } from "./env";
import { base64urlEncode, randomToken, sha256 } from "./encoding";
import type { Jwks } from "./idtoken";

export const LOGIN_SCOPES = ["openid", "email", "profile"] as const;

export class OAuthError extends Error {}

export function newState(): string {
  return randomToken(32);
}

export function newNonce(): string {
  return randomToken(24);
}

export function newCodeVerifier(): string {
  return randomToken(64);
}

export async function codeChallenge(verifier: string): Promise<string> {
  return base64urlEncode(await sha256(new TextEncoder().encode(verifier)));
}

export interface AuthorizationParams {
  redirectUri: string;
  state: string;
  nonce: string;
  verifier: string;
  loginHint?: string;
}

export async function buildAuthorizationUrl(
  env: Env,
  params: AuthorizationParams,
): Promise<string> {
  const query = new URLSearchParams({
    client_id: env.GOOGLE_CLIENT_ID,
    redirect_uri: params.redirectUri,
    response_type: "code",
    scope: LOGIN_SCOPES.join(" "),
    state: params.state,
    nonce: params.nonce,
    code_challenge: await codeChallenge(params.verifier),
    code_challenge_method: "S256",
    access_type: "online",
    include_granted_scopes: "true",
  });
  if (params.loginHint) {
    query.set("login_hint", params.loginHint);
  }
  return `${env.GOOGLE_AUTH_URL}?${query.toString()}`;
}

export interface TokenResponse {
  id_token?: string;
  access_token?: string;
  expires_in?: number;
  scope?: string;
  [key: string]: unknown;
}

export async function exchangeCode(
  env: Env,
  params: { code: string; redirectUri: string; verifier: string },
): Promise<TokenResponse> {
  const body = new URLSearchParams({
    client_id: env.GOOGLE_CLIENT_ID,
    client_secret: env.GOOGLE_CLIENT_SECRET,
    code: params.code,
    code_verifier: params.verifier,
    grant_type: "authorization_code",
    redirect_uri: params.redirectUri,
  });
  const response = await fetch(env.GOOGLE_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
    body: body.toString(),
  });
  if (response.status !== 200) {
    // The body carries an error code, never the client secret.
    throw new OAuthError(`token exchange failed with HTTP ${response.status}`);
  }
  const payload = (await response.json()) as TokenResponse;
  if (!payload.id_token && !payload.access_token) {
    throw new OAuthError("token response carried neither an id_token nor an access_token");
  }
  return payload;
}

let cachedJwks: { url: string; value: Jwks; expiresAt: number } | null = null;

export async function fetchJwks(env: Env): Promise<Jwks> {
  if (env.GOOGLE_JWKS_JSON) {
    return JSON.parse(env.GOOGLE_JWKS_JSON) as Jwks;
  }
  const now = Date.now();
  if (cachedJwks && cachedJwks.url === env.GOOGLE_JWKS_URL && cachedJwks.expiresAt > now) {
    return cachedJwks.value;
  }
  const response = await fetch(env.GOOGLE_JWKS_URL, { headers: { Accept: "application/json" } });
  if (response.status !== 200) {
    throw new OAuthError(`JWKS fetch failed with HTTP ${response.status}`);
  }
  const value = (await response.json()) as Jwks;
  cachedJwks = { url: env.GOOGLE_JWKS_URL, value, expiresAt: now + 3600 * 1000 };
  return value;
}
