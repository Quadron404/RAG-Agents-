/**
 * Google ID token verification, implemented against the OpenID Connect spec.
 *
 * This is deliberately not a general JWT library.  Google ID tokens are always
 * RS256, and the only claims this application trusts are the ones validated
 * here, so every rule stays explicit and testable:
 *
 * - the header algorithm must be RS256 (never `none`, never HMAC);
 * - the signature must verify against the key Google names by `kid`;
 * - `iss` must be a Google issuer;
 * - `aud` must be this application's client id;
 * - `exp`/`iat` must be present and sane (with a small clock-skew allowance);
 * - `sub` must be present (it is the only stable identity);
 * - `nonce` must match the value sent in the authorization request;
 * - a present `email` must have `email_verified === true`.
 *
 * Nothing here trusts a frontend-supplied address: the subject claim is the
 * identity, and only a rule-valid token can produce it.
 */
import { base64urlDecode, fromUtf8, utf8 } from "./encoding";

export const GOOGLE_ISSUERS = ["https://accounts.google.com", "accounts.google.com"];
export const DEFAULT_LEEWAY_SECONDS = 120;

export type Jwks = { keys?: Array<Record<string, unknown>> };

export class TokenError extends Error {}

export interface VerifyOptions {
  jwks: Jwks;
  audience: string;
  nonce?: string | null;
  now?: number;
  leeway?: number;
  issuers?: string[];
}

function decodeJsonSegment(segment: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(fromUtf8(base64urlDecode(segment)));
  } catch {
    throw new TokenError("segment is not valid base64url JSON");
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new TokenError("segment is not a JSON object");
  }
  return parsed as Record<string, unknown>;
}

async function importVerificationKey(jwks: Jwks, kid: unknown): Promise<CryptoKey> {
  let keys = Array.isArray(jwks.keys) ? jwks.keys : [];
  if (typeof kid === "string" && kid) {
    keys = keys.filter((key) => key.kid === kid);
  }
  if (keys.length === 0) {
    throw new TokenError(`no signing key matches kid ${String(kid)}`);
  }
  const jwk = { ...keys[0] };
  if (jwk.kty !== "RSA") {
    throw new TokenError(`unsupported key type ${String(jwk.kty)}`);
  }
  try {
    return await crypto.subtle.importKey(
      "jwk",
      jwk as unknown as JsonWebKey,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
  } catch {
    throw new TokenError("signing key could not be imported");
  }
}

/** Verify the RS256 signature and return the header. */
export async function verifySignature(
  token: string,
  jwks: Jwks,
): Promise<Record<string, unknown>> {
  const parts = token.split(".");
  if (parts.length !== 3) {
    throw new TokenError("a JWT must have three segments");
  }
  const header = decodeJsonSegment(parts[0]);
  if (header.alg !== "RS256") {
    throw new TokenError(`unsupported algorithm ${String(header.alg)}`);
  }
  const key = await importVerificationKey(jwks, header.kid);
  const signature = base64urlDecode(parts[2]);
  const signingInput = utf8(`${parts[0]}.${parts[1]}`);
  const valid = await crypto.subtle.verify(
    "RSASSA-PKCS1-v1_5",
    key,
    signature,
    signingInput,
  );
  if (!valid) {
    throw new TokenError("signature does not verify");
  }
  return header;
}

/**
 * Fully validate a Google ID token and return its claims.
 *
 * Raises {@link TokenError} on any failure; callers map that to an
 * authentication failure and never to a partial success.
 */
export async function verifyIdToken(
  token: string,
  options: VerifyOptions,
): Promise<Record<string, unknown>> {
  const { jwks, audience, nonce, now, leeway = DEFAULT_LEEWAY_SECONDS } = options;
  const issuers = options.issuers ?? GOOGLE_ISSUERS;
  if (!audience) {
    throw new TokenError("no client id configured to validate against");
  }
  const current = now ?? Date.now() / 1000;

  await verifySignature(token, jwks);
  const claims = decodeJsonSegment(token.split(".")[1]);

  const iss = claims.iss;
  if (typeof iss !== "string" || !issuers.includes(iss)) {
    throw new TokenError(`unexpected issuer ${String(iss)}`);
  }

  const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!aud.includes(audience)) {
    throw new TokenError("audience does not match this client id");
  }

  const exp = claims.exp;
  if (typeof exp !== "number") {
    throw new TokenError("token has no expiration");
  }
  if (current > exp + leeway) {
    throw new TokenError("token has expired");
  }

  const iat = claims.iat;
  if (typeof iat === "number" && iat - leeway > current) {
    throw new TokenError("token was issued in the future");
  }

  if (typeof claims.sub !== "string" || !claims.sub) {
    throw new TokenError("token has no subject identifier");
  }

  if (nonce !== undefined && nonce !== null && claims.nonce !== nonce) {
    throw new TokenError("nonce does not match the authorization request");
  }

  if (claims.email && claims.email_verified !== true) {
    throw new TokenError("email is present but not verified");
  }

  return claims;
}

/** The header and payload without verifying anything. Reporting only. */
export function decodeUnverified(token: string): {
  header: Record<string, unknown>;
  payload: Record<string, unknown>;
} {
  const parts = token.split(".");
  if (parts.length !== 3) {
    throw new TokenError("a JWT must have three segments");
  }
  return { header: decodeJsonSegment(parts[0]), payload: decodeJsonSegment(parts[1]) };
}
