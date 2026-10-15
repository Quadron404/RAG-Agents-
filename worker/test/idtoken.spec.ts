import { describe, expect, it } from "vitest";
import { TokenError, decodeUnverified, verifyIdToken } from "../src/idtoken";
import { googleIdToken, jwksFor, makeKeypair, signJwt } from "./helpers";

async function setup() {
  const keys = await makeKeypair();
  const jwks = await jwksFor(keys.publicKey);
  return { keys, jwks };
}

describe("Google ID token verification", () => {
  it("accepts a well-formed token", async () => {
    const { keys, jwks } = await setup();
    const token = await googleIdToken(keys.privateKey, { nonce: "n-1" });
    const claims = await verifyIdToken(token, { jwks, audience: "test-client-id", nonce: "n-1" });
    expect(claims.sub).toBe("sub-1");
  });

  it("rejects an unknown kid", async () => {
    const { keys, jwks } = await setup();
    const token = await googleIdToken(keys.privateKey, { kid: "missing" });
    await expect(verifyIdToken(token, { jwks, audience: "test-client-id" })).rejects.toBeInstanceOf(TokenError);
  });

  it("rejects a non-RS256 algorithm", async () => {
    const { keys, jwks } = await setup();
    const token = await signJwt(
      keys.privateKey,
      { alg: "none", typ: "JWT", kid: "k1" },
      { iss: "https://accounts.google.com", aud: "test-client-id", sub: "s", exp: 2 ** 40 },
    );
    await expect(verifyIdToken(token, { jwks, audience: "test-client-id" })).rejects.toBeInstanceOf(TokenError);
  });

  it("rejects HS256 even when a key is present", async () => {
    const { keys, jwks } = await setup();
    const token = await signJwt(
      keys.privateKey,
      { alg: "HS256", typ: "JWT", kid: "k1" },
      { iss: "https://accounts.google.com", aud: "test-client-id", sub: "s", exp: 2 ** 40 },
    );
    await expect(verifyIdToken(token, { jwks, audience: "test-client-id" })).rejects.toBeInstanceOf(TokenError);
  });

  it("rejects a tampered signature", async () => {
    const { keys, jwks } = await setup();
    const token = await googleIdToken(keys.privateKey);
    const parts = token.split(".");
    parts[2] = parts[2].slice(0, -4) + (parts[2].endsWith("AAAA") ? "BBBB" : "AAAA");
    await expect(
      verifyIdToken(parts.join("."), { jwks, audience: "test-client-id" }),
    ).rejects.toBeInstanceOf(TokenError);
  });

  it("rejects a wrong issuer", async () => {
    const { keys, jwks } = await setup();
    const token = await googleIdToken(keys.privateKey, { iss: "https://evil.example.com" });
    await expect(verifyIdToken(token, { jwks, audience: "test-client-id" })).rejects.toBeInstanceOf(TokenError);
  });

  it("rejects a wrong audience", async () => {
    const { keys, jwks } = await setup();
    const token = await googleIdToken(keys.privateKey, { aud: "other-client" });
    await expect(verifyIdToken(token, { jwks, audience: "test-client-id" })).rejects.toBeInstanceOf(TokenError);
  });

  it("rejects an expired token", async () => {
    const { keys, jwks } = await setup();
    const token = await googleIdToken(keys.privateKey, { exp: Math.floor(Date.now() / 1000) - 300 });
    await expect(verifyIdToken(token, { jwks, audience: "test-client-id" })).rejects.toBeInstanceOf(TokenError);
  });

  it("rejects a token issued in the future", async () => {
    const { keys, jwks } = await setup();
    const token = await googleIdToken(keys.privateKey, { iat: Math.floor(Date.now() / 1000) + 3600 });
    await expect(verifyIdToken(token, { jwks, audience: "test-client-id" })).rejects.toBeInstanceOf(TokenError);
  });

  it("rejects a nonce mismatch", async () => {
    const { keys, jwks } = await setup();
    const token = await googleIdToken(keys.privateKey, { nonce: "other" });
    await expect(
      verifyIdToken(token, { jwks, audience: "test-client-id", nonce: "expected" }),
    ).rejects.toBeInstanceOf(TokenError);
  });

  it("rejects an unverified email", async () => {
    const { keys, jwks } = await setup();
    const token = await googleIdToken(keys.privateKey, { email_verified: false });
    await expect(verifyIdToken(token, { jwks, audience: "test-client-id" })).rejects.toBeInstanceOf(TokenError);
  });

  it("rejects a missing subject", async () => {
    const { keys, jwks } = await setup();
    const token = await googleIdToken(keys.privateKey, { sub: "" });
    await expect(verifyIdToken(token, { jwks, audience: "test-client-id" })).rejects.toBeInstanceOf(TokenError);
  });

  it("decodes without trusting", async () => {
    const { keys } = await setup();
    const token = await googleIdToken(keys.privateKey, { kid: "abc" });
    const decoded = decodeUnverified(token);
    expect((decoded.header as { kid: string }).kid).toBe("abc");
    expect(decoded.payload.sub).toBe("sub-1");
  });
});
