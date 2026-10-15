/**
 * Reverse proxy to the Codespace backend.
 *
 * The Worker is the only caller the backend trusts, so on the way through it:
 *
 *   1. strips any client-supplied internal-token or identity header, so a
 *      browser cannot impersonate the gateway or another user;
 *   2. adds the shared `x-rag-internal-token`; and
 *   3. adds the authenticated identity (`x-rag-user-sub` and friends) taken
 *      from the edge session -- never from the request.  The subject is the
 *      identity; the backend keys its own user row off it.
 */
import type { Env } from "./env";
import {
  IDENTITY_EMAIL_HEADER,
  IDENTITY_EMAIL_VERIFIED_HEADER,
  IDENTITY_NAME_HEADER,
  IDENTITY_PICTURE_HEADER,
  IDENTITY_SUB_HEADER,
  INTERNAL_HEADER,
  SPOOFABLE_HEADERS,
} from "./env";
import type { Principal } from "./principal";

export function buildUpstreamRequest(
  request: Request,
  env: Env,
  url: URL,
  principal: Principal | null,
): Request {
  const base = env.BACKEND_ORIGIN.replace(/\/+$/, "");
  const target = new URL(base + url.pathname + url.search);

  const headers = new Headers(request.headers);
  for (const name of SPOOFABLE_HEADERS) {
    headers.delete(name);
  }
  headers.delete("host");

  headers.set(INTERNAL_HEADER, env.RAG_INTERNAL_TOKEN);
  if (principal) {
    headers.set(IDENTITY_SUB_HEADER, principal.user.google_sub);
    headers.set(IDENTITY_EMAIL_HEADER, principal.user.email);
    headers.set(IDENTITY_EMAIL_VERIFIED_HEADER, principal.user.email_verified ? "1" : "0");
    headers.set(IDENTITY_NAME_HEADER, principal.user.name);
    headers.set(IDENTITY_PICTURE_HEADER, principal.user.picture);
  }

  // A GET/HEAD must not carry a body; everything else streams through.
  const hasBody = !["GET", "HEAD"].includes(request.method);
  return new Request(target.toString(), {
    method: request.method,
    headers,
    body: hasBody ? request.body : undefined,
    redirect: "manual",
  });
}
